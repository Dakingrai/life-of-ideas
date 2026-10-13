from pathlib import Path

import pytest

from loil.errors import LoilError
from loil.record.project import Project, check_session_id, ensure_loil_home, find_root, loil_home

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ID2 = "01BX5ZZKBKACTAV9WEVGEMMVRZ"


def _has_loi_ancestor(p: Path) -> bool:
    return any((a / ".loil" / "config.toml").is_file() for a in Path(p).absolute().parents)


def loil_dir(root: Path, id: str = ID) -> None:
    """A minimal inited project: the committed marker only."""
    (root / ".loil").mkdir(parents=True, exist_ok=True)
    (root / ".loil" / "config.toml").write_text(f'schema = 1\nid = "{id}"\nname = "x"\ncreated = "c"\nloil_version = "v"\n')


def test_loi_dir(tmp_path):
    loil_dir(tmp_path)
    assert find_root(tmp_path) == tmp_path.absolute()
    assert find_root(str(tmp_path)) == tmp_path.absolute()


def test_dotdot_is_folded_before_the_walk(tmp_path):
    loil_dir(tmp_path / "plain")
    (tmp_path / "plain" / "sub").mkdir()
    (tmp_path / "outside").mkdir()
    assert find_root(tmp_path / "plain" / "sub" / ".." / ".." / "outside") is None  # not `plain`


