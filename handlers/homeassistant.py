#!/usr/bin/env python3
"""
Home Assistant handler for emu-serial-bridge.

Drop this into /opt/emu-serial-bridge/handlers/ and restart the bridge
(or send SIGHUP if the bridge supports reload hooks).

Protocol v3 (all commands CR-terminated on the wire):

    HA LIST
        -> HA|PAGE|<pageName>\\r        (one per page, as dividers)
           HA|<id>|<n>|<domain>|<STATE>|<control>|<value>\\r
           ...
           HA|END\\r
        Errors: ERR|UNREACHABLE | ERR|NOTCONFIGURED

    HA LIST <pageName>
        -> Same, but only devices on that page.

    HA PAGES
        -> HA|PAGES|Home|Lights|Scenes\\r

    HA ON <id>       -> OK|<id>|<n>|<domain>|ON|<ctl>|<val>\\r
    HA OFF <id>      -> OK|<id>|<n>|<domain>|OFF|<ctl>|<val>\\r
    HA TOGGLE <id>   -> OK|<id>|<n>|<domain>|<new>|<ctl>|<val>\\r
    HA DIM <id> <n>  -> OK|<id>|<n>|<domain>|<ON|OFF>|dimmer|<n>\\r
    HA SCENE <id>    -> OK|<id>|<n>|scene|OFF|scene|\\r
    HA PRESS <id>    -> OK|<id>|<n>|script|OFF|momentary|\\r (alias for SCENE)

    All errors: ERR|<CODE>[|<id>[|<msg>]]\\r
    Codes: UNKNOWN, BADARG, FAILED, UNREACHABLE, NOTCONFIGURED,
           EXCEPTION, NOCMD, BADCMD

Action replies report the *intended* post-action state, not a fresh read
from HA. This avoids an extra HTTP round-trip and a race with HA's eventual
consistency. If HA's actual state diverges, the next manual HA LIST will
reconcile. For non-DIM ON commands on dimmers, <val> is empty (the handler
does not know what brightness HA will restore to).

Control types:
    toggle     -> ON / OFF / TOGGLE supported
    dimmer     -> plus DIM <n>; value field = brightness %
    scene      -> SCENE activates; value field = blank
    momentary  -> PRESS activates; value field = blank

The handler re-reads its config file automatically when it changes
(mtime-based). A bridge that sends SIGHUP to trigger explicit reload()
is also supported -- the logic is identical.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    import requests
except ImportError:
    requests = None


log = logging.getLogger("emu-serial-bridge.ha")

CONFIG_PATH = Path("/etc/emu-serial-bridge/homeassistant.conf")
STATE_TTL_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 3.0
HANDLER_VERSION = "3.0.0"


# --- Control type helpers --------------------------------------------

def detect_control_type(state: dict) -> str:
    """Auto-detect a control type from an HA state dict."""
    if not state:
        return "toggle"
    entity = state.get("entity_id", "")
    domain = entity.split(".", 1)[0]
    attrs = state.get("attributes") or {}

    if domain == "scene":
        return "scene"
    if domain == "script":
        return "momentary"
    if domain == "light":
        modes = attrs.get("supported_color_modes") or []
        features = attrs.get("supported_features", 0)
        if any(m in ("brightness", "color_temp", "hs", "rgb", "rgbw", "rgbww", "xy")
               for m in modes):
            return "dimmer"
        if features & 1:
            return "dimmer"
        return "toggle"
    if domain == "cover":
        return "dimmer"
    if domain == "fan":
        return "dimmer" if attrs.get("percentage_step") else "toggle"
    return "toggle"


def state_to_wire(state: dict | None) -> str:
    """Map an HA state to a short wire token."""
    if state is None:
        return "GONE"
    raw = (state.get("state") or "unknown").lower()
    if raw in ("on", "off"):
        return raw.upper()
    if raw == "unavailable":
        return "UNAVAIL"
    if raw in ("open", "opening"):
        return "ON"
    if raw in ("closed", "closing"):
        return "OFF"
    if raw == "playing":
        return "ON"
    if raw in ("paused", "idle"):
        return "OFF"
    return raw.upper()[:8]


def brightness_pct(state: dict | None) -> int:
    if state is None:
        return 0
    attrs = state.get("attributes") or {}
    b = attrs.get("brightness")
    if b is not None:
        try:
            return max(0, min(100, int(round(int(b) * 100 / 255))))
        except Exception:
            return 0
    pct = attrs.get("percentage")
    if pct is not None:
        try:
            return max(0, min(100, int(pct)))
        except Exception:
            return 0
    return 0


# --- Client ----------------------------------------------------------

class HAClient:
    def __init__(self, url: str, token: str, aliases: list[dict], pages: list[str]):
        self.url = url.rstrip("/")
        self.token = token
        self.aliases = aliases
        self.pages = pages or ["Home"]
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self._cache: dict[str, dict] = {}
        self._cache_expiry = 0.0
        self._lock = threading.Lock()

    # ---- low-level HA API ----

    def _states(self) -> dict[str, dict]:
        with self._lock:
            now = time.monotonic()
            if now < self._cache_expiry and self._cache:
                return self._cache
            try:
                r = self._session.get(
                    f"{self.url}/api/states",
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                r.raise_for_status()
                self._cache = {s["entity_id"]: s for s in r.json()}
                self._cache_expiry = now + STATE_TTL_SECONDS
                return self._cache
            except Exception as e:
                log.warning("HA /api/states failed: %s", e)
                return {}

    def _invalidate(self) -> None:
        with self._lock:
            self._cache_expiry = 0.0

    def _call_service(self, domain: str, service: str, data: dict) -> bool:
        try:
            r = self._session.post(
                f"{self.url}/api/services/{domain}/{service}",
                json=data,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            self._invalidate()
            return True
        except Exception as e:
            log.warning("HA /api/services/%s/%s failed: %s", domain, service, e)
            return False

    # ---- alias resolution ----

    def _resolve(self, alias_id: str) -> dict | None:
        stripped = alias_id.lstrip("0") or "0"
        for a in self.aliases:
            aid = str(a.get("id", "")).lstrip("0") or "0"
            if aid == stripped:
                return a
        return None

    def _effective_control(self, alias: dict, state: dict | None) -> str:
        ctl = alias.get("control", "auto")
        if ctl != "auto":
            return ctl
        if state is None:
            domain = alias["entity"].split(".", 1)[0]
            if domain == "scene":
                return "scene"
            if domain == "script":
                return "momentary"
            return "toggle"
        return detect_control_type(state)

    # ---- wire-line formatters ----

    def _device_fields(self, alias: dict, state_override: str | None = None,
                       value_override: str | None = None) -> str:
        """Format a device as pipe-delimited fields.
        Returns: <id>|<n>|<domain>|<state>|<ctl>|<value>
        (no HA| or OK| prefix; caller prepends)."""
        entity = alias["entity"]
        domain = entity.split(".", 1)[0]
        name = self._sanitize(alias.get("name", entity))
        aid = str(alias["id"]).zfill(2)
        s = self._states().get(entity)
        ctl = self._effective_control(alias, s)

        if state_override is not None:
            wire_state = state_override
        elif domain in ("scene", "script"):
            wire_state = "OFF"
        else:
            wire_state = state_to_wire(s)

        if value_override is not None:
            value = value_override
        elif ctl == "dimmer":
            value = str(brightness_pct(s))
        else:
            value = ""

        return f"{aid}|{name}|{domain}|{wire_state}|{ctl}|{value}"

    # ---- protocol handlers ----

    def cmd_pages(self) -> str:
        if not self.pages:
            return "HA|PAGES|Home\r"
        return "HA|PAGES|" + "|".join(self.pages) + "\r"

    def cmd_list(self, page_filter: str | None = None) -> str:
        states = self._states()
        if not states:
            return "ERR|UNREACHABLE\r"

        # Group aliases by page, in self.pages order
        by_page: dict[str, list[dict]] = {p: [] for p in self.pages}
        for a in self.aliases:
            p = a.get("page", "Home")
            if p not in by_page:
                by_page[p] = []
                self.pages.append(p)
            if page_filter is None or p == page_filter:
                by_page[p].append(a)

        lines = []
        for page in self.pages:
            items = by_page.get(page, [])
            if page_filter is not None and page != page_filter:
                continue
            if not items and page_filter is None:
                continue  # skip empty pages in full list
            lines.append(f"HA|PAGE|{self._sanitize(page)}")
            for a in items:
                lines.append("HA|" + self._device_fields(a))

        lines.append("HA|END")
        return "\r".join(lines) + "\r"

    def cmd_on(self, alias_id: str) -> str:
        a = self._resolve(alias_id)
        if a is None:
            return f"ERR|UNKNOWN|{alias_id}\r"
        domain = a["entity"].split(".", 1)[0]
        ok = self._call_service(domain, "turn_on", {"entity_id": a["entity"]})
        if not ok:
            return f"ERR|FAILED|{alias_id}\r"
        # For dimmers we don't know restored brightness; leave blank.
        return "OK|" + self._device_fields(a, state_override="ON",
                                           value_override="") + "\r"

    def cmd_off(self, alias_id: str) -> str:
        a = self._resolve(alias_id)
        if a is None:
            return f"ERR|UNKNOWN|{alias_id}\r"
        domain = a["entity"].split(".", 1)[0]
        ok = self._call_service(domain, "turn_off", {"entity_id": a["entity"]})
        if not ok:
            return f"ERR|FAILED|{alias_id}\r"
        ctl = self._effective_control(a, self._states().get(a["entity"]))
        val = "0" if ctl == "dimmer" else ""
        return "OK|" + self._device_fields(a, state_override="OFF",
                                           value_override=val) + "\r"

    def cmd_toggle(self, alias_id: str) -> str:
        a = self._resolve(alias_id)
        if a is None:
            return f"ERR|UNKNOWN|{alias_id}\r"
        entity = a["entity"]
        domain = entity.split(".", 1)[0]
        current = (self._states().get(entity, {}).get("state") or "off").lower()
        if current == "on":
            ok = self._call_service(domain, "turn_off", {"entity_id": entity})
            new_state, new_val = "OFF", "0"
        else:
            ok = self._call_service(domain, "turn_on", {"entity_id": entity})
            new_state, new_val = "ON", ""
        if not ok:
            return f"ERR|FAILED|{alias_id}\r"
        ctl = self._effective_control(a, self._states().get(entity))
        val = new_val if ctl == "dimmer" else ""
        return "OK|" + self._device_fields(a, state_override=new_state,
                                           value_override=val) + "\r"

    def cmd_dim(self, alias_id: str, pct_str: str) -> str:
        a = self._resolve(alias_id)
        if a is None:
            return f"ERR|UNKNOWN|{alias_id}\r"
        try:
            pct = max(0, min(100, int(pct_str)))
        except ValueError:
            return f"ERR|BADARG|{alias_id}\r"
        entity = a["entity"]
        domain = entity.split(".", 1)[0]
        if pct == 0:
            ok = self._call_service(domain, "turn_off", {"entity_id": entity})
            new_state = "OFF"
        elif domain == "light":
            ok = self._call_service(domain, "turn_on",
                                    {"entity_id": entity, "brightness_pct": pct})
            new_state = "ON"
        elif domain == "fan":
            ok = self._call_service(domain, "turn_on",
                                    {"entity_id": entity, "percentage": pct})
            new_state = "ON"
        elif domain == "cover":
            ok = self._call_service(domain, "set_cover_position",
                                    {"entity_id": entity, "position": pct})
            new_state = "ON"
        else:
            ok = self._call_service(domain, "turn_on", {"entity_id": entity})
            new_state = "ON"
        if not ok:
            return f"ERR|FAILED|{alias_id}\r"
        return "OK|" + self._device_fields(a, state_override=new_state,
                                           value_override=str(pct)) + "\r"

    def cmd_scene(self, alias_id: str) -> str:
        a = self._resolve(alias_id)
        if a is None:
            return f"ERR|UNKNOWN|{alias_id}\r"
        entity = a["entity"]
        domain = entity.split(".", 1)[0]
        if domain == "scene":
            ok = self._call_service("scene", "turn_on", {"entity_id": entity})
        elif domain == "script":
            ok = self._call_service("script", "turn_on", {"entity_id": entity})
        else:
            ok = self._call_service(domain, "turn_on", {"entity_id": entity})
        if not ok:
            return f"ERR|FAILED|{alias_id}\r"
        return "OK|" + self._device_fields(a, state_override="OFF",
                                           value_override="") + "\r"

    @staticmethod
    def _sanitize(text: str) -> str:
        return (text.replace("|", "/")
                    .replace("\r", " ")
                    .replace("\n", " "))


# --- Module-level glue -----------------------------------------------

_client: HAClient | None = None
_config_mtime: float = 0.0
_init_lock = threading.Lock()


def _build_client() -> HAClient | None:
    if yaml is None or requests is None:
        return None
    if not CONFIG_PATH.exists():
        return None
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        log.error("Parse error in %s: %s", CONFIG_PATH, e)
        return None
    ha = cfg.get("homeassistant") or {}
    url = ha.get("url")
    token = ha.get("token")
    aliases = ha.get("aliases") or []
    pages = ha.get("pages") or []
    if not url or not token:
        log.error("homeassistant.url and .token required in %s", CONFIG_PATH)
        return None
    # Derive pages if missing
    if not pages:
        seen = set()
        pages = []
        for a in aliases:
            p = a.get("page", "Home")
            if p not in seen:
                pages.append(p); seen.add(p)
        if not pages:
            pages = ["Home"]
    return HAClient(url, token, aliases, pages)


def _ensure_fresh() -> None:
    """Lazy reload if the config file has changed on disk."""
    global _client, _config_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        with _init_lock:
            _client = None
            _config_mtime = 0.0
        return
    if mtime == _config_mtime and _client is not None:
        return
    with _init_lock:
        if mtime == _config_mtime and _client is not None:
            return
        log.info("Reloading HA config (mtime changed)")
        _client = _build_client()
        _config_mtime = mtime
        if _client:
            log.info("HA handler ready: %s (%d aliases, %d pages)",
                     _client.url, len(_client.aliases), len(_client.pages))


# --- Bridge plugin contract ------------------------------------------

def init(bridge_config: dict | None = None) -> None:
    """Called once by the bridge at startup."""
    log.info("HA handler v%s starting", HANDLER_VERSION)
    if yaml is None:
        log.error("PyYAML not installed -- HA handler disabled")
        return
    if requests is None:
        log.error("python3-requests not installed -- HA handler disabled")
        return
    _ensure_fresh()


def reload() -> None:
    """Bridge v1.1+ calls this on SIGHUP. Force a config re-read."""
    global _config_mtime
    log.info("HA handler reload() invoked")
    _config_mtime = 0.0
    _ensure_fresh()


def cleanup() -> None:
    global _client
    _client = None


def handle(line: str) -> str | None:
    """Called by the bridge for every incoming line."""
    if not line:
        return None
    parts = line.strip().split()
    if not parts or parts[0].upper() != "HA":
        return None

    _ensure_fresh()

    if _client is None:
        return "ERR|NOTCONFIGURED\r"

    if len(parts) < 2:
        return "ERR|NOCMD\r"

    cmd = parts[1].upper()

    try:
        if cmd == "PAGES":
            return _client.cmd_pages()
        if cmd == "LIST":
            page = " ".join(parts[2:]) if len(parts) >= 3 else None
            return _client.cmd_list(page)
        if cmd == "ON" and len(parts) >= 3:
            return _client.cmd_on(parts[2])
        if cmd == "OFF" and len(parts) >= 3:
            return _client.cmd_off(parts[2])
        if cmd == "TOGGLE" and len(parts) >= 3:
            return _client.cmd_toggle(parts[2])
        if cmd == "DIM" and len(parts) >= 4:
            return _client.cmd_dim(parts[2], parts[3])
        if cmd in ("SCENE", "PRESS") and len(parts) >= 3:
            return _client.cmd_scene(parts[2])
    except Exception as e:
        log.exception("HA handler error: %s", e)
        return "ERR|EXCEPTION\r"

    return f"ERR|BADCMD|{cmd}\r"


def _ha_adapter(args, write):
    """Bridge dispatch contract adapter.

    The emu-serial-bridge v1.1 contract is ``COMMANDS[cmd](args, write)``
    where ``args`` is a single string containing the line remainder after
    the command keyword (not a list of tokens) and ``write`` is a callable
    that accepts a single string (the bridge adds line termination).

    This handler's internal ``handle(line)`` predates that contract: it
    expects the full line including the ``HA`` prefix and returns a
    CR-terminated (possibly multi-line) string or ``None``. This adapter
    bridges the two without rewriting the dispatch logic below.
    """
    line = "HA " + args if args else "HA"
    reply = handle(line)
    if not reply:
        return
    for chunk in reply.split("\r"):
        if chunk:
            write(chunk)


COMMANDS = {"HA": _ha_adapter}


# --- Standalone test mode --------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    init()
    if _client is None:
        print("Handler failed to initialize -- check config at", CONFIG_PATH,
              file=sys.stderr)
        sys.exit(1)
    print("HA handler test REPL. Commands: LIST, PAGES, ON <id>, OFF <id>,")
    print("                                TOGGLE <id>, DIM <id> <n>,")
    print("                                SCENE <id>, PRESS <id>, quit")
    while True:
        try:
            raw = input("HA> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if raw in ("quit", "exit", "q"):
            break
        if not raw:
            continue
        reply = handle("HA " + raw)
        if reply is None:
            print("(no handler matched)")
        else:
            sys.stdout.write(reply.replace("\r", "\n"))
            sys.stdout.flush()
