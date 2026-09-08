"""pytest plugin: attribute writes under <repo>/data/ to the running test (audit hook)."""
import os, sys, atexit, json
_ROOT = os.getcwd()
_DATA = os.path.join(_ROOT, "data") + os.sep
_cur = {"nodeid": "<no test>"}
_hits: dict[str, set] = {}
def _rec(path):
    try:
        p = os.path.abspath(os.fspath(path))
    except Exception:
        return
    if p.startswith(_DATA):
        _hits.setdefault(os.path.relpath(p, _ROOT), set()).add(_cur["nodeid"])
def _hook(event, args):
    if event == "open":
        path, mode = args[0], args[1]
        if mode and any(c in str(mode) for c in "wax+"):
            _rec(path)
    elif event in ("os.rename", "os.replace", "shutil.move"):
        _rec(args[1])
    elif event == "os.remove" or event == "os.unlink":
        _rec(args[0])
sys.addaudithook(_hook)
def pytest_runtest_setup(item):
    _cur["nodeid"] = item.nodeid
def pytest_runtest_teardown(item):
    _cur["nodeid"] = "<between tests>"
def _dump():
    out = os.environ.get("DATA_WRITE_AUDIT_OUT")
    if not out or not _hits:
        return
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"{out}.{w}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _hits.items()}, f, indent=1)
atexit.register(_dump)
