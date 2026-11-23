# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
"""Plain-English HTML reports generated entirely from local run records."""

from __future__ import annotations

import html
import json
import logging
import tempfile
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from phil.runs import RunStore, parse_timestamp, timestamp_now

logger = logging.getLogger(__name__)

FOOTER_TEXT = "Copyright © 2026 David Aluu"

FRAME_NAMES = {
    1: "Last run",
    2: "Last 10 runs",
    3: "Last hour",
    4: "Last 12 hours",
    5: "Last 24 hours",
    6: "Last 7 days",
}


def utc_offset(value: str) -> str:
    parsed = parse_timestamp(value)
    offset = parsed.strftime("%z")
    if not offset:
        return "UTC"
    sign = "+" if offset[0] == "+" else "-"
    pair = offset.lstrip("+-")
    return f"UTC{sign}{pair[:2]}:{pair[2:]}"


def format_date(value: str) -> str:
    parsed = parse_timestamp(value)
    return parsed.strftime("%A, %d %B %Y")


def format_time(value: str, time_format: str = "24") -> str:
    parsed = parse_timestamp(value)
    if time_format == "12":
        clock = parsed.strftime("%I:%M:%S %p")
    else:
        clock = parsed.strftime("%H:%M:%S")
    return f"{clock} · {utc_offset(value)}"


def select_frame(records: Iterable[dict[str, Any]], choice: int, now: datetime | None = None) -> list[dict[str, Any]]:
    ordered = sorted(records, key=lambda record: parse_timestamp(record["start_time"]), reverse=True)
    if choice == 1:
        return ordered[:1]
    if choice == 2:
        return ordered[:10]
    if choice not in {3, 4, 5, 6}:
        raise ValueError("Choose a time frame from 1 to 6.")
    hours = {3: 1, 4: 12, 5: 24, 6: 24 * 7}[choice]
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    cutoff = current.astimezone(timezone.utc) - timedelta(hours=hours)
    return [
        record for record in ordered
        if parse_timestamp(record["start_time"]).astimezone(timezone.utc) >= cutoff
        and parse_timestamp(record["start_time"]).astimezone(timezone.utc) <= current.astimezone(timezone.utc)
    ]


def plain_step_sentence(step: dict[str, Any]) -> str:
    action = step.get("action") or {}
    name = action.get("name") or action.get("action") or "action"
    result = step.get("result") or {}
    guards = step.get("guards") or {}
    target = action.get("target")
    target_text = f" '{target}'" if target else ""
    if guards.get("duplicate_tap_skipped"):
        return f"Skipped a duplicate tap on{target_text}. The screen had not changed."
    if guards.get("risky_declined"):
        return f"Phil paused for a risky action on{target_text}, but you declined it."
    if guards.get("risky_confirmed"):
        return f"Phil asked before the risky action on{target_text}, and you allowed it."
    if name == "click" and result.get("ok"):
        detail = result.get("error") or ""
        if step.get("screen_after", {}).get("changed") is False:
            return f"Tapped{target_text}. The screen looked the same afterwards, so Phil treated the tap as done and moved on."
        return f"Tapped{target_text}. The tap completed."
    if name == "type" and result.get("ok"):
        return f"Typed text into{target_text}. The type action completed."
    if name == "wait" and result.get("ok"):
        return "Phil waited for the phone to finish changing."
    if name == "stuck":
        return f"Phil stopped because the model said it was stuck: {action.get('reason') or 'no reason was recorded'}."
    if not result.get("ok"):
        error = result.get("error") or "The action failed."
        if "not found" in str(error).lower():
            return f"Tried to use{target_text}, but could not find it on screen."
        return f"Tried to use{target_text}, but the action failed: {error}"
    return f"Ran {name}{target_text}. {result.get('detail') or 'The action completed'}"


def fallback_summary(record: dict[str, Any]) -> str:
    status = record.get("status")
    total = record.get("total_steps", 0)
    if status == "completed":
        return f"Phil completed the task in {total} steps."
    why = record.get("why_stopped") or {}
    return why.get("sentence") or f"Phil stopped with status {status}, after {total} steps."


