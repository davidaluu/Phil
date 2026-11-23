# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from phil.report import (
    FOOTER_TEXT,
    format_date,
    format_time,
    plain_step_sentence,
    render_report,
    select_frame,
)
from phil.runs import RunRecordError, RunStore


def make_record(run_id="r1", status="completed", start="2026-11-22T20:07:15+01:00"):
    return {
        "format_version": 1,
        "run_id": run_id,
        "task": "Do the sample task",
        "start_time": start,
        "end_time": "2026-11-22T20:07:17+01:00",
        "status": status,
        "status_message": "Done" if status == "completed" else "Repeated 'click' without the screen changing.",
        "total_steps": 1,
        "total_seconds": 2,
        "total_tokens": 40,
        "run_type": {"kind": "ai", "handover_step": None},
        "keep": False,
        "model_calls": [],
        "steps": [{
            "number": 1,
            "time": start,
            "duration_seconds": 1.8,
            "screen": {"mode": "full", "full_tree": True, "changed": False, "reason": "first step", "elements": [
                {"key": "id:sample", "text": "Dark mode", "description": "", "bounds": [10, 20, 300, 90], "traits": ["clickable"]}
            ]},
            "action": {"action": "click", "target": "Dark mode", "reason": "Enable it"},
            "result": {"ok": status == "completed", "status": "OK" if status == "completed" else "FAILED", "error": None if status == "completed" else "Element not found"},
            "guards": {},
        }],
        "summary": {"text": "Phil completed the sample task.", "ai_written": True},
        "why_stopped": None,
        "last_action": "click",
        "password_type_targeted": False,
    }


def test_store_save_load_and_format_version(tmp_path):
    store = RunStore(tmp_path)
    record = make_record()
    assert store.save(record)
    assert store.load(tmp_path / "r1.json")["run_id"] == "r1"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format_version": 999}))
    try:
        store.load(bad)
        raise AssertionError("missing unknown format should be rejected")
    except RunRecordError as exc:
        assert "unknown format version" in str(exc)


def test_date_and_time_formatting():
    value = "2026-11-22T20:07:15+01:00"
    assert format_date(value) == "Sunday, 22 November 2026"
    assert format_time(value) == "20:07:15 · UTC+01:00"
    assert format_time(value, "12") == "08:07:15 PM · UTC+01:00"


def test_multi_day_grouping_and_collapsed_views():
    html = render_report([make_record(status="completed"), make_record("r2", "failed", "2026-11-21T20:07:15+01:00")])
    assert "Sunday, 22 November 2026" in html
    assert "Saturday, 21 November 2026" in html
    assert html.count("<details class=") >= 2
    assert "Show screen elements" in html
    assert "Show wireframe" in html
    assert "Expand all screens" in html
    assert "Collapse all screens" in html


def test_report_layout_has_responsive_phone_wireframe_and_timeline():
    html = render_report([make_record()])
    assert "class=\"phone-svg\"" in html
    assert "viewBox=\"-24 -24 1128 2448\"" in html
    assert "height:min(75vh,680px)" in html
    assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in html
    assert "data-enlarge" in html and "wireDialog.showModal()" in html
    assert "wireDialog.close()" in html
    assert "class=\"stat-card\"" in html
    assert "class=\"step-time\"" in html
    assert "class=\"step-status step-ok\"" in html


def test_plain_sentence_and_footer_download_print_standalone():
    step = make_record()["steps"][0]
    assert "Tapped" in plain_step_sentence(step)
    html = render_report([make_record()])
    assert FOOTER_TEXT in html
    assert 'id="saveCopy"' in html
    assert 'id="printPdf"' in html
    assert 'src="' not in html
    assert "@media print" in html
    assert "window.print()" in html


def test_time_frames_around_midnight_fixed_timezone():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 22, 0, 30, tzinfo=zone)
    records = [
        make_record("before", start="2026-11-21T23:00:00-05:00"),
        make_record("after", start="2026-11-22T00:15:00-05:00"),
    ]
    selected = select_frame(records, 3, now)
    assert [record["run_id"] for record in selected] == ["after"]


def test_dst_change_fixed_zone():
    zone = ZoneInfo("America/New_York")
    record = make_record("dst", start="2026-11-01T01:30:00-05:00")
    now = datetime(2026, 11, 1, 2, 30, tzinfo=zone)
    assert select_frame([record], 3, now)


def test_cleanup_keep_and_delete(tmp_path):
    old = make_record("old")
    old["start_time"] = "2020-01-01T00:00:00+00:00"
    old["end_time"] = "2020-01-01T00:00:01+00:00"
    kept = dict(old)
    kept.update(run_id="kept", keep=True)
    store = RunStore(tmp_path)
    store.save(old)
    store.save(kept)
    assert store.cleanup(7) == 1
    assert not (tmp_path / "old.json").exists()
    assert (tmp_path / "kept.json").exists()


def test_examples_load_and_render():
    store = RunStore("examples/runs")
    records = store.list_records()
    assert len(records) == 2
    html = render_report(records)
    assert "Evening color" in html
    assert "Forecast" in html
    sample = Path("examples/runs/sample-report.html")
    assert sample.exists()
