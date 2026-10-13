"""`atomic_write_bytes`: unique temp per writer, atomic replace, temp cleaned on failure."""

import os
import stat
import subprocess
import sys

import pytest

from loi.errors import LoiError
from loi.util.fsutil import atomic_write_bytes


def test_writes_and_creates_parent(tmp_path):
    target = tmp_path / "a" / "b" / "f.json"
    atomic_write_bytes(target, b"x")
    assert target.read_bytes() == b"x"
    assert os.listdir(target.parent) == ["f.json"]  # no temp left behind


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_default_mode_is_umask_not_mkstemp_0600(tmp_path):
    old = os.umask(0o022)
    try:
        target = tmp_path / "f"
        atomic_write_bytes(target, b"x")
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        atomic_write_bytes(target, b"y", mode=0o600)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        atomic_write_bytes(target, b"z", mode=0o755)
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
    finally:
        os.umask(old)


def test_temp_removed_on_failed_replace(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_bytes(b"old")

    def boom(a, b):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(LoiError, match="cannot write"):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old" and os.listdir(tmp_path) == ["f"]


def test_temp_removed_on_non_oserror(tmp_path):
    target = tmp_path / "f"
    with pytest.raises(TypeError):
        atomic_write_bytes(target, "not bytes")  # type: ignore[arg-type]
    assert os.listdir(tmp_path) == []  # a Ctrl-C mid-write must not leave a junk file either


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_unwritable_parent_is_loi_error(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        with pytest.raises(LoiError, match="cannot write"):
            atomic_write_bytes(d / "f", b"x")
    finally:
        d.chmod(0o700)


WRITER = """
import pathlib, sys
from loi.util.fsutil import atomic_write_bytes
path = pathlib.Path(sys.argv[1]); tag = sys.argv[2]
for i in range(200):
    atomic_write_bytes(path, (tag * 4000 + "\\n").encode())  # every payload is whole or absent
"""


def test_concurrent_writers_never_fail_or_tear(tmp_path):
    target = tmp_path / "shared.json"
    procs = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(target), t], stderr=subprocess.PIPE)
        for t in "ABCD"
    ]
    for pr in procs:
        _, err = pr.communicate()
        assert pr.returncode == 0, err  # with one fixed `.tmp` name ~20 % of these failed
    data = target.read_bytes()
    assert data in {(t * 4000 + "\n").encode() for t in "ABCD"}  # one writer's bytes, whole
    assert os.listdir(tmp_path) == ["shared.json"]  # no orphan temp files
