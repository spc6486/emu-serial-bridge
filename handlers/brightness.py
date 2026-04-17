"""Brightness handler — sysfs PWM and auto-dim config.

Shares PWM hardware with the brightness-control tray app (last write wins).
Shares settings with brightness-control via ~/.config/brightness-control/settings.json.

Commands:
    BRIGHT <10-100>   Set brightness percentage
    BRI? / BRIGHT?    Query current brightness
    AUTO <0|1>        Set idle-dim enabled/disabled
    AUTO?             Query idle-dim state
"""

import json
import os
from pathlib import Path

NAME = "Brightness"
DESCRIPTION = "sysfs PWM on GPIO12, shared with brightness-control tray"

CFGDIR = Path.home() / ".config" / "brightness-control"
CFG = CFGDIR / "settings.json"


def _find_pwm_channel():
    base = Path("/sys/class/pwm")
    for chip in sorted(base.glob("pwmchip*")):
        candidate = chip / "pwm0"
        if (candidate / "duty_cycle").exists():
            return candidate
    return None


def _get_period():
    chan = _find_pwm_channel()
    if not chan:
        return 40000
    try:
        return int((chan / "period").read_text().strip())
    except (OSError, ValueError):
        return 40000


def _get_brightness():
    chan = _find_pwm_channel()
    if not chan:
        return None
    try:
        duty = int((chan / "duty_cycle").read_text().strip())
        period = _get_period()
        return max(0, min(100, round(duty * 100 / period)))
    except (OSError, ValueError):
        return None


def _set_brightness(pct):
    pct = max(0, min(100, int(pct)))
    chan = _find_pwm_channel()
    if not chan:
        return False
    period = _get_period()
    duty = period * pct // 100
    try:
        (chan / "duty_cycle").write_text(str(duty))
        return True
    except (OSError, PermissionError):
        return False


def _load_cfg():
    try:
        CFGDIR.mkdir(parents=True, exist_ok=True)
        with open(CFG) as f:
            return json.load(f)
    except Exception:
        return {"brightness": 100, "auto_dim_enabled": False}


def _save_cfg(d):
    try:
        CFGDIR.mkdir(parents=True, exist_ok=True)
        tmp = str(CFG) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, str(CFG))
    except Exception:
        pass


def _cmd_bright(args, write):
    parts = args.split()
    if not parts:
        write("ERR BRIGHT MISSING")
        return
    try:
        val = int(parts[0])
    except ValueError:
        write("ERR BRIGHT NAN")
        return
    val = max(0, min(100, val))
    if _set_brightness(val):
        cfg = _load_cfg()
        cfg["brightness"] = val
        _save_cfg(cfg)
        write(f"OK BRIGHT {val}")
    else:
        write("ERR BRIGHT FAIL")


def _cmd_bri_query(args, write):
    val = _get_brightness()
    if val is not None:
        write(f"BRIGHT {val}")
    else:
        write("ERR BRIGHT NODATA")


def _cmd_auto_set(args, write):
    parts = args.split()
    if not parts or parts[0] not in ("0", "1"):
        write("ERR AUTO USAGE")
        return
    enabled = parts[0] == "1"
    cfg = _load_cfg()
    cfg["auto_dim_enabled"] = enabled
    _save_cfg(cfg)
    write(f"OK AUTO {parts[0]}")


def _cmd_auto_query(args, write):
    cfg = _load_cfg()
    val = 1 if cfg.get("auto_dim_enabled", False) else 0
    write(f"AUTO {val}")


COMMANDS = {
    "BRIGHT": _cmd_bright,
    "BRI?": _cmd_bri_query,
    "BRIGHT?": _cmd_bri_query,
    "AUTO": _cmd_auto_set,
    "AUTO?": _cmd_auto_query,
}
