"""Tests for the battery, brightness, and volume handlers.

None of these require a Raspberry Pi, sysfs PWM, socat, wpctl, or gpiozero:
hardware access is monkeypatched. The tests smoke-test the *protocol*
(argument parsing, clamping, response formatting, error codes) that runs on
the wire to the emulated Mac.

Note: the handlers' ``init()`` sets an internal ``_gpio_available`` flag,
which is ``False`` on non-Pi hosts after importing ``gpiozero`` fails — so
GPIO-backed code paths are exercised in their "unavailable" branch.
"""

import json
import subprocess
import time

import pytest

import battery
import brightness
import volume


# ── Battery ────────────────────────────────────────────────────────────

def _write_status(tmp_path, **overrides):
    data = {
        "timestamp": time.time(),
        "bat_percent": 81,
        "ac_power": True,
        "estimated_runtime_min": 332,
        "output_power_w": 9.1,
        "is_charging": True,
        "shutdown_request": 0,
    }
    data.update(overrides)
    path = tmp_path / "battery-monitor-status.json"
    path.write_text(json.dumps(data))
    return path


def test_bat_full_response(monkeypatch, tmp_path):
    monkeypatch.setattr(battery, "STATUS_FILE", str(_write_status(tmp_path)))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies == ["BAT 81 CHG 332 9.1 1 0"]


def test_bat_on_battery_discharging(monkeypatch, tmp_path):
    monkeypatch.setattr(battery, "STATUS_FILE",
                        str(_write_status(tmp_path, ac_power=False)))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies == ["BAT 81 DIS 332 9.1 1 0"]


def test_bat_unknown_ac_state(monkeypatch, tmp_path):
    monkeypatch.setattr(battery, "STATUS_FILE",
                        str(_write_status(tmp_path, ac_power=None)))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies[0].split()[2] == "UNK"


def test_bat_stale_file(monkeypatch, tmp_path):
    monkeypatch.setattr(battery, "STATUS_FILE",
                        str(_write_status(tmp_path, timestamp=time.time() - 999)))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies == ["ERR BAT stale"]


def test_bat_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(battery, "STATUS_FILE", str(tmp_path / "nope.json"))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies == ["ERR BAT battery-monitor not running"]


def test_bat_missing_percent_is_nodata(monkeypatch, tmp_path):
    monkeypatch.setattr(battery, "STATUS_FILE",
                        str(_write_status(tmp_path, bat_percent=None)))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies == ["ERR BAT NODATA"]


def test_bat_missing_extended_fields_default_to_minus_one(monkeypatch, tmp_path):
    monkeypatch.setattr(
        battery, "STATUS_FILE",
        str(_write_status(tmp_path, estimated_runtime_min=None,
                          output_power_w=None, is_charging=False)))
    replies = []
    battery._cmd_bat("", replies.append)
    assert replies == ["BAT 81 CHG -1 -1 0 0"]


# ── Brightness ─────────────────────────────────────────────────────────

@pytest.fixture
def pwm(monkeypatch, tmp_path):
    monkeypatch.setattr(brightness, "CFGDIR", tmp_path)
    monkeypatch.setattr(brightness, "CFG", tmp_path / "settings.json")
    chan = tmp_path / "pwm0"
    chan.mkdir()
    monkeypatch.setattr(brightness, "_find_pwm_channel", lambda: chan)
    monkeypatch.setattr(brightness, "_get_period", lambda: 40000)
    return chan


def test_bright_set_writes_duty(monkeypatch, pwm):
    replies = []
    brightness._cmd_bright("50", replies.append)
    assert replies == ["OK BRIGHT 50"]
    assert (pwm / "duty_cycle").read_text() == "20000"


def test_bright_set_clamps_above_100(monkeypatch, pwm):
    replies = []
    brightness._cmd_bright("150", replies.append)
    assert replies == ["OK BRIGHT 100"]
    assert (pwm / "duty_cycle").read_text() == "40000"


def test_bright_set_clamps_below_0(monkeypatch, pwm):
    replies = []
    brightness._cmd_bright("-5", replies.append)
    assert replies == ["OK BRIGHT 0"]
    assert (pwm / "duty_cycle").read_text() == "0"


def test_bright_set_nan(monkeypatch, pwm):
    replies = []
    brightness._cmd_bright("abc", replies.append)
    assert replies == ["ERR BRIGHT NAN"]


def test_bright_set_missing_arg(monkeypatch, pwm):
    replies = []
    brightness._cmd_bright("", replies.append)
    assert replies == ["ERR BRIGHT MISSING"]


def test_bright_query_roundtrip(monkeypatch, pwm):
    (pwm / "duty_cycle").write_text("20000")
    replies = []
    brightness._cmd_bri_query("", replies.append)
    assert replies == ["BRIGHT 50"]


def test_auto_set_and_query(monkeypatch, pwm):
    replies = []
    brightness._cmd_auto_set("1", replies.append)
    assert replies == ["OK AUTO 1"]
    q = []
    brightness._cmd_auto_query("", q.append)
    assert q == ["AUTO 1"]


# ── Volume ─────────────────────────────────────────────────────────────

def test_vol_query_parses_percent(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *a, **k: "Volume: 0.65 [MUTED]\n")
    replies = []
    volume._cmd_vol_query("", replies.append)
    assert replies == ["VOL 65"]


def test_vol_query_rounds_half_up(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *a, **k: "Volume: 0.999\n")
    replies = []
    volume._cmd_vol_query("", replies.append)
    assert replies == ["VOL 100"]


def test_mute_query(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *a, **k: "Volume: 0.65 [MUTED]\n")
    replies = []
    volume._cmd_mute_query("", replies.append)
    assert replies == ["MUTE 1"]


def test_vol_set_clamps_and_reports(monkeypatch):
    calls = {}
    class _R:  # fake CompletedProcess
        def __init__(self, *a, **k): self.returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.setdefault(
        "args", a[0]) or _R())
    replies = []
    volume._cmd_vol_set("150", replies.append)
    assert replies == ["OK VOL 100"]
    assert calls["args"][3] == "1.00"


def test_mute_set_usage_error(monkeypatch):
    replies = []
    volume._cmd_mute_set("maybe", replies.append)
    assert replies == ["ERR MUTE USAGE"]


def test_hp_unavailable_without_gpio(monkeypatch):
    monkeypatch.setattr(volume, "_gpio_available", False)
    replies = []
    volume._cmd_hp_set("1", replies.append)
    assert replies == ["ERR HP NOGPIO"]
    q = []
    volume._cmd_hp_query("", q.append)
    assert q == ["ERR HP NOGPIO"]