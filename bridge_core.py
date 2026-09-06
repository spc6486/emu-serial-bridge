"""GI-free core: handler loading, line parsing, and command dispatch.

This module has **no** GTK/PyGObject dependency, so the protocol core of the
bridge (loading handler plugins, splitting inbound lines, and dispatching
them to ``COMMANDS[cmd](args, write)`` callables) can be unit-tested on any
machine — including CI hosts that lack ``gi``, ``Gdk``, or an indicator
library. :mod:`emu-serial-bridge` imports these helpers rather than
reimplementing them, so the bridge and the tests exercise the same code.

Two small behavioral notes intentional in the original implementation and
preserved here:

* Command keys are matched case-insensitively (looked up uppercased).
* A command that the caller joinder to the line's first token loses its
  arguments: the registry also tries the *entire* stripped line as a single
  command key (so ``BAT?`` matches even when written ``bat?``), falling back
  to ``ERR UNKNOWN``.

Public API:

* :func:`load_handlers` — scan a directory for ``*.py`` handler plugins,
  load them, and return ``(handlers, commands)``.
* :func:`parse_line` — split one inbound line into ``(cmd_upper, args)``.
* :func:`dispatch` — run one inbound line through a command table.
"""

import importlib.util
from pathlib import Path


def load_handlers(handler_dir, disabled_list=(), log=None, call_init=True):
    """Load all ``*.py`` handler plugins from *handler_dir*.

    Files whose name starts with ``_`` are ignored (same convention as the
    bridge). Each module is imported once; its ``COMMANDS`` mapping is merged
    into a shared command table keyed by uppercase command name. Modules in
    *disabled_list* are still imported (so their ``NAME``/description can be
    shown in the Settings window) but have no commands registered.

    Returns ``(handlers, commands)`` where ``handlers`` maps module stem →
    module object and ``commands`` maps ``UPPERCASE_CMD`` →
    ``(callable, handler_stem)``.

    *log* is an optional ``callable(str)`` used for diagnostics; defaults to
    ``None`` (quiet). *call_init* controls whether each module's ``init()``
    hook is invoked after loading.
    """
    handlers = {}
    commands = {}
    disabled = set(disabled_list)
    if handler_dir and Path(handler_dir).is_dir():
        for path in sorted(Path(handler_dir).glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"handlers.{name}", path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not create spec")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                handlers[name] = mod
                if name not in disabled:
                    for cmd, func in (getattr(mod, "COMMANDS", {}) or {}).items():
                        commands[cmd.upper()] = (func, name)
                if call_init and hasattr(mod, "init"):
                    mod.init()
            except Exception as e:
                if log:
                    log(f"handler load error ({name}): {e}")
    return handlers, commands


def parse_line(line):
    """Split *line* into ``(command, args)``.

    ``command`` is the first whitespace-delimited token, uppercased; ``args``
    is everything after it (or ``""``). Returns ``None`` for a blank line.
    """
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split(None, 1)
    return parts[0].upper(), (parts[1] if len(parts) > 1 else "")


def dispatch(commands, line, write_func):
    """Dispatch one inbound *line* through the *commands* table.

    Tries ``parse_line``'s command token first (allowing args), then the
    entire stripped line as a single command key (covering query commands
    with no args such as ``BAT?``). If neither matches, calls
    ``write_func("ERR UNKNOWN")``.

    Returns ``True`` if a command matched, ``False`` otherwise.
    """
    parsed = parse_line(line)
    if parsed is None:
        return False

    cmd, args = parsed
    pair = commands.get(cmd)
    if pair is not None:
        func, _ = pair
        func(args, write_func)
        return True

    pair = commands.get(line.strip().upper())
    if pair is not None:
        func, _ = pair
        func("", write_func)
        return True

    write_func("ERR UNKNOWN")
    return False


def collect_command_names(handlers, disabled=()):
    """Flat, sorted list of command names across *handlers* for display."""
    names = set()
    disabled = set(disabled)
    for name, mod in handlers.items():
        if name in disabled:
            continue
        names.update((getattr(mod, "COMMANDS", {}) or {}).keys())
    return sorted(names)