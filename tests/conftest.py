"""Suite-wide guards: no test may touch the real `~/.local/bin/loil` or the real `~/.loil`.

`shim.shim_path()` and `project.loil_home()` read `HOME` / `LOIL_HOME` per call, and `init`
writes both, so a fixture misapplied (or a test forgetting the isolated env) would create
the real shim or the real store. The real HOME is captured at import time, before any
monkeypatch runs; every test also gets `LOIL_HOME` pointed at its own tmp dir.
"""

import os
from pathlib import Path

import pytest

REAL_HOME = Path(os.path.expanduser("~"))
REAL_SHIM = REAL_HOME / ".local" / "bin" / "loil"
REAL_STORE = REAL_HOME / ".loil"
_EXISTED = REAL_SHIM.exists() or REAL_SHIM.is_symlink()
_BYTES = REAL_SHIM.read_bytes() if _EXISTED and REAL_SHIM.is_file() else None


def _tree(root: Path) -> dict:
    """Every path under `root`: files by mtime, directories by their presence alone (their
    mtime moves whenever a child does)."""
    if not root.exists():
        return {}
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns:
            out[str(Path(dp) / n) + os.sep] = None
        for n in fns:
            p = Path(dp) / n
            out[str(p)] = p.stat().st_mtime_ns
    return out


_STORE_TREE = _tree(REAL_STORE)


@pytest.fixture(autouse=True, scope="session")
def real_shim_untouched():
    yield
    if not _EXISTED:
        assert not (REAL_SHIM.exists() or REAL_SHIM.is_symlink()), f"a test created {REAL_SHIM}"
    elif _BYTES is not None:
        assert REAL_SHIM.read_bytes() == _BYTES, f"a test rewrote {REAL_SHIM}"


@pytest.fixture(autouse=True, scope="session")
def real_store_untouched():
    yield
    assert _tree(REAL_STORE) == _STORE_TREE, f"a test touched {REAL_STORE}"


@pytest.fixture(autouse=True)
def store_home(tmp_path, monkeypatch):
    """Every test's central store is its own tmp dir (subprocesses inherit it)."""
    home = tmp_path / "loilhome"
    monkeypatch.setenv("LOIL_HOME", str(home))
    return home
