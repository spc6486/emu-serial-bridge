"""
ha_discovery.py — build the HomeControl device list from Home Assistant.

Rather than maintaining an `aliases:` list by hand, this enumerates Home
Assistant once and caches the result.  HomeControl then curates visually: the
app hides and reorders, and its preferences file remembers those choices.

Kept deliberately separate from homeassistant.py so the working handler is not
put at risk, and so the filtering rules can be unit-tested without a live HA.

WHY DISCOVERY IS A SEPARATE STEP
--------------------------------
State polling already fetches every entity in one `/api/states` call, so
discovery adds nothing to the per-refresh cost.  What is expensive is the area
lookup: `area_name()` has to be rendered server-side through /api/template
because areas are not exposed through /api/states.  That runs once, on demand,
and the result is cached to disk.

ID STABILITY
------------
HomeControl's preferences file keys hidden flags and display order by device
id, so ids must never change or be reused.  The cache holds a permanent
entity_id -> id map; a new entity takes the next free id and a removed one
leaves its id retired.  Sorted enumeration would have been simpler and wrong:
adding one light would renumber everything after it and scramble a saved
layout.

FILTERING
---------
Three rules, applied at discovery only:

1. Domain must be controllable.  A sensor is not something to tap.

2. Collision-suffixed duplicates are dropped.  Home Assistant appends " (N)"
   to a friendly_name that collides with an existing one, so an entity named
   "X (N)" whose base "X" also exists is an un-renamed sibling -- typically an
   extra channel on a multi-output controller.  A channel that was given a
   real name does not match and survives.

3. Entities that are unavailable *at discovery time* are skipped.  These are
   almost always orphans left behind by a rename or re-pairing.  This is not
   applied when listing: a device that goes offline later must still appear,
   shown as Offline, rather than disappearing from the page.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

CACHE_PATH = Path("/var/lib/emu-serial-bridge/ha-devices.json")
CACHE_VERSION = 1

CONTROLLABLE_DOMAINS = (
    "light", "switch", "fan", "cover", "scene", "script", "input_boolean",
)

# "Kitchen Lights (3)" -> base "Kitchen Lights"
_COLLISION = re.compile(r"^(.*) \((\d+)\)$")

UNASSIGNED_PAGE = "Unassigned"


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def friendly_name(entity_id: str, state: dict | None) -> str:
    attrs = (state or {}).get("attributes") or {}
    name = (attrs.get("friendly_name") or "").strip()
    if name:
        return name
    return entity_id.split(".", 1)[1].replace("_", " ").title()


# --------------------------------------------------------------------------
# Entity registry
# --------------------------------------------------------------------------
#
# entity_category and hidden_by are registry properties, not state attributes.
# /api/states returns neither -- its attributes dict for a config switch holds
# only friendly_name -- so filtering on them there silently never fires.  The
# registry is reachable only over the WebSocket API, which is exactly what the
# Home Assistant frontend uses to decide what to show a user.

REGISTRY_CATEGORIES = ("config", "diagnostic")


def fetch_registry(client, timeout: int = 20) -> dict[str, dict]:
    """entity_id -> registry entry.  Returns {} when websocket-client is not
    installed or the connection fails, in which case discovery proceeds
    without registry filtering rather than failing outright."""
    try:
        import websocket  # websocket-client
    except ImportError:
        return {}

    url = client.url.replace("https://", "wss://").replace("http://", "ws://")
    ws = None
    try:
        ws = websocket.create_connection(f"{url}/api/websocket", timeout=timeout)
        json.loads(ws.recv())                       # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": client.token}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            return {}
        ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
        reply = json.loads(ws.recv())
    except Exception:
        return {}
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    entries = reply.get("result") or []
    return {e["entity_id"]: e for e in entries if e.get("entity_id")}


def registry_excludes(entry: dict | None) -> str | None:
    """Reason this entity should not be offered as a control, or None."""
    if not entry:
        return None
    cat = entry.get("entity_category")
    if cat in REGISTRY_CATEGORIES:
        return cat
    if entry.get("hidden_by"):
        return "hidden"
    return None


def control_for(entity_id: str, state: dict | None) -> str:
    """Control type from the domain and attributes.

    Duplicates the handler's detect_control_type deliberately: that function
    reads the domain out of state["entity_id"], which is unavailable for an
    entity Home Assistant cannot resolve.  Here the entity_id is always known."""
    domain = entity_id.split(".", 1)[0]
    attrs = (state or {}).get("attributes") or {}
    if domain == "scene":
        return "scene"
    if domain == "script":
        return "momentary"
    if domain == "cover":
        return "dimmer"
    if domain == "light":
        modes = attrs.get("supported_color_modes") or []
        if any(m in ("brightness", "color_temp", "hs", "rgb", "rgbw", "rgbww", "xy")
               for m in modes):
            return "dimmer"
        if attrs.get("supported_features", 0) & 1:
            return "dimmer"
        return "toggle"
    if domain == "fan":
        return "dimmer" if attrs.get("percentage_step") else "toggle"
    return "toggle"


def is_controllable(entity_id: str, state: dict | None) -> bool:
    if entity_id.split(".", 1)[0] not in CONTROLLABLE_DOMAINS:
        return False
    attrs = (state or {}).get("attributes") or {}
    if attrs.get("entity_category") or attrs.get("hidden_by"):
        return False
    return True


def select_entities(states: dict[str, dict],
                    registry: dict[str, dict] | None = None,
                    skip_unavailable: bool = True) -> tuple[list[str], dict[str, int]]:
    """Apply the filtering rules.  Returns (kept entity_ids, reason counts)."""
    reasons = {"domain": 0, "config": 0, "diagnostic": 0, "hidden": 0,
               "unavailable": 0, "duplicate": 0}
    registry = registry or {}

    candidates = []
    for eid, s in states.items():
        if eid.split(".", 1)[0] not in CONTROLLABLE_DOMAINS:
            reasons["domain"] += 1
            continue
        why = registry_excludes(registry.get(eid))
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        candidates.append(eid)

    # Base names are computed over the candidates only, so a suffixed entity is
    # dropped only when its unsuffixed twin is also a candidate.
    base_names = {friendly_name(e, states.get(e)) for e in candidates}

    kept = []
    for eid in candidates:
        s = states.get(eid) or {}
        if skip_unavailable and str(s.get("state", "")).lower() == "unavailable":
            reasons["unavailable"] += 1
            continue
        m = _COLLISION.match(friendly_name(eid, s))
        if m and m.group(1) in base_names:
            reasons["duplicate"] += 1
            continue
        kept.append(eid)

    return kept, reasons


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def load_cache(path: Path = CACHE_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return new_cache()
        data.setdefault("devices", {})
        data.setdefault("next_id", 1)
        data.setdefault("retired", {})
        return data
    except Exception:
        return new_cache()


def new_cache() -> dict:
    return {"version": CACHE_VERSION, "next_id": 1, "devices": {}, "retired": {}}


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    """Write atomically: a half-written cache would lose every id, and with
    them every hidden flag and ordering choice in HomeControl."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ha-devices-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _format_id(n: int) -> str:
    return f"{n:02d}" if n < 100 else str(n)


