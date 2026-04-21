#!/usr/bin/env python3
"""
Emu Serial Bridge — Serial bridge between classic Mac OS emulators and Pi hardware.

Creates a socat PTY pair and listens for line-oriented ASCII commands from
SheepShaver/BasiliskII (or any emulator with serial port support). Commands
are dispatched to handler plugins in the handlers/ directory.

Usage:
    emu-serial-bridge           Open settings window, quit on close
    emu-serial-bridge --tray    Start as tray icon, stay running after window close
"""

import fcntl
import importlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, Gdk, GLib, GObject, AyatanaAppIndicator3 as AppIndicator3

# ── Paths ────────────────────────────────────────────────────────────

APP_ID = "emu-serial-bridge"
APP_DIR = Path(__file__).resolve().parent
VERSION = (APP_DIR / "VERSION").read_text().strip()
ICON_DIR = APP_DIR / "icons"
HANDLER_DIR = APP_DIR / "handlers"
LOCK_FILE = f"/tmp/.{APP_ID}.lock"

CONF_DIR = Path.home() / ".config" / APP_ID
CONF_FILE = CONF_DIR / "config.json"

DEFAULT_SERIAL_DIR = Path.home() / ".serial"
DEFAULT_EMU_PTY = "macmodem"
DEFAULT_BRIDGE_PTY = "macbridge"

# ── Logging ──────────────────────────────────────────────────────────

_log_buffer = deque(maxlen=200)
_log_lock = threading.Lock()


def log(msg):
    ts = time.strftime("%H:%M:%S")
    entry = f"{ts} {msg}"
    with _log_lock:
        _log_buffer.append(entry)
    sys.stderr.write(f"[{APP_ID}] {entry}\n")
    sys.stderr.flush()


# ── Config ───────────────────────────────────────────────────────────

_default_config = {
    "serial_dir": str(DEFAULT_SERIAL_DIR),
    "emu_pty": DEFAULT_EMU_PTY,
    "bridge_pty": DEFAULT_BRIDGE_PTY,
    "emu_port": "seriala",
    "disabled_handlers": [],
    "show_tray": True,
    "verbose": False,
}


def load_config():
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONF_FILE) as f:
            cfg = json.load(f)
        merged = dict(_default_config)
        merged.update(cfg)
        return merged
    except Exception:
        return dict(_default_config)


def save_config(cfg):
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(CONF_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, str(CONF_FILE))
    except Exception as e:
        log(f"config save error: {e}")


def is_tray_enabled():
    cfg = load_config()
    return cfg.get("show_tray", True)


def set_tray_enabled(enabled):
    cfg = load_config()
    cfg["show_tray"] = bool(enabled)
    save_config(cfg)


# ── Single-instance lock ─────────────────────────────────────────────

_lock_fd = None


def acquire_lock():
    global _lock_fd
    try:
        _lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        return False


# ── Handler loader ───────────────────────────────────────────────────

