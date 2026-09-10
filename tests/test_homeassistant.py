"""Tests for the Home Assistant handler: wire-format helpers and the HAClient
protocol (list / pages / on / off / toggle / dim / scene), using a stub HTTP
session so no live Home Assistant instance is required."""

import time

import pytest

import homeassistant as ha


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, state_list):
        self.state_list = state_list
        self.gets = []
        self.posts = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResp(self.state_list)

    def post(self, url, json=None, timeout=None):
        # Record the service path (e.g. "light/turn_on") for easy assertions.
        self.posts.append((url.split("/api/services/", 1)[1], json))
        return FakeResp({})


ALIASES = [
    {"id": 1, "entity": "light.lamp", "name": "Lamp", "page": "Home"},
    {"id": 2, "entity": "switch.plug", "name": "Plug", "page": "Home"},
    {"id": 3, "entity": "cover.blinds", "name": "Blinds", "page": "Lights"},
]

# The real /api/states endpoint returns a LIST of state dicts; HAClient turns
# it into an {entity_id: state} cache. Mirroring that here.
STATE_LIST = [
    {"entity_id": "light.lamp", "state": "on",
     "attributes": {"brightness": 128, "supported_color_modes": ["brightness"]}},
    {"entity_id": "switch.plug", "state": "off", "attributes": {}},
    {"entity_id": "cover.blinds", "state": "open", "attributes": {}},
]
STATE_MAP = {s["entity_id"]: s for s in STATE_LIST}


@pytest.fixture
def client():
    c = ha.HAClient("http://ha.test", "token", ALIASES, ["Home", "Lights"])
    c._session = FakeSession(STATE_LIST)
    return c


# ── state_to_wire ──────────────────────────────────────────────────────

@pytest.mark.parametrize("state,expected", [
    (None, "GONE"),
    ({"state": "on"}, "ON"),
    ({"state": "off"}, "OFF"),
    ({"state": "unavailable"}, "UNAVAIL"),
    ({"state": "open"}, "ON"),
    ({"state": "opening"}, "ON"),
    ({"state": "closed"}, "OFF"),
    ({"state": "closing"}, "OFF"),
    ({"state": "playing"}, "ON"),
    ({"state": "paused"}, "OFF"),
    ({"state": "idle"}, "OFF"),
    ({"state": ""}, "UNKNOWN"),  # empty/undef state maps to raw token
])
def test_state_to_wire(state, expected):
    assert ha.state_to_wire(state) == expected


# ── detect_control_type ────────────────────────────────────────────────

def test_detect_control_empty_is_toggle():
    assert ha.detect_control_type({}) == "toggle"
    assert ha.detect_control_type(None) == "toggle"


def test_detect_scene_and_script():
    assert ha.detect_control_type({"entity_id": "scene.movie"}) == "scene"
    assert ha.detect_control_type({"entity_id": "script.greet"}) == "momentary"


def test_detect_light_dimmer_by_mode():
    s = {"entity_id": "light.x",
         "attributes": {"supported_color_modes": ["rgb"]}}
    assert ha.detect_control_type(s) == "dimmer"


def test_detect_light_dimmer_by_feature():
    s = {"entity_id": "light.x",
         "attributes": {"supported_features": 1}}
    assert ha.detect_control_type(s) == "dimmer"


def test_detect_plain_light_is_toggle():
    s = {"entity_id": "light.x", "attributes": {}}
    assert ha.detect_control_type(s) == "toggle"


def test_detect_cover_is_dimmer():
    s = {"entity_id": "cover.blinds",
         "attributes": {"supported_features": 0}}
    assert ha.detect_control_type(s) == "dimmer"


def test_detect_fan_without_step_is_toggle():
    s = {"entity_id": "fan.fan", "attributes": {}}
    assert ha.detect_control_type(s) == "toggle"
    s2 = {"entity_id": "fan.fan", "attributes": {"percentage_step": 10}}
    assert ha.detect_control_type(s2) == "dimmer"


# ── brightness_pct ─────────────────────────────────────────────────────

def test_brightness_from_255_scale():
    assert ha.brightness_pct(
        {"attributes": {"brightness": 255}}) == 100
    assert ha.brightness_pct(
        {"attributes": {"brightness": 128}}) == 50


def test_brightness_from_percentage():
    assert ha.brightness_pct({"attributes": {"percentage": 42}}) == 42