def _why_stopped(record: dict[str, Any]) -> dict[str, Any]:
    status = record.get("status")
    message = str(record.get("status_message") or "")
    last_action = str(record.get("last_action") or "No action was attempted.")
    if status == "completed":
        return {}
    if status == "stuck":
        if "Repeated '" in message:
            return {"sentence": f"The repeat guard stopped Phil: {message}", "last_action": last_action}
        return {"sentence": f"The model said Phil was stuck: {message}", "last_action": last_action}
    if status == "max_steps":
        return {"sentence": f"The step limit was reached: {message}", "last_action": last_action}
    if status == "aborted":
        if "risky" in message.lower():
            return {"sentence": f"You declined a risky action: {last_action}.", "last_action": last_action}
        if "interrupt" in message.lower():
            return {"sentence": "The user interrupted the run.", "last_action": last_action}
        return {"sentence": message, "last_action": last_action}
    if "Could not read the screen" in message or "screen" in message.lower() and "read" in message.lower():
        return {"sentence": f"Phil could not read the screen: {message}", "last_action": last_action}
    if "model" in message.lower() or "API" in message or "key" in message.lower() or "model" in message.lower():
        return {"sentence": f"A model or API problem stopped the run: {message}", "last_action": last_action}
    if "action failed" in message.lower() or "element not found" in message.lower():
        return {"sentence": f"Too many actions failed in a row: {message}", "last_action": last_action}
    return {"sentence": message or "Phil stopped before the task was completed.", "last_action": last_action}


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _elements(record_step: dict[str, Any]) -> list[dict[str, Any]]:
    screen = record_step.get("screen") or {}
    elements = screen.get("elements")
    return elements if isinstance(elements, list) else []


