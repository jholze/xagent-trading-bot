import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def demo_mode_env(monkeypatch):
    """Unit tests run with isolated demo JSON paths when touching data files."""
    monkeypatch.setenv("DEMO_MODE", "1")