def assign_ids(cache: dict, entity_ids: list[str]) -> dict[str, str]:
    """Reuse an entity's existing id, or take the next free one.

    Ids of entities that disappear are retired rather than recycled, so a
    later device can never inherit another's hidden flag or position."""
    devices = cache["devices"]
    out = {}
    for eid in entity_ids:
        prev = devices.get(eid)
        if prev and prev.get("id"):
            out[eid] = str(prev["id"])
            continue
        if eid in cache.get("retired", {}):
            out[eid] = str(cache["retired"].pop(eid))
            continue
        out[eid] = _format_id(cache["next_id"])
        cache["next_id"] += 1
    return out


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

AREA_TEMPLATE = ("{% for s in states %}{{ s.entity_id }}|"
                 "{{ area_name(s.entity_id) or '' }}\n{% endfor %}")


def fetch_areas(client) -> dict[str, str]:
    """entity_id -> area name.  Empty dict when the template API is
    unavailable, in which case everything lands on one page."""
    r = client._session.post(f"{client.url}/api/template",
                             json={"template": AREA_TEMPLATE}, timeout=30)
    r.raise_for_status()
    areas = {}
    for line in r.text.splitlines():
        ent, _, area = line.partition("|")
        area = area.strip()
        if area:
            areas[ent.strip()] = area
    return areas


