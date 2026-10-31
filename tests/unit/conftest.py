"""Make H2 sibling helpers importable as ``h2_port_helpers`` / ``test_h2_*``.

The ported #723 / #735 tests import each other by module name. Pytest's root is
the repo, so this directory has to be on ``sys.path`` before collection imports
those modules.
"""
from __future__ import annotations

import os
import sys

_UNIT = os.path.dirname(os.path.abspath(__file__))
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)
