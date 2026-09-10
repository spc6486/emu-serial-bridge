"""Pytest configuration: make repo root and handlers/ importable.

The bridge ships handlers as loose ``*.py`` modules in ``handlers/`` (no
package), so tests import them directly by module name. This conftest puts
both the repo root (for ``bridge_core``) and ``handlers/`` (for the plugins)
on ``sys.path``.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HANDLERS = _ROOT / "handlers"

for _p in (_ROOT, _HANDLERS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))