class HandlerRegistry:
    def __init__(self):
        self.handlers = {}      # name -> module
        self.commands = {}      # "CMD" -> (handler_func, handler_name)
        self.disabled = set()

    def load_all(self, disabled_list):
        self.disabled = set(disabled_list)
        if not HANDLER_DIR.is_dir():
            log(f"handler dir not found: {HANDLER_DIR}")
            return

        for path in sorted(HANDLER_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"handlers.{name}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self.handlers[name] = mod

                if name not in self.disabled:
                    cmds = getattr(mod, "COMMANDS", {})
                    for cmd, func in cmds.items():
                        self.commands[cmd.upper()] = (func, name)

                if hasattr(mod, "init"):
                    mod.init()

                hname = getattr(mod, "NAME", name)
                ncmds = len(getattr(mod, "COMMANDS", {}))
                state = "disabled" if name in self.disabled else f"{ncmds} commands"
                log(f"handler: {hname} ({state})")
            except Exception as e:
                log(f"handler load error ({name}): {e}")

    def dispatch(self, line, write_func):
        upper = line.strip()
        if not upper:
            return

        # Split into command and args
        parts = upper.split(None, 1)
        cmd = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        # Try exact match first (for commands like "BRI?" "AUTO?")
        if cmd in self.commands:
            func, hname = self.commands[cmd]
            func(args, write_func)
            return

        # Try command with args joined (e.g. "BAT?" as one token)
        if upper.upper() in self.commands:
            func, hname = self.commands[upper.upper()]
            func("", write_func)
            return

        write_func("ERR UNKNOWN")

    def cleanup_all(self):
        for name, mod in self.handlers.items():
            if hasattr(mod, "cleanup"):
                try:
                    mod.cleanup()
                except Exception:
                    pass

    def reload_all(self):
        """Call reload() on every handler that implements it."""
        for name, mod in self.handlers.items():
            fn = getattr(mod, "reload", None)
            if callable(fn):
                try:
                    fn()
                    log(f"reloaded handler: {name}")
                except Exception as e:
                    log(f"reload() failed for handler {name}: {e}")


# ── socat + PTY bridge ───────────────────────────────────────────────

class SerialBridge:
    def __init__(self, registry):
        self.registry = registry
        self.cfg = load_config()
        self._socat_proc = None
        self._running = False
        self._thread = None
        self._connected = False
        self._socat_pid = None
        self._on_status_change = None

    @property
    def serial_dir(self):
        return Path(self.cfg.get("serial_dir", str(DEFAULT_SERIAL_DIR)))

    @property
    def emu_pty_path(self):
        return self.serial_dir / self.cfg.get("emu_pty", DEFAULT_EMU_PTY)

    @property
    def bridge_pty_path(self):
        return self.serial_dir / self.cfg.get("bridge_pty", DEFAULT_BRIDGE_PTY)

    def _set_connected(self, state):
        if state != self._connected:
            self._connected = state
            if self._on_status_change:
                GLib.idle_add(self._on_status_change, state)

    def _check_emulator_connected(self):
        """Check if any process besides socat has the emulator PTY open."""
        emu_pty = str(self.emu_pty_path)
        if not Path(emu_pty).exists():
            return False
        try:
            real_path = os.path.realpath(emu_pty)
            socat_pid = self._socat_pid or -1
            my_pid = os.getpid()
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                pid = int(pid_dir.name)
                if pid == my_pid or pid == socat_pid:
                    continue
                try:
                    for fd_link in (pid_dir / "fd").iterdir():
                        try:
                            if os.path.realpath(fd_link) == real_path:
                                return True
                        except OSError:
                            continue
                except (PermissionError, OSError):
                    continue
        except Exception:
            pass
        return False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._kill_socat()

    def _kill_socat(self):
        if self._socat_proc and self._socat_proc.poll() is None:
            self._socat_proc.terminate()
            try:
                self._socat_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._socat_proc.kill()
        self._socat_proc = None
        self._socat_pid = None

    def _start_socat(self):
        self._kill_socat()
        sdir = self.serial_dir
        sdir.mkdir(parents=True, exist_ok=True)

        # Clean stale files that might have wrong ownership
        for stale in ["socat.log", "socat.pid", "serialpair.log"]:
            p = sdir / stale
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                try:
                    subprocess.run(["sudo", "/bin/rm", "-f", str(p)],
                                   timeout=3, check=False)
                except Exception:
                    pass

        # Clean stale symlinks
        for name in [self.cfg.get("emu_pty", DEFAULT_EMU_PTY),
                     self.cfg.get("bridge_pty", DEFAULT_BRIDGE_PTY)]:
            p = sdir / name
            if p.is_symlink() or p.exists():
                try:
                    p.unlink(missing_ok=True)
                except PermissionError:
                    pass

        emu = str(self.emu_pty_path)
        bridge = str(self.bridge_pty_path)

        log_path = sdir / "socat.log"
        try:
            log_fh = open(log_path, "w")
        except PermissionError:
            log("socat.log permission error; writing to /dev/null")
            log_fh = open(os.devnull, "w")

        self._socat_proc = subprocess.Popen(
            ["/usr/bin/socat", "-d", "-d",
             f"pty,raw,echo=0,link={emu}",
             f"pty,raw,echo=0,link={bridge}"],
            stderr=log_fh,
        )
        self._socat_pid = self._socat_proc.pid
        log(f"socat started (pid {self._socat_pid})")

        # Wait for symlinks to appear
        for _ in range(20):
            if Path(emu).exists() and Path(bridge).exists():
                return True
            time.sleep(0.1)
        log("socat symlinks not created")
        return False

    def _run(self):
        while self._running:
            if not self._start_socat():
                log("socat failed; retrying in 5s")
                self._set_connected(False)
                time.sleep(5)
                continue

            tty = str(self.bridge_pty_path)
            log(f"listening on {tty}")

            try:
                fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                with os.fdopen(fd, "rb+", buffering=0) as f:
                    log("bridge PTY open, waiting for emulator")
                    buf = b""
                    last_emu_check = 0

                    while self._running:
                        # Check socat is still alive
                        if self._socat_proc and self._socat_proc.poll() is not None:
                            log("socat exited; restarting")
                            break

                        # Periodically check if emulator has the PTY open
                        now = time.monotonic()
                        if now - last_emu_check > 2.0:
                            emu_active = self._check_emulator_connected()
                            self._set_connected(emu_active)
                            last_emu_check = now

                        try:
                            chunk = f.read(1)
                            if not chunk:
                                time.sleep(0.05)
                                continue
                        except BlockingIOError:
                            time.sleep(0.05)
                            continue

                        buf += chunk
                        if b"\n" in buf or b"\r" in buf:
                            sep = b"\r" if b"\r" in buf else b"\n"
                            line_bytes, _, rest = buf.partition(sep)
                            buf = rest.lstrip(b"\r\n")

                            try:
                                line_str = line_bytes.decode("ascii", "ignore").strip()
                            except Exception:
                                line_str = ""

                            if line_str:
                                cfg = load_config()

                                def write_line(s):
                                    try:
                                        f.write((s + "\r").encode("ascii"))
                                        f.flush()
                                    except Exception:
                                        pass
                                    if cfg.get("verbose"):
                                        log(f"→ {s}")

                                if cfg.get("verbose"):
                                    log(f"← {line_str}")
                                else:
                                    log(f"← {line_str}")

                                self.registry.dispatch(line_str, write_line)

            except FileNotFoundError:
                pass
            except Exception as e:
                log(f"bridge error: {e}")

            self._set_connected(False)
            if self._running:
                time.sleep(1)


def find_emulator_prefs():
    """Find all emulator prefs files that reference macmodem."""
    home = Path.home()
    candidates = [
        home / ".sheepshaver_prefs",
        home / ".config" / "SheepShaver" / "prefs",
        home / ".basilisk_ii_prefs",
        home / ".config" / "BasiliskII" / "prefs",
    ]
    found = []
    for p in candidates:
        if p.exists():
            try:
                content = p.read_text()
                has_macmodem = "macmodem" in content
                found.append((str(p), has_macmodem))
            except Exception:
                found.append((str(p), False))
    return found


# ── Settings window ──────────────────────────────────────────────────

class SettingsWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Emu Serial Bridge")
        self.app = app
        self.set_default_size(440, 380)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self._build_ui()

    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)
        self.add(outer)

        # Header
        header = Gtk.Label()
        header.set_markup(
            f"<big><b>Emu Serial Bridge</b></big>  "
            f"<small>v{VERSION}</small>")
        header.set_xalign(0)
        outer.pack_start(header, False, False, 0)

        # Notebook
        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 4)

        notebook.append_page(self._build_connection_tab(), Gtk.Label(label="Connection"))
        notebook.append_page(self._build_handlers_tab(), Gtk.Label(label="Handlers"))
        notebook.append_page(self._build_log_tab(), Gtk.Label(label="Log"))

        # Bottom bar: tray checkbox only
        bottom = Gtk.Box(spacing=8)
        bottom.set_margin_top(4)

        tray_check = Gtk.CheckButton(label="Tray icon")
        tray_check.set_active(is_tray_enabled())
        tray_check.set_tooltip_text(
            "Show tray icon on login.\n"
            "Bridge runs either way. Takes effect on restart.")
        tray_check.connect("toggled", lambda w: set_tray_enabled(w.get_active()))
        bottom.pack_start(tray_check, False, False, 0)

        outer.pack_end(bottom, False, False, 0)

    def _build_connection_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(8)

        bridge = self.app.bridge
        cfg = load_config()

        # Quick instructions
        instructions = Gtk.Label()
        instructions.set_markup(
            "<small>Bridges the emulator's serial port to Pi hardware.\n"
            "The Mac-side app sends commands (brightness, battery, volume)\n"
            "over the virtual serial link. Start the emulator to connect.</small>")
        instructions.set_xalign(0)
        instructions.set_line_wrap(True)
        vbox.pack_start(instructions, False, False, 0)

        # Serial link section
        lbl = Gtk.Label()
        lbl.set_markup("<b>Serial link</b>")
        lbl.set_xalign(0)
        vbox.pack_start(lbl, False, False, 4)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        grid.set_margin_start(4)

        # Emulator port selector
        l = Gtk.Label(label="Emulator port")
        l.set_xalign(0)
        grid.attach(l, 0, 0, 1, 1)

        port_combo = Gtk.ComboBoxText()
        port_combo.append("seriala", "Modem Port (seriala)")
        port_combo.append("serialb", "Printer Port (serialb)")
        current_port = cfg.get("emu_port", "seriala")
        port_combo.set_active_id(current_port)
        port_combo.set_tooltip_text(
            "Which emulator serial port maps to the bridge PTY.\n"
            "Modem Port is the default for BookMac Control.")
        port_combo.connect("changed", self._on_port_changed)
        port_combo.set_hexpand(True)
        grid.attach(port_combo, 1, 0, 1, 1)

        # PTY paths (read-only)
        def add_path_row(grid, row, label, value, tooltip):
            l = Gtk.Label(label=label)
            l.set_xalign(0)
            grid.attach(l, 0, row, 1, 1)
            e = Gtk.Entry()
            e.set_text(str(value))
            e.set_editable(False)
            e.set_tooltip_text(tooltip)
            e.set_hexpand(True)
            grid.attach(e, 1, row, 1, 1)

        add_path_row(grid, 1, "Emulator PTY",
                     bridge.emu_pty_path,
                     "socat endpoint mapped to emulator serial port")
        add_path_row(grid, 2, "Bridge PTY",
                     bridge.bridge_pty_path,
                     "Internal bridge endpoint (auto-managed)")

        vbox.pack_start(grid, False, False, 0)

        note = Gtk.Label()
        note.set_markup(
            "<small>PTY pair created automatically by socat at startup.</small>")
        note.set_xalign(0)
        note.set_line_wrap(True)
        vbox.pack_start(note, False, False, 2)

        # Status section
        lbl2 = Gtk.Label()
        lbl2.set_markup("<b>Status</b>")
        lbl2.set_xalign(0)
        vbox.pack_start(lbl2, False, False, 4)

        status_grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        status_grid.set_margin_start(4)

        def add_status_row(grid, row, label, value_label):
            l = Gtk.Label(label=label)
            l.set_xalign(0)
            grid.attach(l, 0, row, 1, 1)
            grid.attach(value_label, 1, row, 1, 1)

        self._socat_label = Gtk.Label()
        self._socat_label.set_xalign(0)
        self._pty_label = Gtk.Label()
        self._pty_label.set_xalign(0)

        add_status_row(status_grid, 0, "socat", self._socat_label)
        add_status_row(status_grid, 1, "Emulator", self._pty_label)

        # Show detected prefs files
        prefs_found = find_emulator_prefs()
        if prefs_found:
            for i, (path, has_macmodem) in enumerate(prefs_found):
                plabel = Gtk.Label()
                plabel.set_xalign(0)
                # Shorten home path for display
                display_path = path.replace(str(Path.home()), "~")
                if has_macmodem:
                    plabel.set_markup(
                        f"<span foreground='#639922'>{display_path}</span>")
                else:
                    plabel.set_markup(
                        f"<span foreground='#E24B4A'>{display_path} (seriala not set)</span>")
                add_status_row(status_grid, 2 + i, "Prefs" if i == 0 else "", plabel)
        else:
            plabel = Gtk.Label()
            plabel.set_xalign(0)
            plabel.set_markup(
                "<span foreground='#E24B4A'>No emulator prefs found</span>")
            add_status_row(status_grid, 2, "Prefs", plabel)

        vbox.pack_start(status_grid, False, False, 0)

        self._update_status()
        GLib.timeout_add_seconds(2, self._update_status)

        return vbox

    def _on_port_changed(self, combo):
        port_id = combo.get_active_id()
        if not port_id:
            return
        cfg = load_config()
        old_port = cfg.get("emu_port", "seriala")
        if port_id == old_port:
            return
        cfg["emu_port"] = port_id
        save_config(cfg)

        # Update emulator prefs
        home = Path.home()
        serial_dir = cfg.get("serial_dir", str(DEFAULT_SERIAL_DIR))
        emu_pty = cfg.get("emu_pty", DEFAULT_EMU_PTY)
        pty_path = f"{serial_dir}/{emu_pty}"

        for prefs_path in [home / ".sheepshaver_prefs",
                           home / ".config" / "SheepShaver" / "prefs",
                           home / ".basilisk_ii_prefs",
                           home / ".config" / "BasiliskII" / "prefs"]:
            if prefs_path.exists():
                content = prefs_path.read_text()
                old_key = old_port  # seriala or serialb
                new_key = port_id
                # Remove old mapping, add new
                lines = content.splitlines()
                lines = [l for l in lines
                         if not l.startswith(f"{old_key} {pty_path}")]
                # Check if new_key already points elsewhere
                lines = [l for l in lines
                         if not (l.startswith(f"{new_key} ") and pty_path in l)]
                lines.append(f"{new_key} {pty_path}")
                prefs_path.write_text("\n".join(lines) + "\n")
                log(f"updated {prefs_path.name}: {new_key} {pty_path}")

        log(f"emulator port changed to {port_id} (restart emulator to apply)")

    def _update_status(self):
        if not self.get_visible():
            return False
        bridge = self.app.bridge
        if bridge._socat_proc and bridge._socat_proc.poll() is None:
            self._socat_label.set_markup(
                f"<span foreground='#639922'>running (pid {bridge._socat_pid})</span>")
        else:
            self._socat_label.set_markup(
                "<span foreground='#E24B4A'>not running</span>")

        if bridge._connected:
            self._pty_label.set_markup(
                "<span foreground='#639922'>emulator connected</span>")
        else:
            self._pty_label.set_markup(
                "<span foreground='#EF9F27'>waiting for emulator</span>")
        return True

    def _build_handlers_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(8)

        lbl = Gtk.Label()
        lbl.set_markup("<b>Command handlers</b>")
        lbl.set_xalign(0)
        vbox.pack_start(lbl, False, False, 0)

        cfg = load_config()
        disabled = set(cfg.get("disabled_handlers", []))
        registry = self.app.registry

        for name, mod in sorted(registry.handlers.items()):
            frame = Gtk.Frame()
            frame.set_shadow_type(Gtk.ShadowType.NONE)

            hbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            hbox.set_margin_start(4)
            hbox.set_margin_end(4)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)

            # Header row: name + toggle
            top = Gtk.Box(spacing=8)
            hname = getattr(mod, "NAME", name)
            nlabel = Gtk.Label()
            nlabel.set_markup(f"<b>{hname}</b>")
            nlabel.set_xalign(0)
            top.pack_start(nlabel, True, True, 0)

            switch = Gtk.Switch()
            switch.set_active(name not in disabled)
            switch.connect("notify::active",
                           self._on_handler_toggle, name)
            top.pack_end(switch, False, False, 0)
            hbox.pack_start(top, False, False, 0)

            # Commands
            cmds = " ".join(getattr(mod, "COMMANDS", {}).keys())
            clabel = Gtk.Label()
            clabel.set_markup(f"<small><tt>{cmds}</tt></small>")
            clabel.set_xalign(0)
            hbox.pack_start(clabel, False, False, 0)

            # Description
            desc = getattr(mod, "DESCRIPTION", "")
            if desc:
                dlabel = Gtk.Label()
                dlabel.set_markup(f"<small>{desc}</small>")
                dlabel.set_xalign(0)
                dlabel.set_line_wrap(True)
                hbox.pack_start(dlabel, False, False, 0)

            frame.add(hbox)
            vbox.pack_start(frame, False, False, 0)

        note = Gtk.Label()
        note.set_markup(
            f"<small>Drop handler files into {HANDLER_DIR}/ and restart.</small>")
        note.set_xalign(0)
        note.set_line_wrap(True)
        vbox.pack_start(note, False, False, 6)

        return vbox

    def _on_handler_toggle(self, switch, _pspec, handler_name):
        cfg = load_config()
        disabled = set(cfg.get("disabled_handlers", []))
        if switch.get_active():
            disabled.discard(handler_name)
        else:
            disabled.add(handler_name)
        cfg["disabled_handlers"] = sorted(disabled)
        save_config(cfg)
        log(f"handler '{handler_name}' {'enabled' if switch.get_active() else 'disabled'} (restart to apply)")

    def _build_log_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(8)

        lbl = Gtk.Label()
        lbl.set_markup("<b>Protocol activity</b>")
        lbl.set_xalign(0)
        vbox.pack_start(lbl, False, False, 0)

        # Scrolled text view
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(180)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_monospace(True)
        self._log_view.set_wrap_mode(Gtk.WrapMode.NONE)
        scroll.add(self._log_view)
        vbox.pack_start(scroll, True, True, 0)

        # Verbose toggle
        cfg = load_config()
        verbose_check = Gtk.CheckButton(label="Verbose logging")
        verbose_check.set_active(cfg.get("verbose", False))
        verbose_check.set_tooltip_text("Log all traffic to journalctl")
        verbose_check.connect("toggled", self._on_verbose_toggle)
        vbox.pack_start(verbose_check, False, False, 0)

        self._update_log()
        GLib.timeout_add(1000, self._update_log)

        return vbox

    def _update_log(self):
        if not self.get_visible():
            return False
        with _log_lock:
            lines = list(_log_buffer)
        buf = self._log_view.get_buffer()
        buf.set_text("\n".join(lines))
        # Auto-scroll to bottom
        end = buf.get_end_iter()
        self._log_view.scroll_to_iter(end, 0, False, 0, 0)
        return True

    def _on_verbose_toggle(self, widget):
        cfg = load_config()
        cfg["verbose"] = widget.get_active()
        save_config(cfg)


