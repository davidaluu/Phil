# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
"""Drives the real Device class against a fake ``adb`` executable (plain-ADB backend)."""

import stat
import sys

import pytest

from phil.adb import Device, DeviceError, list_devices
from phil.screen import ScreenTracker
from tests.helpers import sample_screen

FAKE_ADB = '''#!{python}
import os, sys
args = sys.argv[1:]
log = os.environ["FAKE_ADB_LOG"]
with open(log, "a") as handle:
    handle.write(" ".join(args) + "\\n")
if args[:1] == ["devices"]:
    print("* daemon started successfully")
    print("List of devices attached")
    for line in os.environ.get("FAKE_ADB_DEVICES", "ABC123\\tdevice").split(";"):
        print(line)
    sys.exit(0)
if "exec-out" in args:
    print(open(os.environ["FAKE_ADB_XML"]).read())
    print("UI hierchary dumped to: /dev/tty")
    sys.exit(0)
if any(a.startswith("monkey") for a in args):
    print("** No activities found to run, monkey aborted." if "missing" in " ".join(args) else "Events injected: 1")
sys.exit(0)
'''


@pytest.fixture
def fake_adb(tmp_path, monkeypatch):
    script = tmp_path / "adb"
    script.write_text(FAKE_ADB.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    xml = tmp_path / "screen.xml"
    xml.write_text(sample_screen())
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    monkeypatch.setenv("FAKE_ADB_XML", str(xml))
    return str(script), log


def connected(adb_path):
    device = Device(adb_path=adb_path, prefer_uiautomator2=False)
    device.connect()
    return device


def test_list_devices_skips_daemon_noise(fake_adb):
    assert list_devices(fake_adb[0]) == [("ABC123", "device")]


def test_connect_selects_only_phone_and_reports_backend(fake_adb):
    device = connected(fake_adb[0])
    assert device.serial == "ABC123" and device.backend == "adb"


def test_dump_ui_extracts_clean_xml_that_the_tracker_can_read(fake_adb):
    xml = connected(fake_adb[0]).dump_ui()
    assert xml.startswith("<?xml") and xml.endswith("</hierarchy>")
    assert ScreenTracker().update(xml).element_count == 5


def test_gestures_become_adb_input_commands(fake_adb):
    adb_path, log = fake_adb
    device = connected(adb_path)
    device.tap(10, 20)
    device.swipe(1, 2, 3, 4, 250)
    device.long_press(5, 6, 700)
    device.press("back")
    device.statusbar("expand-settings")
    calls = log.read_text()
    assert "-s ABC123 shell input tap 10 20" in calls
    assert "shell input swipe 1 2 3 4 250" in calls
    assert "shell input swipe 5 6 5 6 700" in calls
    assert "shell input keyevent 4" in calls
    assert "shell cmd statusbar expand-settings" in calls


def test_type_text_escapes_spaces_and_rejects_unicode(fake_adb):
    adb_path, log = fake_adb
    device = connected(adb_path)
    device.type_text("hello world", clear=False)
    assert "input text hello%sworld" in log.read_text()
    device.type_text("a;b $(x)", clear=False)  # shell metacharacters must stay quoted
    assert "input text 'a;b%s$(x)'" in log.read_text()
    with pytest.raises(DeviceError):
        device.type_text("h\u00e9llo")
    with pytest.raises(DeviceError):
        device.type_text("line1\nline2")


def test_launch_app_validates_package_names(fake_adb):
    device = connected(fake_adb[0])
    device.launch_app("com.android.settings")
    for bad in ("settings", "com.x; rm -rf /", "com.x && reboot", ""):
        with pytest.raises(DeviceError):
            device.launch_app(bad)
    with pytest.raises(DeviceError):
        device.launch_app("com.missing.app")


def test_unsupported_key_and_command_are_rejected(fake_adb):
    device = connected(fake_adb[0])
    with pytest.raises(DeviceError):
        device.press("power")
    with pytest.raises(DeviceError):
        device.statusbar("shutdown")


def test_device_selection_errors(fake_adb, monkeypatch):
    adb_path, _ = fake_adb
    monkeypatch.setenv("FAKE_ADB_DEVICES", "AAA\tdevice;BBB\tdevice")
    with pytest.raises(DeviceError, match="Several phones"):
        Device(adb_path=adb_path, prefer_uiautomator2=False).connect()
    Device("BBB", adb_path=adb_path, prefer_uiautomator2=False).connect()
    monkeypatch.setenv("FAKE_ADB_DEVICES", "AAA\tunauthorized")
    with pytest.raises(DeviceError, match="unauthorized"):
        Device(adb_path=adb_path, prefer_uiautomator2=False).connect()
    monkeypatch.setenv("FAKE_ADB_DEVICES", "")
    with pytest.raises(DeviceError, match="No phone detected"):
        Device(adb_path=adb_path, prefer_uiautomator2=False).connect()


def test_missing_adb_binary_has_helpful_message():
    with pytest.raises(DeviceError, match="platform-tools"):
        list_devices("/definitely/not/adb")
