"""Battery handler — reads battery-monitor tray app status file.

The battery-monitor tray app writes UPS data to a JSON file each poll cycle.
This handler reads that file rather than accessing the serial port directly.

Commands:
    BAT? / BAT    Query battery status

Response:
    BAT <pct> <ac> <runtime_min> <watts> <charging> <shutdown>

    pct          0-100
    ac           CHG (plugged in) | DIS (on battery) | UNK
    runtime_min  estimated minutes remaining, -1 if unavailable
    watts        current output power draw, -1 if unavailable
    charging     1 if battery is actively charging, else 0
    shutdown     1 if UPS has requested shutdown, else 0

The first three fields (pct, ac) are unchanged from v1.0 for compatibility.
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

    # Extended fields (v1.2.0)
    runtime = data.get("estimated_runtime_min")
    runtime_str = str(int(runtime)) if isinstance(runtime, (int, float)) else "-1"

    watts = data.get("output_power_w")
    watts_str = f"{watts:.1f}" if isinstance(watts, (int, float)) else "-1"

    charging = 1 if data.get("is_charging") is True else 0

    shutdown_req = data.get("shutdown_request", 0)
    shutdown = 1 if shutdown_req else 0

    write(f"BAT {pct} {state} {runtime_str} {watts_str} {charging} {shutdown}")


COMMANDS = {
    "BAT?": _cmd_bat,
    "BAT": _cmd_bat,
}