def discover(client, path: Path = CACHE_PATH,
             skip_unavailable: bool = True) -> dict:
    """Enumerate HA, assign stable ids and write the cache.

    Returns a summary dict; raises on HA failure so the caller can report it
    rather than silently writing an empty device list."""
    states = client._states()
    if not states:
        raise RuntimeError("Home Assistant returned no states")

    registry = fetch_registry(client)
    kept, reasons = select_entities(states, registry=registry,
                                    skip_unavailable=skip_unavailable)

    try:
        areas = fetch_areas(client)
    except Exception:
        areas = {}

    cache = load_cache(path)
    ids = assign_ids(cache, kept)

    previous = set(cache["devices"])
    devices = {}
    for eid in kept:
        entry = registry.get(eid) or {}
        devices[eid] = {
            "id": ids[eid],
            "name": friendly_name(eid, states.get(eid)),
            "page": areas.get(eid) or UNASSIGNED_PAGE,
            "domain": eid.split(".", 1)[0],
            # Remembered so a device still renders with the right control when
            # Home Assistant cannot resolve it later: without this a dimmer
            # would silently lose its slider whenever it went offline.
            "control": control_for(eid, states.get(eid)),
            "flags": "C" if entry.get("entity_category") or entry.get("hidden_by") else "",
        }

    # Retire ids of entities that vanished, so they are never handed out again.
    for eid in previous - set(devices):
        old = cache["devices"][eid].get("id")
        if old:
            cache.setdefault("retired", {})[eid] = old

    cache["devices"] = devices
    save_cache(cache, path)

    pages = []
    for d in devices.values():
        if d["page"] not in pages:
            pages.append(d["page"])
    pages.sort()

    return {
        "registry": len(registry),
        "devices": len(devices),
        "pages": pages,
        "added": len(set(devices) - previous),
        "removed": len(previous - set(devices)),
        "reasons": reasons,
    }


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------
#
# An area per page is wrong at this scale: most rooms hold a single light, and
# switching pages to reach one switch costs more than scrolling past it.  So a
# page is a *group* of areas, and each area becomes a section header inside the
# reply.  The wire format is unchanged -- HA|PAGE| markers already separate
# areas in an unfiltered HA LIST; there are simply several of them per reply
# now instead of one.
#
# Groups are declared in the config rather than packed automatically by size.
# Auto-packing would let a newly added light push another room onto a different
# page, moving controls under the user's fingers between refreshes.  Explicit
# grouping stays put.
#
#   homeassistant:
#     groups:
#       Main:     [Living Room, Hallway, Hall Bathroom]
#       Bedrooms: [Master Bedroom, Master Bathroom, North Bedroom]
#       Other:    [Garage, Exterior, Unassigned]
#
# An area named in no group lands on DEFAULT_GROUP.  With no groups: block at
# all, every area becomes its own page, which is the previous behaviour.

DEFAULT_GROUP = "Other"


def normalise_groups(groups_cfg) -> "list[tuple[str, list[str]]]":
    """Accept a mapping or a list of {name, areas}; return ordered pairs."""
    out = []
    if not groups_cfg:
        return out
    if isinstance(groups_cfg, dict):
        items = groups_cfg.items()
    else:
        items = []
        for entry in groups_cfg:
            if isinstance(entry, dict) and entry.get("name"):
                items.append((entry["name"], entry.get("areas") or []))
    for name, areas in items:
        if not name:
            continue
        if isinstance(areas, str):
            areas = [areas]
        out.append((str(name), [str(a) for a in (areas or [])]))
    return out


def build_pages(devices: "dict[str, dict]", groups_cfg=None,
                default_group: str = DEFAULT_GROUP):
    """Arrange discovered devices into pages of area sections.

    devices maps entity_id -> {id, name, page, domain}, where `page` is the
    Home Assistant area.  Returns an ordered list of
    (page_name, [(area_name, [entity_id, ...]), ...]).

    Area order within a page follows the config; devices within an area follow
    name order, which is stable across discoveries.  Empty areas are dropped so
    a group listing a room with nothing in it does not render a bare header."""
    by_area = {}
    for eid, d in devices.items():
        by_area.setdefault(d.get("page") or UNASSIGNED_PAGE, []).append(eid)
    for ents in by_area.values():
        ents.sort(key=lambda e: (devices[e].get("name") or e).lower())

    groups = normalise_groups(groups_cfg)
    if not groups:
        # No grouping configured: one page per area, alphabetically.
        return [(area, [(area, by_area[area])]) for area in sorted(by_area)]

    claimed = set()
    pages = []
    for name, areas in groups:
        sections = []
        for area in areas:
            if area in by_area:
                sections.append((area, by_area[area]))
                claimed.add(area)
        if sections:
            pages.append((name, sections))

    leftover = sorted(a for a in by_area if a not in claimed)
    if leftover:
        sections = [(a, by_area[a]) for a in leftover]
        existing = next((i for i, (n, _) in enumerate(pages)
                         if n == default_group), None)
        if existing is None:
            pages.append((default_group, sections))
        else:
            pages[existing] = (default_group, pages[existing][1] + sections)

    return pages


def suggest_groups(devices: "dict[str, dict]", target: int = 8) -> "dict[str, list[str]]":
    """Propose a starting groups: block, largest areas first, filling each page
    to roughly `target` devices.  Only a starting point -- the names are
    generic because only the user knows which rooms belong together."""
    by_area = {}
    for d in devices.values():
        by_area[d.get("page") or UNASSIGNED_PAGE] = \
            by_area.get(d.get("page") or UNASSIGNED_PAGE, 0) + 1

    order = sorted(by_area, key=lambda a: (-by_area[a], a))
    pages, current, count = {}, [], 0
    n = 1
    for area in order:
        if current and count + by_area[area] > target:
            pages[f"Page {n}"] = current
            n += 1
            current, count = [], 0
        current.append(area)
        count += by_area[area]
    if current:
        pages[f"Page {n}"] = current
    return pages
