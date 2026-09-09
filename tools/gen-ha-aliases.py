#!/usr/bin/env python3
"""
gen-ha-aliases.py — build the emu-serial-bridge alias block from a live
Home Assistant instance.

The Home Assistant handler only exposes entities listed under `aliases:` in
/etc/emu-serial-bridge/homeassistant.conf.  Writing that list by hand is
tedious for anything past a handful of devices, so this enumerates HA, groups
entities into pages, and prints a ready-to-paste configuration block.

It never writes the config file.  Output goes to stdout for review, or to a
path given with --output, so a working configuration is never clobbered by a
bad run.

ID STABILITY
------------
Device ids appear in the HomeControl preferences file, which records display
order and hidden flags per id.  Renumbering would silently scramble a saved
layout, so ids already present in the config are preserved for their entity
and only new entities receive new ids.  Pass --renumber to override.

NON-ASCII NAMES
---------------
emu-serial-bridge.py writes replies with `.encode("ascii")` inside a bare
try/except, so a device whose name contains a non-ASCII character is silently
dropped from the wire with no error anywhere.  Names are transliterated to
ASCII here to avoid that.  Mac OS 7.5 could not render UTF-8 anyway.

Usage:
    python3 gen-ha-aliases.py                     # group by HA area
    python3 gen-ha-aliases.py --by domain         # group by entity domain
    python3 gen-ha-aliases.py --domains light,switch,scene
    python3 gen-ha-aliases.py --output /tmp/aliases.yaml
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: sudo apt install python3-yaml")

try:
    import requests
except ImportError:
    sys.exit("requests required: sudo apt install python3-requests")


DEFAULT_CONFIG = Path("/etc/emu-serial-bridge/homeassistant.conf")

# Domains the handler can actually actuate.  detect_control_type() maps each
# of these onto one of its four control types; anything else would arrive as
# a read-only row, so it is excluded by default rather than cluttering pages.
DEFAULT_DOMAINS = ["light", "switch", "fan", "cover", "scene", "script",
                   "input_boolean"]

HTTP_TIMEOUT = 10

# Keep names short enough to display and to leave the gateway's 3072-byte
# reply cap usable.  kHANameMax in hamodel.h is 48, so 40 fits with room.
NAME_MAX = 40


def load_config(path):
    if not path.exists():
        sys.exit(f"No config at {path}")
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        sys.exit(f"Could not parse {path}: {e}")
    ha = cfg.get("homeassistant") or {}
    if not ha.get("url") or not ha.get("token"):
        sys.exit(f"{path} has no url/token — populate it before generating")
    return cfg, ha


def ha_get(url, token, path):
    r = requests.get(f"{url.rstrip('/')}{path}",
                     headers={"Authorization": f"Bearer {token}"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def ha_template(url, token, template):
    """Render a Jinja template server-side.  Areas are not exposed through
    /api/states, but area_name() is available to templates."""
    r = requests.post(f"{url.rstrip('/')}/api/template",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      data=json.dumps({"template": template}),
                      timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def to_ascii(text):
    """Transliterate to ASCII so the bridge's ascii encode cannot drop the
    line.  Accented Latin decomposes to its base letter; anything with no
    ASCII equivalent is dropped."""
    norm = unicodedata.normalize("NFKD", text)
    out = norm.encode("ascii", "ignore").decode("ascii")
    out = out.replace("|", "/")            # '|' is the wire delimiter
    out = re.sub(r"\s+", " ", out).strip()
    return out


def elide(name, limit):
    """Shorten from the middle, not the tail.

    Home Assistant puts the distinguishing word last: the four switches on one
    Nest Protect differ only by their Pathlight / Heads Up / Nightly Promise /
    Steam Check suffix.  Cutting the tail makes them identical; cutting the
    middle keeps both the device and the function readable."""
    if len(name) <= limit:
        return name
    head = (limit - 3) // 2
    tail = limit - 3 - head
    return name[:head].rstrip() + "..." + name[len(name) - tail:].lstrip()


def clean_name(raw, entity_id):
    name = to_ascii(raw or "")
    if not name:
        name = entity_id.split(".", 1)[1].replace("_", " ").title()
    if len(name) > NAME_MAX:
        name = elide(name, NAME_MAX)
    return name


def fetch_areas(url, token):
    """entity_id -> area name.

    Returns None when areas are unusable — the template API is unavailable, or
    HA has no areas assigned — so the caller can genuinely fall back to domain
    grouping instead of filing every device under Unassigned."""
    tpl = ("{% for s in states %}{{ s.entity_id }}|"
           "{{ area_name(s.entity_id) or '' }}\n{% endfor %}")
    try:
        text = ha_template(url, token, tpl)
    except Exception as e:
        print(f"# area lookup failed ({e}); grouping by domain instead",
              file=sys.stderr)
        return None
    areas = {}
    for line in text.splitlines():
        if "|" not in line:
            continue
        ent, _, area = line.partition("|")
        area = to_ascii(area.strip())
        if area:
            areas[ent.strip()] = area
    if not areas:
        print("# no areas assigned in Home Assistant; grouping by domain",
              file=sys.stderr)
        return None
    return areas


def report_stale(ha, states, out=sys.stderr):
    """Warn about aliases whose entity is no longer in Home Assistant.

    This is the failure that is otherwise invisible: the device keeps its
    configured name and simply reports GONE forever, with nothing pointing at
    the entity_id that needs updating.  Suggests a replacement when exactly
    one entity in the same domain has a similar name."""
    live = {s.get("entity_id") for s in states}
    aliases = ha.get("aliases") or []
    stale = [a for a in aliases if a.get("entity") not in live]
    if not stale:
        return 0

    by_name = {}
    for s in states:
        fn = ((s.get("attributes") or {}).get("friendly_name") or "").lower()
        if fn:
            by_name.setdefault(fn, []).append(s["entity_id"])

    print("", file=out)
    print(f"WARNING: {len(stale)} alias(es) point at entities not in HA:",
          file=out)
    for a in stale:
        ent = a.get("entity", "?")
        label = a.get("name") or ent
        print(f"  id {a.get('id')}: {ent}  (shown as \"{label}\")", file=out)

        dom = ent.split(".", 1)[0]
        want = str(label).lower()
        hits = [e for name, ents in by_name.items()
                for e in ents
                if e.startswith(dom + ".") and (want in name or name in want)]
        for h in sorted(set(hits))[:3]:
            print(f"      possible replacement: {h}", file=out)
    return len(stale)


def existing_aliases(ha):
    """entity_id -> {id, control}, from the current config.

    Both are preserved across regeneration.  Ids because the preferences file
    keys display order and hidden flags by id.  Control because a deliberate
    override (forcing a colour bulb to a plain toggle, say) must not be
    silently reverted to auto by a rerun."""
    out = {}
    for a in ha.get("aliases") or []:
        ent = a.get("entity")
        if not ent:
            continue
        entry = {}
        if a.get("id") is not None:
            entry["id"] = str(a["id"])
        ctl = a.get("control")
        if ctl and ctl != "auto":
            entry["control"] = ctl
        out[ent] = entry
    return out


def next_free_id(used):
    n = 1
    while True:
        cand = f"{n:02d}"
        if cand not in used:
            return cand
        n += 1


def build(args):
    cfg_path = Path(args.config)
    cfg, ha = load_config(cfg_path)
    url, token = ha["url"], ha["token"]

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]

    states = ha_get(url, token, "/api/states")
    report_stale(ha, states)

    group_by = args.by
    areas = {}
    if group_by == "area":
        found = fetch_areas(url, token)
        if found is None:
            group_by = "domain"
        else:
            areas = found

    keep = {}
    raw_names = {}
    for s in states:
        ent = s.get("entity_id", "")
        dom = ent.split(".", 1)[0]
        if dom not in domains:
            continue
        if args.exclude and re.search(args.exclude, ent):
            continue
        attrs = s.get("attributes") or {}
        if attrs.get("hidden_by") or attrs.get("entity_category"):
            continue          # diagnostic and config entities are not controls
        keep[ent] = clean_name(attrs.get("friendly_name"), ent)
        raw_names[ent] = (attrs.get("friendly_name") or "").strip()

    if not keep:
        sys.exit("No matching entities — check --domains")

    prior = {} if args.renumber else existing_aliases(ha)
    used = {e["id"] for e in prior.values() if "id" in e}

    rows = []
    for ent in sorted(keep):
        dom = ent.split(".", 1)[0]
        if group_by == "area":
            page = areas.get(ent) or "Unassigned"
        else:
            page = dom.replace("_", " ").title()
        if dom in ("scene", "script") and args.scenes_page:
            page = args.scenes_page
        prev = prior.get(ent, {})
        name = keep[ent]
        # With --live-names, a name that survived cleaning unchanged is left
        # out of the config so the handler reads it from HA each time.  A name
        # that had to be transliterated or truncated is pinned, because the
        # handler would otherwise emit something too long or non-ASCII.
        emit_name = name
        if args.live_names and name == (raw_names.get(ent) or ""):
            emit_name = None
        rows.append({"entity": ent, "name": emit_name, "page": page,
                     "id": prev.get("id"),
                     "control": prev.get("control", "auto")})

    for r in rows:
        if r["id"] is None:
            r["id"] = next_free_id(used)
            used.add(r["id"])

    # Page order: the configured order first so existing pages keep their
    # position in the popup, then any new pages alphabetically.
    configured = [p for p in (ha.get("pages") or [])]
    found = sorted({r["page"] for r in rows})
    pages = [p for p in configured if p in found]
    pages += [p for p in found if p not in pages]

    rows.sort(key=lambda r: (pages.index(r["page"]),
                             (r["name"] or r["entity"]).lower()))
    return pages, rows, len(states), raw_names


def emit(pages, rows, total_entities, args, raw_names):
    out = []
    out.append("# Generated by gen-ha-aliases.py — review before installing.")
    out.append(f"# {len(rows)} entities selected from {total_entities} in HA.")
    out.append("#")
    out.append("# Replace the pages: and aliases: blocks in")
    out.append("# /etc/emu-serial-bridge/homeassistant.conf with the following.")
    out.append("# Keep the existing url: and token: lines.")
    out.append("")
    out.append("  pages:")
    for p in pages:
        out.append(f"    - {p}")
    out.append("  aliases:")
    for r in rows:
        out.append(f'    - id: "{r["id"]}"')
        out.append(f'      entity: {r["entity"]}')
        if r["name"] is not None:
            out.append(f'      name: {r["name"]}')
        out.append(f'      page: {r["page"]}')
        out.append(f'      control: {r["control"]}')
    text = "\n".join(out) + "\n"

    # Size check against the gateway's relay buffer.  A page that cannot fit
    # in one reply would be silently truncated, so warn before it happens.
    print("", file=sys.stderr)
    print("Per-page reply size estimate (gateway cap 3072 bytes):",
          file=sys.stderr)
    for p in pages:
        items = [r for r in rows if r["page"] == p]
        size = 9 + len(p) + 7
        for r in items:
            nm = r["name"] or raw_names.get(r["entity"]) or r["entity"]
            size += len(f'HA|{r["id"]}|{nm[:32]}|xxxxx|OFF|dimmer|100') + 1
        flag = "  <-- OVER CAP, split this page" if size > 3072 else ""
        print(f"  {p}: {len(items)} devices, ~{size} bytes{flag}",
              file=sys.stderr)
    print("", file=sys.stderr)

    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def main():
    ap = argparse.ArgumentParser(
        description="Generate emu-serial-bridge HA aliases from a live "
                    "Home Assistant instance.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help="existing config, read for url/token/ids")
    ap.add_argument("--by", choices=["area", "domain"], default="area",
                    help="page grouping (default: area)")
    ap.add_argument("--domains", default=",".join(DEFAULT_DOMAINS),
                    help="comma-separated domains to include")
    ap.add_argument("--exclude", default="",
                    help="regex; matching entity_ids are skipped")
    ap.add_argument("--scenes-page", default="Scenes",
                    help="page for scene and script entities; "
                         "empty string to group them like everything else")
    ap.add_argument("--renumber", action="store_true",
                    help="assign fresh ids, discarding existing mappings "
                         "(breaks saved HomeControl layouts)")
    ap.add_argument("--live-names", action="store_true",
                    help="omit name: where Home Assistant's own name is "
                         "already clean, so renames in HA propagate "
                         "automatically (requires the handler's "
                         "friendly_name fallback)")
    ap.add_argument("--output", default="",
                    help="write here instead of stdout")
    args = ap.parse_args()

    pages, rows, total, raw_names = build(args)
    emit(pages, rows, total, args, raw_names)


if __name__ == "__main__":
    main()
