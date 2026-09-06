"""Tests for the GI-free protocol core (bridge_core.py)."""

from pathlib import Path

import pytest

import bridge_core as core

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── parse_line ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    ("", None),
    ("   ", None),
    ("\n", None),
    ("VOL 50", ("VOL", "50")),
    ("   BRIGHT 25  ", ("BRIGHT", "25")),
    ("vol", ("VOL", "")),
    ("BAT?", ("BAT?", "")),
    ("HA LIST Home", ("HA", "LIST Home")),
    ("MUTE 1", ("MUTE", "1")),
])
def test_parse_line(line, expected):
    assert core.parse_line(line) == expected


# ── dispatch ───────────────────────────────────────────────────────────

def _make_commands():
    calls = {}

    def cat(args, write):
        calls["cat"] = calls.get("cat", []) + [args]

    def ping(args, write):
        calls["ping"] = True
        write("PONG")

    return {"CAT": (cat, "demo"), "PING": (ping, "demo")}, calls


def test_dispatch_exact_match_passes_args():
    commands, calls = _make_commands()
    replies = []
    handled = core.dispatch(commands, "CAT alpha beta", replies.append)
    assert handled is True
    assert calls["cat"] == ["alpha beta"]


def test_dispatch_case_insensitive():
    commands, calls = _make_commands()
    replies = []
    core.dispatch(commands, "cat 7", replies.append)
    assert calls["cat"] == ["7"]


def test_dispatch_whole_line_as_token():
    # "PING" with no args matches via first-token path; but a no-arg query
    # command like "ping?" would match via full-line fallback.
    commands, calls = _make_commands()
    replies = []
    core.dispatch(commands, "ping", replies.append)
    assert calls["ping"] is True
    assert replies == ["PONG"]


def test_dispatch_unknown_writes_err():
    commands, _ = _make_commands()
    replies = []
    handled = core.dispatch(commands, "NOSUCH 3", replies.append)
    assert handled is False
    assert replies == ["ERR UNKNOWN"]


def test_dispatch_blank_line_is_noop():
    commands, calls = _make_commands()
    replies = []
    handled = core.dispatch(commands, "   ", replies.append)
    assert handled is False
    assert replies == []
    assert "cat" not in calls


def test_dispatch_single_char_cmd():
    commands, calls = _make_commands()
    replies = []
    handled = core.dispatch(commands, "BRI?", replies.append)
    # "BRI?" not a key in this table → falls back to full-line, still unknown
    assert handled is False
    assert replies == ["ERR UNKNOWN"]


# ── load_handlers ──────────────────────────────────────────────────────

def test_load_real_handlers_and_all_commands():
    handlers, commands = core.load_handlers(str(REPO_ROOT / "handlers"))
    assert {"battery", "brightness", "homeassistant", "volume"} <= set(handlers)
    # Every handler contributes COMMANDS (homeassistant is lazy on config).
    assert len(commands) >= 13


def test_load_handlers_disabled_excludes_commands(tmp_path):
    (tmp_path / "a.py").write_text(
        'COMMANDS={"A": lambda a, w: None}\n'
        'NAME="Alpha"\n')
    (tmp_path / "_skip.py").write_text(
        'COMMANDS={"S": lambda a, w: None}\n')
    handlers, commands = core.load_handlers(str(tmp_path), disabled_list=["a"])
    assert "a" in handlers            # still imported for Settings display
    assert "A" not in commands        # but no commands registered
    assert "_skip" not in handlers    # underscore-prefixed files ignored
    assert "S" not in commands


def test_load_handlers_missing_dir_returns_empty():
    handlers, commands = core.load_handlers("/nonexistent/handlers")
    assert handlers == {}
    assert commands == {}


def test_load_handlers_calls_init_by_default(tmp_path):
    (tmp_path / "a.py").write_text(
        'COMMANDS={"A": lambda a, w: None}\n'
        'INIT_CALLS = []\n'
        'def init(config=None):\n'
        '    INIT_CALLS.append(1)\n')
    handlers, _ = core.load_handlers(str(tmp_path))
    assert handlers["a"].INIT_CALLS == [1]


def test_load_handlers_no_init_when_disabled_flag(tmp_path):
    (tmp_path / "a.py").write_text(
        'COMMANDS={"A": lambda a, w: None}\n'
        'INIT_CALLS = []\n'
        'def init(config=None):\n'
        '    INIT_CALLS.append(1)\n')
    handlers, _ = core.load_handlers(str(tmp_path), call_init=False)
    assert handlers["a"].INIT_CALLS == []


def test_load_handlers_error_does_not_crash(tmp_path):
    (tmp_path / "bad.py").write_text("this is not valid python !!\n")
    handlers, commands = core.load_handlers(str(tmp_path), log=print)
    assert "bad" not in handlers
    assert commands == {}


# ── collect_command_names ──────────────────────────────────────────────

def test_collect_command_names():
    handlers = {
        "a": type("M", (), {"COMMANDS": {"ON": None, "OFF": None}})(),
        "b": type("M", (), {"COMMANDS": {"DIM": None}})(),
    }
    assert core.collect_command_names(handlers) == ["DIM", "OFF", "ON"]
    assert core.collect_command_names(handlers, disabled=["a"]) == ["DIM"]