# ── Tray application ─────────────────────────────────────────────────

class MacOSBridgeApp:
    def __init__(self, tray_mode):
        self.tray_mode = tray_mode
        self._settings_win = None
        self._open_windows = set()

        # Load config and handlers
        cfg = load_config()
        self.registry = HandlerRegistry()
        self.registry.load_all(cfg.get("disabled_handlers", []))

        # Start bridge
        self.bridge = SerialBridge(self.registry)
        self.bridge._on_status_change = self._on_bridge_status
        self.bridge.start()

        if tray_mode:
            if cfg.get("show_tray", True):
                self._build_tray()
            # Bridge runs headless if show_tray is False;
            # Gtk.main() keeps the process alive either way
        else:
            self._open_settings()
            # Quit when window closes in non-tray mode
            for w in self._open_windows:
                w.connect("destroy", lambda _: Gtk.main_quit())
                break

    def _build_tray(self):
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            "emu-serial-bridge-waiting",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu()

        # Status item
        self._status_item = Gtk.MenuItem()
        status_box = Gtk.Box(spacing=6)
        self._status_dot = Gtk.DrawingArea()
        self._status_dot.set_size_request(8, 8)
        self._status_dot.connect("draw", self._draw_dot)
        status_box.pack_start(self._status_dot, False, False, 0)
        self._status_label = Gtk.Label(label="Waiting for emulator")
        status_box.pack_start(self._status_label, False, False, 0)
        self._status_item.add(status_box)
        self._status_item.set_sensitive(False)
        menu.append(self._status_item)

        menu.append(Gtk.SeparatorMenuItem())

        item_settings = Gtk.MenuItem(label="Settings…")
        item_settings.connect("activate", lambda _: self._open_settings())
        menu.append(item_settings)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._on_quit)
        menu.append(item_quit)

        menu.show_all()
        self.indicator.set_menu(menu)

    def _draw_dot(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        cr.arc(w / 2, h / 2, 3.5, 0, 2 * 3.14159)
        if self.bridge._connected:
            cr.set_source_rgb(0.39, 0.60, 0.13)  # green
        else:
            cr.set_source_rgb(0.94, 0.62, 0.15)  # amber
        cr.fill()

    def _on_bridge_status(self, connected):
        if self.tray_mode and hasattr(self, "indicator"):
            icon_name = "emu-serial-bridge-connected" if connected else "emu-serial-bridge-waiting"
            self.indicator.set_icon_full(icon_name, "Emu Serial Bridge")
            if connected:
                self._status_label.set_text("Connected")
            else:
                self._status_label.set_text("Waiting for emulator")
            self._status_dot.queue_draw()

    def _open_settings(self, *_):
        if self._settings_win and self._settings_win.get_visible():
            self._settings_win.present()
            return
        win = SettingsWindow(self)
        self._settings_win = win
        self._open_windows.add(win)
        win.connect("destroy", lambda w: self._open_windows.discard(w))
        win.show_all()

    def _on_quit(self, _=None):
        self.bridge.stop()
        self.registry.cleanup_all()
        Gtk.main_quit()


# ── CLI status ───────────────────────────────────────────────────────

def print_status():
    """Print bridge status and exit. Works even when another instance runs."""
    cfg = load_config()
    serial_dir = Path(cfg.get("serial_dir", str(DEFAULT_SERIAL_DIR)))
    emu_pty = serial_dir / cfg.get("emu_pty", DEFAULT_EMU_PTY)
    bridge_pty = serial_dir / cfg.get("bridge_pty", DEFAULT_BRIDGE_PTY)
    emu_port = cfg.get("emu_port", "seriala")

    print(f"  Emu Serial Bridge v{VERSION}")
    print()

    # Bridge process
    try:
        result = subprocess.run(
            ["pgrep", "-af", "emu-serial-bridge.py"],
            capture_output=True, text=True, timeout=2)
        procs = [l for l in result.stdout.strip().splitlines()
                 if "pgrep" not in l and "emu-serial-bridge.py" in l]
        if procs:
            print(f"  Bridge:    running (pid {procs[0].split()[0]})")
        else:
            print(f"  Bridge:    not running")
    except Exception:
        print(f"  Bridge:    unknown")

    # socat
    try:
        result = subprocess.run(
            ["pgrep", "-af", "socat.*macmodem"],
            capture_output=True, text=True, timeout=2)
        procs = [l for l in result.stdout.strip().splitlines()
                 if "pgrep" not in l]
        if procs:
            print(f"  socat:     running (pid {procs[0].split()[0]})")
        else:
            print(f"  socat:     not running")
    except Exception:
        print(f"  socat:     unknown")

    # PTY links
    if emu_pty.is_symlink():
        target = os.path.realpath(emu_pty)
        print(f"  Emu PTY:   {emu_pty} → {target}")
    else:
        print(f"  Emu PTY:   {emu_pty} (not found)")

    if bridge_pty.is_symlink():
        target = os.path.realpath(bridge_pty)
        print(f"  Bridge PTY: {bridge_pty} → {target}")
    else:
        print(f"  Bridge PTY: {bridge_pty} (not found)")

    print(f"  Emu port:  {emu_port}")

    # Emulator detection
    if emu_pty.exists():
        real_path = os.path.realpath(emu_pty)
        socat_pids = set()
        try:
            result = subprocess.run(
                ["pgrep", "-f", "socat.*macmodem"],
                capture_output=True, text=True, timeout=2)
            for line in result.stdout.strip().splitlines():
                socat_pids.add(int(line.strip()))
        except Exception:
            pass

        my_pid = os.getpid()
        emu_procs = []
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                pid = int(pid_dir.name)
                if pid == my_pid or pid in socat_pids:
                    continue
                try:
                    for fd_link in (pid_dir / "fd").iterdir():
                        try:
                            if os.path.realpath(fd_link) == real_path:
                                # Get process name
                                try:
                                    cmdline = (pid_dir / "comm").read_text().strip()
                                except Exception:
                                    cmdline = str(pid)
                                emu_procs.append(f"{cmdline} (pid {pid})")
                                break
                        except OSError:
                            continue
                except (PermissionError, OSError):
                    continue
        except Exception:
            pass

        if emu_procs:
            print(f"  Emulator:  {', '.join(emu_procs)}")
        else:
            print(f"  Emulator:  not connected")
    else:
        print(f"  Emulator:  PTY not available")

    # Handlers
    enabled = []
    disabled_list = cfg.get("disabled_handlers", [])
    if HANDLER_DIR.is_dir():
        for path in sorted(HANDLER_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            if name in disabled_list:
                enabled.append(f"{name} (off)")
            else:
                enabled.append(name)
    print(f"  Handlers:  {', '.join(enabled) if enabled else 'none'}")

    # Prefs files
    prefs_found = find_emulator_prefs()
    if prefs_found:
        for path, has_macmodem in prefs_found:
            display = path.replace(str(Path.home()), "~")
            status = "ok" if has_macmodem else "seriala NOT set"
            print(f"  Prefs:     {display} ({status})")
    else:
        print(f"  Prefs:     none found")

    print()


# ── Main ─────────────────────────────────────────────────────────────

def main():
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"{APP_ID} {VERSION}")
        return

    if "--help" in sys.argv or "-h" in sys.argv:
        print(f"{APP_ID} {VERSION}")
        print("Usage: emu-serial-bridge [OPTIONS]")
        print("  (none)      Open settings window, quit on close")
        print("  --tray      Start as system tray icon")
        print("  --status    Show bridge status (works while running)")
        print("  --version   Show version")
        return

    if "--status" in sys.argv or "--cli" in sys.argv:
        print_status()
        return

    if not acquire_lock():
        print(f"{APP_ID}: already running", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    tray_mode = "--tray" in sys.argv
    app = MacOSBridgeApp(tray_mode)

    # SIGHUP → reload() on all handlers that implement it
    def _on_sighup():
        log("SIGHUP received — reloading handlers")
        app.registry.reload_all()
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, _on_sighup)

    Gtk.main()


if __name__ == "__main__":
    main()