def test_brightness_fallbacks():
    assert ha.brightness_pct(None) == 0
    assert ha.brightness_pct({"attributes": {}}) == 0
    assert ha.brightness_pct({"attributes": {"brightness": "zzz"}}) == 0


# ── sanitize ───────────────────────────────────────────────────────────

def test_sanitize_strips_delimiters():
    assert ha.HAClient._sanitize("a|b\r\nc") == "a/b  c"


# ── HAClient protocol framing ──────────────────────────────────────────

def test_cmd_pages(client):
    assert client.cmd_pages() == "HA|PAGES|Home|Lights\r"


def test_cmd_list_full(client):
    out = client.cmd_list()
    assert out == (
        "HA|PAGE|Home\r"
        "HA|01|Lamp|light|ON|dimmer|50\r"
        "HA|02|Plug|switch|OFF|toggle|\r"
        "HA|PAGE|Lights\r"
        "HA|03|Blinds|cover|ON|dimmer|0\r"
        "HA|END\r")


def test_cmd_list_filtered_page(client):
    out = client.cmd_list("Lights")
    assert "PAGE|Home" not in out
    assert "HA|03|Blinds" in out


def test_cmd_on_reports_intended_state(client):
    out = client.cmd_on("01")
    assert out.startswith("OK|01|Lamp|light|ON|dimmer|")
    assert ("light/turn_on", {"entity_id": "light.lamp"}) in \
        client._session.posts


def test_cmd_off_reports_intended_state(client):
    out = client.cmd_off("02")
    assert out.startswith("OK|02|Plug|switch|OFF|toggle|")


def test_cmd_toggle_from_on_turns_off(client):
    out = client.cmd_toggle("01")
    assert out.startswith("OK|01|Lamp|light|OFF|dimmer|0")


def test_cmd_dim_sets_brightness(client):
    out = client.cmd_dim("01", "70")
    assert out.startswith("OK|01|Lamp|light|ON|dimmer|70")
    assert ("light/turn_on", {"entity_id": "light.lamp", "brightness_pct": 70}) in \
        client._session.posts


def test_cmd_dim_zero_turns_off(client):
    out = client.cmd_dim("01", "0")
    assert out.startswith("OK|01|Lamp|light|OFF|dimmer|0")
    assert ("light/turn_off", {"entity_id": "light.lamp"}) in \
        client._session.posts


def test_cmd_dim_badarg(client):
    out = client.cmd_dim("01", "xyz")
    assert out == "ERR|BADARG|01\r"


def test_cmd_unknown_alias(client):
    assert client.cmd_on("99") == "ERR|UNKNOWN|99\r"


def test_cmd_scene_calls_scene_service(client):
    c = ha.HAClient("http://ha.test", "t", [
        {"id": 9, "entity": "scene.movie", "name": "Movie"}],
        ["Home"])
    c._session = FakeSession(STATE_LIST)
    out = c.cmd_scene("9")
    assert out.startswith("OK|09|Movie|scene|OFF|scene|")
    assert ("scene/turn_on", {"entity_id": "scene.movie"}) in c._session.posts


# ── module-level handle / glue ─────────────────────────────────────────

def test_handle_non_ha_returns_none(client):
    assert ha.handle("BRIGHT 50") is None


def test_handle_without_config_is_notconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(ha, "CONFIG_PATH", tmp_path / "missing.conf")
    ha._config_mtime = -1.0  # force a re-read
    assert ha.handle("HA LIST") == "ERR|NOTCONFIGURED\r"


def test_handle_ha_without_subcommand_is_nocmd(monkeypatch, tmp_path, client):
    monkeypatch.setattr(ha, "CONFIG_PATH", tmp_path / "missing.conf")
    monkeypatch.setattr(ha, "_ensure_fresh", lambda: None)
    monkeypatch.setattr(ha, "_client", client)
    assert ha.handle("HA") == "ERR|NOCMD\r"


def test_reload_unconditional_rebuild(client, monkeypatch, tmp_path):
    cfg = tmp_path / "homeassistant.conf"
    cfg.write_text("homeassistant:\n  url: http://ha.test\n  token: t\n")
    state = {}

    def fake_build():
        state["built"] = True
        return client

    monkeypatch.setattr(ha, "CONFIG_PATH", cfg)
    monkeypatch.setattr(ha, "_build_client", fake_build)
    monkeypatch.setattr(ha, "_config_mtime", 0.0)
    ha.reload()
    assert state.get("built") is True