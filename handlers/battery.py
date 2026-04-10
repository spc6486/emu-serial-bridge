"""Battery handler — reads battery-monitor tray app status file.

The battery-monitor tray app writes UPS data to a JSON file each poll cycle.
This handler reads that file rather than accessing the serial port directly.

Commands:
    BAT? / BAT    Query battery level and charging state
"""

import json
import os
import time

NAME = "Battery"
DESCRIPTION = "Reads battery-monitor-status.json"

_runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
STATUS_FILE = os.path.join(_runtime, "battery-monitor-status.json")

# How old the status file can be before we consider it stale (seconds)
MAX_AGE = 60


def _cmd_bat(args, write):
    try:
        with open(STATUS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        write("ERR BAT battery-monitor not running")
        return
    except (json.JSONDecodeError, OSError) as e:
        write(f"ERR BAT {e}")
        return

    ts = data.get("timestamp", 0)
    if time.time() - ts > MAX_AGE:
        write("ERR BAT stale")
        return

    pct = data.get("bat_percent")
    ac = data.get("ac_power")

    if pct is None:
        write("ERR BAT NODATA")
        return

    if ac is True:
        state = "CHG"
    elif ac is False:
        state = "DIS"
    else:
        state = "UNK"

    write(f"BAT {pct} {state}")


COMMANDS = {
    "BAT?": _cmd_bat,
    "BAT": _cmd_bat,
}