def test_nested_start(tmp_path):
    loil_dir(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_root(sub) == tmp_path.absolute()
    assert find_root(tmp_path / ".loil") == tmp_path.absolute()  # from inside .loil/ itself


def test_no_project(tmp_path):
    if _has_loi_ancestor(tmp_path):
        pytest.skip(f"an ancestor of {tmp_path} has a .loil/config.toml")
    assert find_root(tmp_path) is None
    assert find_root(tmp_path / "missing") is None  # a start that does not exist


def test_bare_loi_dir_is_not_a_root(tmp_path):
    if _has_loi_ancestor(tmp_path):
        pytest.skip(f"an ancestor of {tmp_path} has a .loil/config.toml")
    (tmp_path / ".loil").mkdir()  # half-created, or a clone whose .loil/ is ignored
    assert find_root(tmp_path) is None
    assert find_root(tmp_path / "sub") is None
    (tmp_path / ".loil" / "local.toml").write_text("schema = 1\n")  # per-user file alone: still not
    assert find_root(tmp_path) is None
    (tmp_path / ".loil" / "config.toml").mkdir()  # a directory of that name is not the marker
    assert find_root(tmp_path) is None


def test_nested_loi_is_its_own_project(tmp_path):
    loil_dir(tmp_path)
    inner = tmp_path / "pkg" / "inner"
    loil_dir(inner, ID2)
    assert find_root(inner) == inner
    assert find_root(inner / "deep") == inner  # nearest wins, no inheritance
    assert find_root(tmp_path / "pkg") == tmp_path  # outside the inner one: the outer
    assert (Project.open(inner).root, Project.open(inner).id) == (inner, ID2)
    assert (Project.open(tmp_path / "pkg").root, Project.open(tmp_path / "pkg").id) == (tmp_path, ID)


def test_git_is_ignored(tmp_path):
    if _has_loi_ancestor(tmp_path):
        pytest.skip(f"an ancestor of {tmp_path} has a .loil/config.toml")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert find_root(tmp_path) is None  # a git repo alone is not a project
    (tmp_path / "wt" / ".git").parent.mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /elsewhere\n")
    assert find_root(tmp_path / "wt") is None
    sub = tmp_path / "sub"
    loil_dir(sub)
    assert find_root(sub / "x") == sub  # a .git above a .loil does not move the root up
    assert find_root(tmp_path) is None


def test_repo_paths(tmp_path):
    p = Project(tmp_path)
    assert p.loil_dir == tmp_path / ".loil"
    assert p.config_path == tmp_path / ".loil" / "config.toml"
    assert p.local_path == tmp_path / ".loil" / "local.toml"
    assert p.id is None and p.home is None
    assert not hasattr(p, "store_dir") and not hasattr(p, "from_cwd") and not hasattr(p, "manifest_path")
    assert not hasattr(p, "record_dir") and not hasattr(p, "sessions_dir")
    assert not hasattr(p, "events_path") and not hasattr(p, "board_dir")


def test_check_session_id():
    assert check_session_id("6d086dbc-0dca-41be-8cf2-b9aedf4cae25") == "6d086dbc-0dca-41be-8cf2-b9aedf4cae25"
    assert check_session_id("x" * 200)
    for bad in ("", ".", "..", "a/b", "a\\b", "a\x00b", "../x", "x" * 201, None, 3):
        with pytest.raises(LoilError):
            check_session_id(bad)


def test_loi_home_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOIL_HOME", str(tmp_path / "x"))
    assert loil_home() == tmp_path / "x"
    monkeypatch.delenv("LOIL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert loil_home() == tmp_path / "h" / ".loil"
    monkeypatch.setenv("LOIL_HOME", "")  # empty counts as unset
    assert loil_home() == tmp_path / "h" / ".loil"


def test_loi_home_precedence_env_pin_default(tmp_path, monkeypatch):
    pin = tmp_path / "pin"
    monkeypatch.setenv("LOIL_HOME", str(tmp_path / "env"))
    assert loil_home(pin) == tmp_path / "env"
    assert Project(tmp_path, id=ID, home=pin).home_dir == tmp_path / "env" / "projects" / ID
    monkeypatch.delenv("LOIL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert loil_home(pin) == pin
    assert loil_home(str(pin)) == pin
    assert Project(tmp_path, id=ID, home=str(pin)).home_dir == pin / "projects" / ID
    assert Project(tmp_path, id=ID).home_dir == tmp_path / "h" / ".loil" / "projects" / ID
    assert loil_home(None) == tmp_path / "h" / ".loil" and loil_home("") == tmp_path / "h" / ".loil"


def test_loi_home_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOIL_HOME", "rel")
    assert loil_home() == tmp_path / "rel"
    monkeypatch.setenv("LOIL_HOME", "~/x")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert loil_home() == tmp_path / "x"
    monkeypatch.delenv("LOIL_HOME")
    assert loil_home("rel2") == tmp_path / "rel2" and loil_home("~/y") == tmp_path / "y"
    monkeypatch.setenv("HOME", ".")
    assert loil_home() == tmp_path / ".loil" and loil_home().is_absolute()
    monkeypatch.setenv("HOME", "")
    assert loil_home().is_absolute()


def test_ensure_loi_home_is_private(tmp_path, store_home):
    import stat

    assert ensure_loil_home() == store_home
    assert (store_home / "projects").is_dir()
    for d in (store_home, store_home / "projects"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert ensure_loil_home() == store_home  # idempotent


def test_central_paths_need_an_id(tmp_path, store_home):
    p = Project(tmp_path)
    for name in ("home_dir", "log_path"):
        with pytest.raises(LoilError, match="loil init"):
            getattr(p, name)
    with pytest.raises(LoilError, match="loil init"):
        p.artifacts_dir("claude-code", "s")
    assert p.loil_home == store_home  # the root itself never needs an id
    p = Project(tmp_path, id=ID)
    assert p.home_dir == store_home / "projects" / ID
    assert p.log_path == store_home / "projects" / ID / "loil.log"
    assert not hasattr(p, "cache_dir")
    assert p.artifacts_dir("claude-code", "S1") == store_home / "projects" / ID / "sessions" / "claude-code" / "S1"
    for bad in ("../x", "a/b", "", ".", "a\x00b", "x" * 201):
        with pytest.raises(LoilError):
            p.artifacts_dir("claude-code", bad)
    for bad in ("Claude Code", "", "../x", "claude_code"):
        with pytest.raises(LoilError, match="adapter name"):
            p.artifacts_dir(bad, "S1")
    assert not store_home.exists()  # paths only; nothing created


def test_open_reads_id_from_config_only(tmp_path, store_home):
    from loil.record import config

    with pytest.raises(LoilError, match="not a loil project"):
        Project.open(tmp_path)  # neither file: not a root at all
    config.save_config(Project(tmp_path), config.Config(id=ID, name="x", created="c", loil_version="v"))
    p = Project.open(str(tmp_path))  # config.toml only: a fresh clone
    assert p == Project(tmp_path, id=ID) and p.root == tmp_path and p.home is None
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert Project.open(sub).root == tmp_path  # walks up
    pin = tmp_path / "pin"
    (tmp_path / ".loil" / "local.toml").write_text(f'schema = 1\nid = "{ID2}"\nhome = "{pin}"\n')  # a pre-13d pin
    p = Project.open(tmp_path)
    assert p.id == ID and p.home == pin  # the id comes from config.toml only; `home` still from local.toml
    assert p.home_dir == store_home / "projects" / ID  # …and $LOIL_HOME wins over `home`
    (tmp_path / ".loil" / "config.toml").unlink()
    with pytest.raises(LoilError, match="not a loil project"):
        Project.open(tmp_path)  # local.toml alone (a branch that predates `loil init`) is not a root
    config.save_config(Project(tmp_path), config.Config(id=ID, name="x", created="c", loil_version="v"))
    config.save_local(Project(tmp_path), config.Local(home=str(pin)))
    assert Project.open(tmp_path) == Project(tmp_path, id=ID, home=pin)
    (tmp_path / ".loil" / "config.toml").write_text("schema = 1\n")  # a root whose config names no id
    with pytest.raises(LoilError, match="id must be a string"):  # a SchemaError, not "not a project"
        Project.open(tmp_path)


def test_open_outside_a_project(tmp_path, monkeypatch):
    if _has_loi_ancestor(tmp_path):
        pytest.skip(f"an ancestor of {tmp_path} has a .loil/config.toml")
    with pytest.raises(LoilError, match=r"not a loil project here \(no .loil/config.toml in this directory or above\); run `loil init`"):
        Project.open(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LoilError, match="not a loil project"):
        Project.open()


def test_str_coercion_and_absolute_root(tmp_path, monkeypatch):
    p = Project(str(tmp_path), home=str(tmp_path / "h"))
    assert p.root == tmp_path and isinstance(p.root, Path)
    assert p.home == tmp_path / "h" and isinstance(p.home, Path)
    monkeypatch.chdir(tmp_path)
    p = Project("sub")
    assert p.root == tmp_path / "sub" and p.root.is_absolute()  # a later chdir can't move .loil/
    assert Project(tmp_path, home="").home is None and Project(tmp_path, home=Path("")).home is None


def test_ensure_loi_home_errors_are_loi_errors(tmp_path, monkeypatch):
    f = tmp_path / "afile"
    f.write_text("x")
    monkeypatch.setenv("LOIL_HOME", str(f))
    with pytest.raises(LoilError, match="cannot create"):
        ensure_loil_home()