def _wireframe(step: dict[str, Any]) -> str:
    elements = _elements(step)
    screen = step.get("screen") or {}
    action = step.get("action") or {}
    target = str(action.get("target") or "")
    dimensions = screen.get("dimensions") or {}
    width = screen.get("width") or screen.get("screen_width") or dimensions.get("width")
    height = screen.get("height") or screen.get("screen_height") or dimensions.get("height")
    if not (isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0):
        width, height = 1080, 2400
    valid: list[tuple[int, dict[str, Any], list[int], str]] = []
    for element in elements:
        bounds = element.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            continue
        left, top, right, bottom = (int(value) for value in bounds)
        if right <= 0 or bottom <= 0 or left >= width or top >= height:
            continue
        left, top = max(0, left), max(0, top)
        right, bottom = min(int(width), right), min(int(height), bottom)
        area = (right - left) * (bottom - top)
        if area < (width * height * 0.00015):
            continue
        label = str(element.get("text") or element.get("description") or "")
        valid.append((area, element, [left, top, right, bottom], label))
    valid.sort(key=lambda item: item[0], reverse=True)

    def target_matches(key: str) -> bool:
        return bool(target) and (key == target or key.removeprefix("id:") == target.removeprefix("id:"))

    font_size = max(24, int(width * 0.035))
    svg_elements = []
    tap = ""
    for _area, element, bounds, label in valid:
        left, top, right, bottom = bounds
        key = str(element.get("key") or "")
        kind = str(element.get("type") or element.get("class") or element.get("class_name") or "").lower()
        traits = set(element.get("traits") or [])
        if "image" in kind or "image" in key.lower():
            style = "image"
        elif "edit" in kind or "input" in kind or ("focusable" in traits and "clickable" not in traits):
            style = "input"
        elif "clickable" in traits or "button" in kind:
            style = "tap"
        else:
            style = "text"
        highlighted = target_matches(key)
        title = _e(f"{label or '(no label)'} · {key or 'no element id'}")
        width_px, height_px = right - left, bottom - top
        # Estimate a readable label length in screen coordinates and truncate it.
        max_chars = max(0, int((width_px - font_size * 0.6) / (font_size * 0.58)))
        shown = label
        if max_chars == 0:
            shown = ""
        elif len(shown) > max_chars:
            shown = shown[: max(1, max_chars - 1)].rstrip() + "…"
        css_class = f"element-{style}" + (" element-target" if highlighted else "")
        svg_elements.append(
            f'<g class="{css_class}" tabindex="0" role="img" aria-label="{title}">'
            f'<title>{title}</title><rect x="{left}" y="{top}" width="{width_px}" height="{height_px}" rx="{font_size * 0.22}"/>'
            + (f'<text x="{left + font_size * 0.35}" y="{top + font_size * 1.15}" style="font-size:{font_size}px">{_e(shown)}</text>' if shown else "")
            + "</g>"
        )
        if highlighted and not tap and action.get("action", action.get("name")) in {"click", "long_click", "type", "focus"}:
            marker_class = "tap-marker failed" if not (step.get("result") or {}).get("ok") else "tap-marker"
            tap = (
                f'<g class="{marker_class}" aria-label="Phil tapped here{"; action failed" if marker_class.endswith("failed") else ""}">'
                f'<title>Phil tapped here{"; action failed" if marker_class.endswith("failed") else ""}</title>'
                f'<circle cx="{(left + right) // 2}" cy="{(top + bottom) // 2}" r="{font_size * 0.55}"/>'
                f'<circle class="marker-core" cx="{(left + right) // 2}" cy="{(top + bottom) // 2}" r="{font_size * 0.15}"/>'
                "</g>"
            )
    viewbox = f"-24 -24 {int(width) + 48} {int(height) + 48}"
    svg = (
        f'<svg class="phone-svg" viewBox="{viewbox}" role="img" aria-label="Phone screen wireframe">'
        f'<rect class="phone-frame" x="-18" y="-18" width="{int(width) + 36}" height="{int(height) + 36}" rx="{width * 0.06}"/>'
        f'<rect class="phone-screen" x="0" y="0" width="{int(width)}" height="{int(height)}" rx="{width * 0.045}"/>'
        + "".join(svg_elements) + tap + "</svg>"
    )
    failed_word = '<span class="marker-failed-word">Action failed</span>' if not (step.get("result") or {}).get("ok") else ""
    return (
        '<div class="wireframe-wrap"><div class="wireframe">'
        + svg
        + f'<div class="wire-legend"><span><i class="legend-tap"></i>Tap target</span><span><i class="legend-input"></i>Input</span><span><i class="legend-image"></i>Image</span><span><i class="legend-marker"></i>Phil tapped</span>{failed_word}</div>'
        + '</div><button class="enlarge-button" type="button" data-enlarge>Enlarge</button></div>'
    )


