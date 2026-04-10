"""Volume handler — PipeWire volume/mute and GPIO24 headphone amp.

Uses wpctl (PipeWire/WirePlumber) for volume and mute control.
Uses GPIO24 for headphone amplifier switching.

Commands:
    VOL <0-100>    Set volume percentage
    VOL?           Query current volume
    MUTE <0|1>     Set mute (1=muted)
    MUTE?          Query mute state
    HP <0|1>       Set headphone amp (1=headphones active)
    HP?            Query headphone amp state
"""

import subprocess

NAME = "Volume"
DESCRIPTION = "wpctl for PipeWire, GPIO24 for headphone amp"

WPCTL = "/usr/bin/wpctl"
GPIO_HP = 24
_gpio_available = False


def init(config=None):
    global _gpio_available
    try:
        from gpiozero import OutputDevice
        _gpio_available = True
    except ImportError:
        _gpio_available = False


def _get_volume():
    try:
        out = subprocess.check_output(
            [WPCTL, "get-volume", "@DEFAULT_AUDIO_SINK@"],
            text=True, timeout=2
        ).strip()
        # Output: "Volume: 0.65" or "Volume: 0.65 [MUTED]"
        parts = out.split()
        if len(parts) >= 2:
            vol = float(parts[1])
            muted = "[MUTED]" in out
            return int(round(vol * 100)), muted
    except Exception:
        pass
    return None, None


def _set_volume(pct):
    pct = max(0, min(100, int(pct)))
    frac = pct / 100.0
    try:
        subprocess.run(
            [WPCTL, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{frac:.2f}"],
            timeout=2, check=True
        )
        return True
    except Exception:
        return False


def _set_mute(muted):
    val = "1" if muted else "0"
    try:
        subprocess.run(
            [WPCTL, "set-mute", "@DEFAULT_AUDIO_SINK@", val],
            timeout=2, check=True
        )
        return True
    except Exception:
        return False


def _get_hp():
    if not _gpio_available:
        return None
    try:
        from gpiozero import OutputDevice
        # Read GPIO24 state via sysfs
        gpio_path = f"/sys/class/gpio/gpio{GPIO_HP}/value"
        if os.path.exists(gpio_path):
            with open(gpio_path) as f:
                return f.read().strip() == "0"  # LOW = headphones active
    except Exception:
        pass
    return None


def _set_hp(active):
    if not _gpio_available:
        return False
    try:
        from gpiozero import OutputDevice
        gpio_path = f"/sys/class/gpio/gpio{GPIO_HP}/value"
        if os.path.exists(gpio_path):
            with open(gpio_path, "w") as f:
                f.write("0" if active else "1")  # LOW = headphones active
            return True
    except Exception:
        pass
    return False


def _cmd_vol_set(args, write):
    parts = args.split()
    if not parts:
        write("ERR VOL MISSING")
        return
    try:
        val = int(parts[0])
    except ValueError:
        write("ERR VOL NAN")
        return
    if _set_volume(val):
        write(f"OK VOL {max(0, min(100, val))}")
    else:
        write("ERR VOL FAIL")


def _cmd_vol_query(args, write):
    vol, _ = _get_volume()
    if vol is not None:
        write(f"VOL {vol}")
    else:
        write("ERR VOL NODATA")


def _cmd_mute_set(args, write):
    parts = args.split()
    if not parts or parts[0] not in ("0", "1"):
        write("ERR MUTE USAGE")
        return
    if _set_mute(parts[0] == "1"):
        write(f"OK MUTE {parts[0]}")
    else:
        write("ERR MUTE FAIL")


def _cmd_mute_query(args, write):
    _, muted = _get_volume()
    if muted is not None:
        write(f"MUTE {1 if muted else 0}")
    else:
        write("ERR MUTE NODATA")


def _cmd_hp_set(args, write):
    if not _gpio_available:
        write("ERR HP NOGPIO")
        return
    parts = args.split()
    if not parts or parts[0] not in ("0", "1"):
        write("ERR HP USAGE")
        return
    if _set_hp(parts[0] == "1"):
        write(f"OK HP {parts[0]}")
    else:
        write("ERR HP FAIL")


def _cmd_hp_query(args, write):
    if not _gpio_available:
        write("ERR HP NOGPIO")
        return
    state = _get_hp()
    if state is not None:
        write(f"HP {1 if state else 0}")
    else:
        write("ERR HP NODATA")


import os

COMMANDS = {
    "VOL": _cmd_vol_set,
    "VOL?": _cmd_vol_query,
    "MUTE": _cmd_mute_set,
    "MUTE?": _cmd_mute_query,
    "HP": _cmd_hp_set,
    "HP?": _cmd_hp_query,
}
