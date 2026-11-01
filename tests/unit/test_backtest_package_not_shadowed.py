"""Regression: a stray ``tests/backtest.py`` script shadowed the ``backtest``
package once ``tests/`` landed early on sys.path during collection, so the
harness tests failed with "'backtest' is not a package"."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_module_named_backtest_under_tests():
    assert not (ROOT / "tests" / "backtest.py").exists()
    assert not (ROOT / "tests" / "backtest").exists()


def test_backtest_resolves_to_repo_package():
    import backtest

    assert Path(backtest.__file__).resolve() == ROOT / "backtest" / "__init__.py"
    from backtest.harness import regime  # noqa: F401