def render_report(records: list[dict[str, Any]], *, generated: str | None = None, time_format: str = "24", frame_name: str = "Selected runs") -> str:
    generated = generated or timestamp_now()
    ordered = sorted(records, key=lambda record: parse_timestamp(record["start_time"]))
    completed = sum(record.get("status") == "completed" for record in records)
    not_completed = len(records) - completed
    total_steps = sum(record.get("total_steps", 0) for record in records)
    total_seconds = sum(record.get("total_seconds", 0) for record in records)
    total_tokens = sum(record.get("total_tokens", 0) for record in records)
    start = ordered[0]["start_time"] if ordered else generated
    end = ordered[-1]["end_time"] or generated if ordered else generated

    table_rows = []
    detail_sections = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in sorted(records, key=lambda record: parse_timestamp(record["start_time"]), reverse=True):
        day = format_date(record["start_time"])
        grouped.setdefault(day, []).append(record)

    for record in records:
        run_id = _e(record["run_id"])
        status = _e(record["status"])
        failed = record["status"] != "completed"
        table_rows.append(
            f'<tr class="{"failed-row" if failed else ""}" data-status="{status}">'
            f'<td><a href="#{run_id}">{_e(format_time(record["start_time"], time_format))}</a></td>'
            f'<td>{_e(record["task"])}</td><td><strong>{"Completed" if not failed else status.replace("_", " ").title()}</strong></td>'
            f'<td>{record["total_steps"]}</td><td>{record["total_seconds"]:.1f} s</td>'
            f'<td>{record["total_tokens"]}</td></tr>'
        )

    for day, day_records in grouped.items():
        detail_sections.append(f'<h2 class="day-heading">{_e(day)}</h2>')
        for record in day_records:
            run_id = _e(record["run_id"])
            status = record["status"]
            why = record.get("why_stopped") or _why_stopped(record)
            summary = record.get("summary")
            summary_text = summary.get("text") if isinstance(summary, dict) else None
            if not summary_text:
                summary_text = fallback_summary(record)
            summary_label = "AI-written summary" if isinstance(summary, dict) and summary.get("ai_written") else "Plain-language summary"
            detail_sections.append(
                f'<section class="run {"" if status == "completed" else "not-completed"}" id="{run_id}">'
                f'<div class="run-heading"><h3>{_e(record["task"])}</h3>'
                f'<span class="status-chip {"status-ok" if status == "completed" else "status-bad"}">'
                f'{"✓ Completed" if status == "completed" else "• " + _e(status.replace("_", " ").title())}</span></div>'
                f'<div class="run-meta"><span>Started {_e(format_time(record["start_time"], time_format))}</span>'
                f'<span>Ended {_e(format_time(record["end_time"] or record["start_time"], time_format))}</span>'
                f'<span>{record["total_seconds"]:.1f} seconds</span></div>'
                f'<p><strong>Final status:</strong> {_e(status.replace("_", " ").title())}</p>'
            )
            if status != "completed":
                detail_sections.append(
                    '<div class="why"><strong>Why it stopped</strong><br>'
                    f'{_e(why.get("sentence") or fallback_summary(record))}'
                    f'<br><strong>Last action:</strong> {_e(record.get("last_action") or "No action was attempted.")}</div>'
                )
            detail_sections.append(
                f'<p class="summary"><strong>{summary_label}</strong><br>{_e(summary_text)}</p>'
                '<div class="screen-controls"><button type="button" data-expand-run>Expand all screens</button>'
                '<button type="button" data-collapse-run>Collapse all screens</button></div>'
                '<ol class="timeline">'
            )
            previous_time = None
            for step in record.get("steps", []):
                number = step.get("number", 1)
                step_time = step.get("time")
                elapsed_gap = ""
                if previous_time is not None:
                    try:
                        gap = (parse_timestamp(step_time) - parse_timestamp(previous_time)).total_seconds()
                        elapsed_gap = f'<span class="gap"> {gap:.1f} s between steps</span>'
                    except (TypeError, ValueError):
                        pass
                previous_time = step_time
                result = step.get("result") or {}
                outcome = "OK" if result.get("ok") else "FAILED"
                guards = step.get("guards") or {}
                guard_items = [name.replace("_", " ") for name, fired in guards.items() if fired]
                guards_text = " · ".join(guard_items) if guard_items else "No special guards"
                action = step.get("action") or {}
                action_text = json.dumps(action, ensure_ascii=False)
                label = "went wrong, then recovered" if step.get("recovered") else plain_step_sentence(step)
                step_class = "bad" if outcome == "FAILED" else "recovered" if step.get("recovered") else ""
                step_status = (
                    "! Failed" if outcome == "FAILED"
                    else "↻ Recovered" if step.get("recovered")
                    else "✓ OK"
                )
                status_class = "step-bad" if outcome == "FAILED" else "step-recovered" if step.get("recovered") else "step-ok"
                detail_sections.append(
                    f'<li class="step {step_class}">'
                    f'<div class="step-time"><strong>{_e(format_time(step_time, time_format))}</strong>'
                    f'<span>took {float(step.get("duration_seconds", 0)):.1f} s</span>{elapsed_gap}</div>'
                    f'<div class="step-content"><div class="step-status {status_class}">'
                    f'{step_status}</div>'
                    f'<p class="step-sentence">{_e(label)}</p><div class="step-facts">'
                    f'<span><strong>Action:</strong> {_e(action_text)}</span>'
                    f'<span><strong>Result:</strong> {outcome}</span><span><strong>Guards:</strong> {_e(guards_text)}</span></div>'
                    '<div class="screen-view-grid">'
                    '<details class="screen-panel elements-panel"><summary><span class="arrow">▶</span> Show screen elements</summary>'
                    f'<div class="elements-box"><pre>{_e(json.dumps(_elements(step), ensure_ascii=False, indent=2))}</pre></div></details>'
                    '<details class="screen-panel wire-panel"><summary><span class="arrow">▶</span> Show wireframe</summary>'
                    f'{_wireframe(step)}</details></div></div>'
                    '</li>'
                )
            detail_sections.append("</ol></section>")

    rows = "".join(table_rows)
    sections = "".join(detail_sections)
    range_from = format_date(start) + " · " + format_time(start, time_format)
    range_to = format_date(end) + " · " + format_time(end, time_format)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phil run report</title>
