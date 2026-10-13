import json
import stat
import subprocess
import sys

import pytest

from loil.record import registry
from loil.errors import LoilError
from loil.record.project import Project

A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
B = "01BX5ZZKBKACTAV9WEVGEMMVRZ"
KEYS = ["schema", "id", "name", "inited_at", "root", "roots"]


def test_missing_file_is_empty(store_home):
    assert registry.projects() == []
    assert not store_home.exists()
    assert registry.unregister(A) is False
    assert not store_home.exists()


def test_add_update_unregister(tmp_path, store_home):
    r1 = tmp_path / "one"
    assert registry.register(Project(r1, id=A)) == "added"
    assert registry.entry_path(Project(r1, id=A)) == store_home / "projects" / A / "project.json"
    e = json.loads((store_home / "projects" / A / "project.json").read_text())
    assert list(e) == KEYS and e["schema"] == 1 and e["name"] == "one"
    assert e["root"] == str(r1) and e["roots"] == [str(r1)] and "agents" not in e
    assert e["inited_at"].endswith("Z")
    assert registry.register(Project(r1, id=A)) is None  # unchanged, byte for byte
    assert json.loads((store_home / "projects" / A / "project.json").read_text()) == e
    r2 = tmp_path / "moved"
    assert registry.register(Project(r2, id=A)) == "updated (root)"
    e = registry.projects()[0]
    assert e["root"] == str(r2) and e["name"] == "moved" and e["roots"] == [str(r1), str(r2)]
    assert registry.register(Project(r1, id=A)) == "updated (root)"  # back: roots does not grow
    assert registry.projects()[0]["roots"] == [str(r1), str(r2)]
    assert registry.register(Project(tmp_path / "b", id=B)) == "added"
    assert [e["id"] for e in registry.projects()] == [B, A]  # sorted by name: "b" < "one"
    assert registry.unregister(A) is True
    assert [e["id"] for e in registry.projects()] == [B]
    assert (store_home / "projects" / A).is_dir()  # the store itself is never removed
    assert sorted(x.name for x in (store_home / "projects" / B).iterdir()) == ["project.json"]  # no temp left


def test_without_id_raises(tmp_path, store_home):
    with pytest.raises(LoilError, match="loil init"):
        registry.register(Project(tmp_path))
    assert not store_home.exists()


@pytest.mark.parametrize(
    "bad",
    [
        "{",
        "[]",
        '{"schema": 2, "id": "%s"}' % A,
        '{"schema": 1, "id": "other"}',
        '{"schema": 1, "id": "%s", "roots": 3}' % A,
    ],
)
def test_malformed_raises_naming_file(tmp_path, store_home, bad):
    f = store_home / "projects" / A / "project.json"
    f.parent.mkdir(parents=True)
    f.write_text(bad)
    with pytest.raises(LoilError, match="project.json"):
        registry.register(Project(tmp_path, id=A))
    with pytest.raises(LoilError, match="project.json"):
        registry.projects()
    assert f.read_text() == bad
    (store_home / "projects" / "stray-dir-without-file").mkdir()  # a store dir alone is not an entry
    f.unlink()
    assert registry.projects() == []


def test_honours_loi_home_env_over_pin(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("LOIL_HOME", str(other))
    registry.register(Project(tmp_path, id=A, home=tmp_path / "pinned"))
    assert (other / "projects" / A / "project.json").is_file()
    assert not (tmp_path / "pinned").exists()
    assert [e["id"] for e in registry.projects()] == [A]
    assert [e["id"] for e in registry.projects(home=tmp_path / "pinned")] == [A]  # env still wins over a pin


def test_pin_used_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LOIL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    pin = tmp_path / "pinned"
    registry.register(Project(tmp_path / "r", id=A, home=pin))
    assert (pin / "projects" / A / "project.json").is_file() and not (tmp_path / "h").exists()
    assert registry.projects() == []  # default ~/.loil has nothing
    assert [e["id"] for e in registry.projects(home=pin)] == [A]
    assert registry.unregister(A) is False and registry.unregister(A, home=pin) is True


WRITER = """
import pathlib, sys
from loil.record import registry
from loil.record.project import Project
root = pathlib.Path(sys.argv[1]); tag = sys.argv[2]
for i in range(25):  # 25 distinct ids per process, plus 25 hits on one shared id (same-id race)
    own = "01ARZ3NDEKTSV4RRFFQ69G5F%s%s" % (tag, "0123456789ABCDEFGHJKMNPQR"[i])
    assert registry.register(Project(root / f"{tag}{i}", id=own)) == "added"
    registry.register(Project(root / f"shared{tag}{i}", id="01ARZ3NDEKTSV4RRFFQ69G5FAV"))
"""


def test_concurrent_registers_lose_nothing(tmp_path, store_home):
    procs = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), t], stderr=subprocess.PIPE)
        for t in "ABCD"
    ]
    for pr in procs:
        _, err = pr.communicate()
        assert pr.returncode == 0, err
    assert len(registry.projects()) == 101
    shared = [e for e in registry.projects() if e["id"] == A]
    assert len(shared[0]["roots"]) == 100  # every register of the shared id landed
    assert all(x.name == "project.json" for x in (store_home / "projects" / A).iterdir())  # no temp files


def test_central_dirs_are_private(tmp_path, store_home):
    registry.register(Project(tmp_path, id=A))
    for d in (store_home, store_home / "projects", store_home / "projects" / A):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
