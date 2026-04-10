# Emu Serial Bridge

Serial bridge between classic Mac OS emulators (SheepShaver, BasiliskII) and
Raspberry Pi hardware. Creates a virtual serial link and translates text
commands from the emulated Mac into real hardware actions.

## Requirements

- Raspberry Pi (tested on Pi 5, compatible with Pi 4/3/Zero 2 W)
- Raspberry Pi OS Bookworm with labwc (Wayland)
- SheepShaver or BasiliskII with serial port support
- Python 3, GTK 3, AyatanaAppIndicator3, socat

## Architecture

```
Mac OS 7.5.5 (emulated)
  └─ Modem Port (.AIn/.AOut)
       └─ seriala → ~/.serial/macmodem  ←── socat PTY pair ──→  ~/.serial/macbridge
                                                                      │
                                                              emu-serial-bridge.py
                                                                      │
                                                     ┌────────────────┼────────────────┐
                                                     │                │                │
                                              brightness.py      battery.py      volume.py
                                              sysfs PWM GPIO12   status JSON     wpctl + GPIO24
```

The bridge creates a socat PTY pair at startup. The emulator opens one end
(`macmodem`) as its serial port. The bridge listens on the other end
(`macbridge`), parsing line-oriented ASCII commands and dispatching them to
handler plugins.

## Installation

```bash
git clone https://github.com/spc6486/emu-serial-bridge.git
cd emu-serial-bridge
./install.sh
```

The installer:

- Installs dependencies (python3-gi, socat, etc.)
- Copies files to `/opt/emu-serial-bridge/`
- Creates launcher at `/usr/local/bin/emu-serial-bridge`
- Installs tray icons to hicolor theme
- Creates XDG autostart and desktop entries
- Configures emulator prefs (`seriala`) if found
- Migrates old `macdesk-serial.service` if present

## Emulator setup

Add this line to your emulator prefs file:

**SheepShaver** (`~/.sheepshaver_prefs`):
```
seriala /home/pi/.serial/macmodem
```

**BasiliskII** (`~/.basilisk_ii_prefs`):
```
seriala /home/pi/.serial/macmodem
```

The installer writes this automatically if the prefs file exists. The port
(Modem or Printer) can be changed in the Settings window.

## Usage

```bash
emu-serial-bridge --tray     # System tray icon (autostart mode)
emu-serial-bridge            # Open settings window, quit on close
emu-serial-bridge --status   # Show bridge status (works while running)
emu-serial-bridge --version  # Show version
```

### Tray icon states

| Icon | Meaning |
|---|---|
| Green fill | Emulator connected (serial port open) |
| Yellow fill | Waiting for emulator |

### Status output

```
$ emu-serial-bridge --status
  Emu Serial Bridge v1.0.0

  Bridge:    running (pid 1438)
  socat:     running (pid 1507)
  Emu PTY:   /home/pi/.serial/macmodem → /dev/pts/0
  Bridge PTY: /home/pi/.serial/macbridge → /dev/pts/1
  Emu port:  seriala
  Emulator:  SheepShaver (pid 2801)
  Handlers:  battery, brightness, volume
```

## Protocol

Line-oriented ASCII, CR or CRLF terminated.

| Command | Response | Handler |
|---|---|---|
| `BRIGHT <0-100>` | `OK BRIGHT <n>` | brightness |
| `BRI?` / `BRIGHT?` | `BRIGHT <n>` | brightness |
| `AUTO <0\|1>` | `OK AUTO <0\|1>` | brightness |
| `AUTO?` | `AUTO <0\|1>` | brightness |
| `BAT?` / `BAT` | `BAT <pct> <CHG\|DIS\|UNK>` | battery |
| `VOL <0-100>` | `OK VOL <n>` | volume |
| `VOL?` | `VOL <n>` | volume |
| `MUTE <0\|1>` | `OK MUTE <0\|1>` | volume |
| `MUTE?` | `MUTE <0\|1>` | volume |
| `HP <0\|1>` | `OK HP <0\|1>` | volume |
| `HP?` | `HP <0\|1>` | volume |

Unknown commands return `ERR UNKNOWN`. Handler errors return `ERR <CMD> <detail>`.

## Handlers

### Brightness

Controls display backlight via sysfs hardware PWM on GPIO12. Shares the PWM
channel and config file (`~/.config/brightness-control/settings.json`) with the
[brightness-control](https://github.com/spc6486/brightness-control) tray app.

### Battery

Reads battery status from `$XDG_RUNTIME_DIR/battery-monitor-status.json`,
written by the [battery-monitor](https://github.com/spc6486/battery-monitor)
tray app. Returns charge percentage and charging state (CHG/DIS/UNK).

### Volume

Controls PipeWire audio via `wpctl` and headphone amplifier via GPIO24. Works
with the [volume-control](https://github.com/spc6486/volume-control) tray app.

## Adding handlers

Drop a Python file into `/opt/emu-serial-bridge/handlers/` and restart:

```python
NAME = "My Handler"
DESCRIPTION = "What it does"

def _cmd_example(args, write):
    write("OK EXAMPLE")

COMMANDS = {
    "EXAMPLE": _cmd_example,
}
```

Optional: define `init(config=None)` and `cleanup()` for setup/teardown.
Handlers can be enabled/disabled in the Settings window without removing files.

## Configuration

Settings are stored in `~/.config/emu-serial-bridge/config.json`:

```json
{
  "serial_dir": "/home/pi/.serial",
  "emu_pty": "macmodem",
  "bridge_pty": "macbridge",
  "emu_port": "seriala",
  "disabled_handlers": [],
  "verbose": false
}
```

## File locations

| Path | Purpose |
|---|---|
| `/opt/emu-serial-bridge/` | Application files |
| `/opt/emu-serial-bridge/handlers/` | Command handler plugins |
| `/usr/local/bin/emu-serial-bridge` | Launcher script |
| `/etc/xdg/autostart/emu-serial-bridge.desktop` | Login autostart |
| `/usr/share/applications/emu-serial-bridge.desktop` | Desktop entry |
| `~/.config/emu-serial-bridge/config.json` | User configuration |
| `~/.serial/macmodem` | Emulator-side PTY (socat) |
| `~/.serial/macbridge` | Bridge-side PTY (socat) |

## Uninstall

```bash
/opt/emu-serial-bridge/install.sh --uninstall
```

Or right-click the desktop entry and select "Uninstall Emu Serial Bridge".

## License

MIT