<style>
:root {{ color-scheme:light dark; --bg:#f4f6f9; --surface:#fff; --text:#202632; --muted:#687386; --line:#dce2ea; --accent:#1769aa; --accent-soft:#eaf3fb; --good:#25734a; --good-soft:#e9f5ee; --bad:#a83c3c; --bad-soft:#fbefed; --wire-bg:#f3f6f8; --wire-frame:#455568; --wire-tap:#d5e8f7; --wire-input:#fff; --wire-image:#e9eef3 }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#10151d; --surface:#171f2a; --text:#e9eef5; --muted:#aab6c5; --line:#303c4b; --accent:#72b8f0; --accent-soft:#172b3b; --good:#77c99a; --good-soft:#1d3428; --bad:#f0978a; --bad-soft:#3c2829; --wire-bg:#e9eef2; --wire-frame:#536477; --wire-tap:#d2e7f7; --wire-input:#fff; --wire-image:#e5ebf0 }} }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1160px; margin:auto; padding:32px clamp(16px,4vw,48px) 48px }}
h1,h2,h3 {{ line-height:1.2; letter-spacing:-.025em }} h1 {{ font-size:clamp(1.8rem,4vw,2.4rem); margin:0 0 8px }} h2 {{ font-size:1.35rem; margin-top:32px }} h3 {{ font-size:1.2rem; margin:0 }}
.quiet,.run-meta {{ color:var(--muted) }} .privacy {{ background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:8px; padding:12px 16px; margin:20px 0 }}
.overview {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:12px; margin:22px 0 }}
.stat-card {{ min-height:86px; display:flex; flex-direction:column; justify-content:center; gap:2px; padding:14px 16px; border:1px solid var(--line); border-radius:10px; background:var(--surface); box-shadow:0 1px 2px #1723350b }}
.stat-card strong {{ font-size:1.4rem; letter-spacing:-.03em; font-variant-numeric:tabular-nums }} .stat-card span {{ color:var(--muted); font-size:.84rem }}
.controls {{ display:flex; gap:8px; flex-wrap:wrap; margin:18px 0 }} button {{ color:var(--accent); background:var(--surface); border:1px solid var(--line); border-radius:7px; padding:8px 12px; font:inherit; font-size:.9rem; cursor:pointer }} button:hover {{ border-color:var(--accent); background:var(--accent-soft) }} button:focus-visible,summary:focus-visible,a:focus-visible {{ outline:3px solid var(--accent); outline-offset:3px }}
table {{ width:100%; border-collapse:collapse; margin:12px 0 28px; background:var(--surface); border:1px solid var(--line); border-radius:10px; overflow:hidden }} th,td {{ border-bottom:1px solid var(--line); padding:11px 12px; text-align:left; vertical-align:top }} th {{ color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.06em }} tr:last-child td {{ border-bottom:0 }} .failed-row {{ background:var(--bad-soft) }} a {{ color:var(--accent); text-decoration:none }} a:hover {{ text-decoration:underline }}
.day-heading {{ margin:34px 0 10px; color:var(--muted); font-size:.93rem; text-transform:uppercase; letter-spacing:.08em }} .run {{ border:1px solid var(--line); border-radius:12px; padding:22px; margin:12px 0 22px; background:var(--surface); box-shadow:0 2px 6px #17233508 }} .run-heading {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px }} .run-meta {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin:10px 0; font-size:.88rem; font-variant-numeric:tabular-nums }}
.status-chip,.step-status {{ display:inline-flex; align-items:center; gap:6px; border-radius:99px; padding:4px 10px; font-size:.8rem; font-weight:650; white-space:nowrap }} .status-ok,.step-ok {{ color:var(--good); background:var(--good-soft) }} .status-bad,.step-bad {{ color:var(--bad); background:var(--bad-soft) }} .step-recovered {{ color:var(--bad); background:var(--bad-soft) }}
.why {{ border:1px solid color-mix(in srgb,var(--bad) 32%,var(--line)); border-left:4px solid var(--bad); border-radius:9px; background:var(--bad-soft); padding:14px 16px; margin:16px 0; color:var(--text) }} .why strong:first-child {{ display:inline-block; margin-bottom:5px; font-size:1rem }} .summary {{ border-radius:8px; background:var(--bg); border:1px solid var(--line); padding:13px 16px }}
.timeline {{ list-style:none; padding:0 0 0 20px; margin:20px 0 0 10px; border-left:2px solid var(--line) }} .step {{ position:relative; display:grid; grid-template-columns:190px minmax(0,1fr); gap:18px; padding:4px 0 22px; margin:0; break-inside:avoid }}
.step::before {{ content:""; position:absolute; left:-27px; top:7px; width:12px; height:12px; border:3px solid var(--surface); border-radius:50%; background:var(--good); box-shadow:0 0 0 1px var(--good) }} .step.bad::before,.step.recovered::before {{ background:var(--bad); box-shadow:0 0 0 1px var(--bad) }} .step.bad .step-sentence,.step.recovered .step-sentence {{ color:var(--bad) }}
.step-time {{ display:flex; flex-direction:column; align-items:flex-start; gap:3px; color:var(--muted); font-size:.84rem; font-variant-numeric:tabular-nums }} .step-time strong {{ color:var(--text) }} .gap {{ color:var(--muted); font-size:.78rem }} .step-content {{ min-width:0 }} .step-status {{ margin:0 0 6px }} .step-sentence {{ margin:0 0 10px; font-size:1.03rem; font-weight:570 }} .step-facts {{ display:grid; gap:4px; color:var(--muted); font-size:.83rem; overflow-wrap:anywhere }} .step-facts strong {{ color:var(--text) }}
.screen-view-grid {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); align-items:start; gap:12px; margin-top:14px }} details {{ min-width:0; border-top:1px solid var(--line); padding-top:9px }} summary {{ list-style:none; color:var(--accent); cursor:pointer; font-size:.88rem; font-weight:580 }} summary::-webkit-details-marker {{ display:none }} summary .arrow {{ display:inline-block; width:17px }} details[open] summary .arrow {{ transform:rotate(90deg) }}
.elements-box {{ max-height:320px; overflow:auto; border:1px solid var(--line); border-radius:7px; background:var(--bg); padding:10px; margin-top:8px }} pre {{ margin:0; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace }}
.wire-panel[open] {{ position:sticky; top:12px; align-self:start }} .wireframe-wrap {{ position:relative; margin-top:8px; border:1px solid var(--line); border-radius:9px; padding:12px; background:var(--bg); text-align:center }} .wireframe {{ display:flex; justify-content:center; align-items:center; height:min(75vh,680px); width:100%; overflow:visible }} .phone-svg {{ display:block; width:auto; height:100%; max-width:100%; object-fit:contain; overflow:visible }}
.phone-frame {{ fill:var(--wire-frame) }} .phone-screen {{ fill:var(--wire-bg); stroke:#c5d0dc; stroke-width:2 }} .element-tap rect {{ fill:var(--wire-tap); stroke:var(--accent); stroke-width:4 }} .element-input rect {{ fill:var(--wire-input); stroke:#718295; stroke-width:3 }} .element-input text {{ text-decoration:underline }} .element-text rect {{ fill:transparent; stroke:transparent }} .element-image rect {{ fill:var(--wire-image); stroke:#aab7c5; stroke-width:3; stroke-dasharray:10 8 }} .element-target rect {{ stroke:var(--accent); stroke-width:9; fill:var(--accent-soft) }} .phone-svg text {{ fill:#263546; font:600 1em system-ui,sans-serif; pointer-events:none }} .phone-svg g:focus {{ outline:none }} .phone-svg g:focus rect {{ stroke:var(--accent); stroke-width:10 }} .tap-marker circle {{ fill:#fff; stroke:var(--accent); stroke-width:8 }} .tap-marker .marker-core {{ fill:var(--accent); stroke:#fff; stroke-width:2 }} .tap-marker.failed circle {{ fill:#fff; stroke:#aa3535; stroke-width:9 }} .tap-marker.failed .marker-core {{ fill:#aa3535; stroke:#fff }} .wire-legend {{ display:flex; justify-content:center; flex-wrap:wrap; gap:10px; color:var(--muted); font-size:.72rem; margin-top:7px }} .wire-legend span {{ display:inline-flex; align-items:center; gap:4px }} .wire-legend i {{ display:inline-block; width:11px; height:11px; border:1px solid var(--accent); border-radius:3px; background:var(--wire-tap) }} .wire-legend .legend-input {{ background:var(--wire-input); border-radius:0 0 2px 2px; border-width:1px 1px 2px }} .wire-legend .legend-image {{ background:var(--wire-image); border-style:dashed }} .wire-legend .legend-marker {{ width:10px; height:10px; border:2px solid var(--accent); border-radius:50%; background:#fff }} .marker-failed-word {{ color:var(--bad); font-weight:700 }}
.enlarge-button {{ margin-top:8px; font-size:.8rem }} dialog {{ width:min(92vw,720px); max-width:92vw; max-height:88vh; padding:14px; border:1px solid var(--line); border-radius:12px; color:var(--text); background:var(--surface) }} dialog::backdrop {{ background:#101820aa }} .dialog-close {{ display:block; margin:0 0 10px auto }} .dialog-wireframe {{ height:min(75vh,720px); border:0; background:transparent }}
footer {{ border-top:1px solid var(--line); margin-top:32px; padding:18px 24px; color:var(--muted); text-align:center }}
@media(max-width:850px) {{ main {{ padding:20px 16px 36px }} .overview {{ grid-template-columns:repeat(3,minmax(0,1fr)) }} .screen-view-grid {{ grid-template-columns:minmax(0,1fr) }} .wire-panel[open] {{ position:static }} .wireframe {{ height:min(75vh,620px) }} .phone-svg {{ margin:auto }} }}
@media(max-width:560px) {{ .overview {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px }} .stat-card {{ min-height:74px; padding:10px 12px }} .run {{ padding:16px 12px }} .run-heading {{ flex-direction:column; gap:8px }} table {{ display:block; overflow-x:auto }} .step {{ grid-template-columns:minmax(0,1fr); gap:8px }} .step-time {{ display:grid; grid-template-columns:1fr auto; align-items:center }} .step-time .gap {{ grid-column:1/-1 }} .wireframe {{ height:min(75vh,560px) }} }}
@media print {{ :root {{ color-scheme:light; --bg:#fff; --surface:#fff; --text:#000; --muted:#333; --line:#999; --accent:#000; --accent-soft:#fff; --good:#000; --good-soft:#fff; --bad:#000; --bad-soft:#fff }} body {{ background:#fff;color:#000 }} .controls,button {{ display:none!important }} .run {{ box-shadow:none }} .screen-view-grid {{ grid-template-columns:1fr 1fr }} details {{ display:block }} details:not([open]) > * {{ display:block }} details > summary {{ display:none }} .elements-box {{ max-height:none; overflow:visible }} .wireframe {{ height:75vh }} .step {{ break-inside:avoid; page-break-inside:avoid }} footer {{ position:fixed; bottom:0; left:0; right:0; background:#fff }} }}
</style>
</head>
<body>
<main>
<h1>Phil run report</h1>
<p class="quiet"><strong>Generated:</strong> {_e(format_time(generated, time_format))}</p>
<p><strong>Time range:</strong> {_e(range_from)} to {_e(range_to)}<br><span class="quiet">{_e(frame_name)}</span></p>
<p class="privacy">This report may contain text that was on your phone's screen. Check it before you share it.</p>
<div class="overview">
<div class="stat-card"><strong>{len(records)}</strong><span>Runs</span></div>
<div class="stat-card"><strong>{completed}</strong><span>Completed</span></div>
<div class="stat-card"><strong>{not_completed}</strong><span>Did not complete</span></div>
<div class="stat-card"><strong>{total_steps}</strong><span>Steps</span></div>
<div class="stat-card"><strong>{total_seconds:.1f} s</strong><span>Time</span></div>
<div class="stat-card"><strong>{total_tokens}</strong><span>Tokens</span></div>
</div>
<div class="controls">
<button type="button" id="saveCopy">Save a copy</button><button type="button" id="printPdf">Print / Save as PDF</button>
<button type="button" id="failedOnly">Show failed only</button><button type="button" id="allRuns">Show all runs</button>
</div>
<h2>Runs</h2>
<table><thead><tr><th>Start time</th><th>Task</th><th>Status</th><th>Steps</th><th>Time</th><th>Tokens</th></tr></thead><tbody>{rows}</tbody></table>
{sections}
</main>
<footer>{FOOTER_TEXT}</footer>
<dialog id="wireframeDialog" aria-label="Enlarged phone wireframe">
<button type="button" class="dialog-close">Close</button>
<div class="dialog-wireframe"></div>
</dialog>
<script>
document.querySelectorAll("details").forEach(function(d){{
  d.addEventListener("toggle",function(){{
    var summary=d.querySelector("summary");
    var label=d.classList.contains("wire-panel")?"wireframe":"screen elements";
    if(d.open){{
      summary.innerHTML='<span class="arrow">▼</span> Hide ' + label;
    }} else {{
      summary.innerHTML='<span class="arrow">▶</span> Show ' + label;
    }}
  }});
}});
document.querySelectorAll("[data-expand-run]").forEach(function(b){{b.addEventListener("click",function(){{
  b.closest(".run").querySelectorAll("details").forEach(function(d){{d.open=true}});
}});}});
document.querySelectorAll("[data-collapse-run]").forEach(function(b){{b.addEventListener("click",function(){{
  b.closest(".run").querySelectorAll("details").forEach(function(d){{d.open=false}});
}});}});
document.getElementById("failedOnly").addEventListener("click",function(){{
  document.querySelectorAll("tbody tr").forEach(function(r){{r.style.display=r.classList.contains("failed-row")?"table-row":"none";}});
}});
document.getElementById("allRuns").addEventListener("click",function(){{
  document.querySelectorAll("tbody tr").forEach(function(r){{r.style.display="table-row";}});
}});
document.getElementById("saveCopy").addEventListener("click",function(){{
  var blob=new Blob(["<!doctype html>\\n"+document.documentElement.outerHTML],{{type:"text/html"}});
  var a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="phil-report.html";a.click();URL.revokeObjectURL(a.href);
}});
document.getElementById("printPdf").addEventListener("click",function(){{window.print();}});
var wireDialog=document.getElementById("wireframeDialog");
document.querySelectorAll("[data-enlarge]").forEach(function(b){{b.addEventListener("click",function(){{
  var source=b.closest(".wireframe-wrap").querySelector(".wireframe");
  var copy=source.cloneNode(true);copy.classList.add("dialog-wireframe");
  wireDialog.querySelector(".dialog-wireframe").replaceWith(copy);
  wireDialog.showModal();
}});}});
wireDialog.querySelector(".dialog-close").addEventListener("click",function(){{wireDialog.close();}});
wireDialog.addEventListener("click",function(event){{if(event.target===wireDialog)wireDialog.close();}});
</script>
</body></html>"""


def write_report(records: list[dict[str, Any]], directory: str | Path = "reports", *, time_format: str = "24", frame_name: str = "Selected runs", open_browser: bool = True) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"phil-report-{timestamp_now().replace(':','').replace('+','plus')}.html"
    path = directory / name
    html_text = render_report(records, time_format=time_format, frame_name=frame_name)
    path.write_text(html_text, encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception as exc:
            logger.warning("Could not open report in the browser: %s", exc)
    return path
