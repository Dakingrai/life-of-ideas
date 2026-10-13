"""`loi init` against temp folders (`git init`-ed only where `.gitignore` or `check-ignore`
is under test). HOME, LOI_HOME and git config are always isolated."""

import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loi.record import config, registry
from loi.util import ulid
from loi.commands.init import GITIGNORE_ALWAYS, INSTRUCTIONS as INSTRUCTIONS_LINE, NO_AGENT_ROW, NO_GIT_ROW, run_init
from loi.errors import LoiError
from loi.record.project import Project

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shim")

SETTINGS_LINE = ".claude/settings.local.json"
LINES = (*GITIGNORE_ALWAYS, SETTINGS_LINE)  # what a checkout's .gitignore ends with
INSTRUCTIONS = ("Commit .loi/config.toml and .gitignore", "runs `loi init` once per clone")
CC = "claude-code"
HOST_BIN = "/usr/bin:/bin"  # git and sh, and no coding agent: see the `env` fixture


def _has_loi_ancestor(p: Path) -> bool:
    return any((a / ".loi" / "config.toml").is_file() for a in Path(p).absolute().parents)


@pytest.fixture
def env(tmp_path, monkeypatch, store_home):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("UV_CACHE_DIR", "GIT_DIR", "GIT_WORK_TREE", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    # a fake `claude` on PATH, and *only* the fake: which adapters `init` enables must be the
    # test's choice, not a fact about the developer's laptop. `/usr/bin:/bin` carries the
    # `git` and `sh` these tests shell out to and nothing else.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{HOST_BIN}")
    return home


@pytest.fixture
def repo(tmp_path, env):
    """A git repo: what the `.gitignore` and `check-ignore` tests need; loi itself does not."""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


@pytest.fixture
def plain(tmp_path, env):
    """A folder that is not a git repo, with no project above it."""
    d = tmp_path / "plain"
    d.mkdir()
    if _has_loi_ancestor(d):
        pytest.skip(f"an ancestor of {d} has a .loi/config.toml")
    return d


def init(cwd, **kw):
    out, err = io.StringIO(), io.StringIO()
    kw.setdefault("backfill", False)
    code = run_init(cwd=cwd, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def rows(out: str) -> dict[str, str]:
    """The summary's `  <name><pad> <what>` lines by name; the header, `hook command:` and
    the instructions are not rows. A name never holds a space; a `what` may."""
    got = {}
    for line in out.splitlines():
        if not line.startswith("  ") or line.startswith("  hook command:"):
            continue
        name, _, what = line[2:].partition(" ")
        got[name] = what.strip()
    return got


FIXED_ROWS = (".loi/config.toml", ".loi/local.toml", ".gitignore", "registry")


def assert_unchanged(out: str, but: dict[str, str] | None = None) -> None:
    """Every row is `unchanged` except `but`. Pins the *set* of rows, not how many."""
    r = rows(out)
    store = [n for n in r if n.endswith("/")]
    assert len(store) == 1, r
    hooks = [n for n in r if n == SETTINGS_LINE or n == NO_AGENT_ROW[0]]
    assert len(hooks) == 1, r
    assert set(r) == {*FIXED_ROWS, *store, *hooks}, r
    expect = {name: "unchanged" for name in r}
    expect.update(but or {})
    assert r == expect, out


def snapshot(root: Path) -> dict:
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for n in fns:
            p = Path(dp) / n
            out[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def settings(root):
    return json.loads((root / ".claude" / "settings.local.json").read_text())


def hook_cmd(root, event):
    return settings(root)["hooks"][event][0]["hooks"][0]["command"]


def no_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))


def test_fresh(repo, env, store_home):
    code, out, err = init(repo)
    assert code == 0 and err == ""
    lines = (repo / ".loi" / "config.toml").read_text().splitlines()
    assert len(lines) == 5
    cfg = config.load_config(Project(repo))
    assert ulid.is_valid(cfg.id)
    assert lines == [
        "schema = 1", f'id = "{cfg.id}"', 'name = "repo"', f'created = "{cfg.created}"', 'loi_version = "0.2.1"'
    ]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", cfg.created)
    assert (repo / ".loi" / "local.toml").read_text() == f'schema = 1\nhome = "{store_home}"\n'
    p = Project.open(repo)
    assert p.id == cfg.id and p.home == store_home
    assert sorted(x.name for x in (repo / ".loi").iterdir()) == ["config.toml", "local.toml"]
    # central side
    assert p.home_dir == store_home / "projects" / cfg.id
    assert sorted(x.relative_to(store_home).as_posix() for x in store_home.rglob("*")) == sorted(
        ["projects", f"projects/{cfg.id}", f"projects/{cfg.id}/loi.log", f"projects/{cfg.id}/sessions", f"projects/{cfg.id}/project.json"]
    )
    for d in (store_home, store_home / "projects", p.home_dir, p.home_dir / "sessions"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    (e,) = registry.projects()
    assert list(e) == ["schema", "id", "name", "inited_at", "root", "roots"]
    assert (e["id"], e["root"], e["name"], e["roots"]) == (cfg.id, str(repo), "repo", [str(repo)])
    assert f"{store_home}/projects/{cfg.id}/" in out and "registry" in out and "added" in out
    assert (repo / ".gitignore").read_bytes() == "".join(f"{line}\n" for line in LINES).encode()
    assert out.endswith(f"{INSTRUCTIONS_LINE}\n")
    shim = env / ".local" / "bin" / "loi"
    assert shim.is_file() and os.access(shim, os.X_OK)
    assert settings(repo) == {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": f"{shim} hook claude-code session-start"}]}],
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": f"{shim} hook claude-code session-end", "timeout": 30}]}
            ],
            "PreCompact": [
                {"hooks": [{"type": "command", "command": f"{shim} hook claude-code pre-compact", "timeout": 30}]}
            ],
        },
    }
    assert "permissions" not in settings(repo)  # loi asks the agent to run nothing
    assert rows(out)[SETTINGS_LINE] == "hooks installed"
    assert str(shim).startswith(str(env)) and ".venv" not in str(shim)
    assert "created" in out and all(s in out for s in INSTRUCTIONS)
    assert cfg.id in out and "hook command: ~/.local/bin/loi" in out
    assert not (env / ".loi").exists()  # LOI_HOME set: nothing under ~


def test_second_run_byte_identical(repo, env, store_home):
    assert init(repo)[0] == 0
    before = snapshot(repo), snapshot(store_home)
    shim_stat = os.stat(env / ".local" / "bin" / "loi")
    code, out, err = init(repo)
    assert code == 0 and err == ""
    assert (snapshot(repo), snapshot(store_home)) == before
    assert os.stat(env / ".local" / "bin" / "loi").st_mtime_ns == shim_stat.st_mtime_ns
    assert "created" not in out and "added" not in out and "updated" not in out
    assert_unchanged(out)
    assert all(s in out for s in INSTRUCTIONS)


def test_no_claude_on_path_means_no_hooks(repo, env, monkeypatch, tmp_path, store_home):
    no_claude(monkeypatch, tmp_path)
    code, out, err = init(repo)
    assert code == 0
    assert "no supported coding agent found on PATH" in err and "claude" in err and "loi init" in err
    local_text = (repo / ".loi" / "local.toml").read_text()
    assert local_text == f'schema = 1\nhome = "{store_home}"\n'
    assert not (repo / ".claude").exists()  # no settings file created
    assert SETTINGS_LINE in (repo / ".gitignore").read_text().splitlines()  # ignored regardless
    assert rows(out)["hooks"] == "skipped (no coding agent on PATH)"
    assert (env / ".local" / "bin" / "loi").is_file()  # the shim is still there for later
    # the warning repeats on every run while nothing is found…
    code, out, err = init(repo)
    assert code == 0 and "no supported coding agent" in err
    assert_unchanged(out, {"hooks": "skipped (no coding agent on PATH)"})
    # …and once claude appears, a plain re-run installs its hooks; local.toml is untouched
    monkeypatch.setenv("PATH", f"{tmp_path / 'fakebin'}:{HOST_BIN}")
    code, out, err = init(repo)
    assert code == 0 and err == ""
    assert_unchanged(out, {SETTINGS_LINE: "hooks installed"})
    assert (repo / ".loi" / "local.toml").read_text() == local_text
    assert hook_cmd(repo, "SessionStart").endswith(" hook claude-code session-start")
    # the binary vanishes again: the settings file marks the adapter enabled, so its hooks
    # stay repaired, the row says unchanged and there is no warning
    no_claude(monkeypatch, tmp_path)
    before = snapshot(repo)
    code, out, err = init(repo)
    assert code == 0 and err == ""
    assert_unchanged(out)
    assert snapshot(repo) == before and hook_cmd(repo, "SessionStart").endswith(" hook claude-code session-start")


def test_a_settings_file_enables_the_adapter_without_its_binary(repo, env, monkeypatch, tmp_path):
    """Claude Code's alias install is not on PATH; its settings file is the better signal."""
    no_claude(monkeypatch, tmp_path)
    s = repo / ".claude" / "settings.local.json"
    s.parent.mkdir()
    s.write_text('{"permissions": {"allow": ["Bash(ls *)"]}}\n')
    code, out, err = init(repo)
    assert code == 0 and err == ""
    assert rows(out)[SETTINGS_LINE] == "hooks installed" and "hooks" not in rows(out)
    assert hook_cmd(repo, "SessionStart").endswith(" hook claude-code session-start")
    assert json.loads(s.read_text())["permissions"] == {"allow": ["Bash(ls *)"]}  # untouched


def test_foreign_hooks_survive_and_stale_loi_entries_are_repaired(repo, env):
    s = repo / ".claude" / "settings.local.json"
    s.parent.mkdir()
    foreign = {"matcher": "startup", "hooks": [{"type": "command", "command": "/usr/bin/motd"}]}
    s.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls *)"]},
                "hooks": {
                    "SessionStart": [
                        foreign,
                        {"hooks": [{"type": "command", "command": "/home/u/.local/bin/loi hook claude-code session-start"}]},
                        {"hooks": [{"type": "command", "command": "/old/.venv/bin/loi hook claude-code session-start"}]},
                    ],
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": "/old/.venv/bin/loi hook claude-code session-end", "timeout": 10}]}
                    ],
                },
            },
            indent=2,
        )
        + "\n"
    )
    code, out, _ = init(repo)
    assert code == 0 and rows(out)[SETTINGS_LINE] == "hooks installed"  # PreCompact added while the stale ones are repaired
    d = settings(repo)
    assert d["permissions"] == {"allow": ["Bash(ls *)"]}
    assert d["hooks"]["SessionStart"][0] == foreign  # not ours: untouched
    shim = str(env / ".local" / "bin" / "loi")
    starts = [e["command"] for g in d["hooks"]["SessionStart"] for e in g["hooks"] if "loi hook" in e["command"]]
    assert starts == [f"{shim} hook claude-code session-start"]
    assert len(d["hooks"]["SessionStart"]) == 2  # the first stale entry rewritten in place, the second removed
    assert d["hooks"]["SessionEnd"] == [
        {"hooks": [{"type": "command", "command": f"{shim} hook claude-code session-end", "timeout": 30}]}
    ]
    assert d["hooks"]["PreCompact"] == [
        {"hooks": [{"type": "command", "command": f"{shim} hook claude-code pre-compact", "timeout": 30}]}
    ]
    before = snapshot(repo)
    assert init(repo)[0] == 0 and snapshot(repo) == before


def test_invalid_settings_json_exit_1_untouched(repo, env):
    s = repo / ".claude" / "settings.local.json"
    s.parent.mkdir()
    s.write_bytes(b"{\n")
    code, out, err = init(repo)
    assert code == 1 and out == ""
    assert "settings.local.json" in err and "JSON" in err
    assert s.read_bytes() == b"{\n"
    s.write_bytes(b"{}\n")  # after the user fixes it, init completes
    assert init(repo)[0] == 0


@pytest.mark.parametrize(
    "existing, expect_prefix",
    [
        (None, b""),
        (b"node_modules/", b"node_modules/\n"),
        (b"a\r\nb\r\n", b"a\r\nb\r\n"),
        (b"\xef\xbb\xbfx\n", b"\xef\xbb\xbfx\n"),
    ],
)
def test_gitignore_variants(repo, env, existing, expect_prefix):
    if existing is not None:
        (repo / ".gitignore").write_bytes(existing)
    assert init(repo)[0] == 0
    assert (repo / ".gitignore").read_bytes() == expect_prefix + "".join(f"{line}\n" for line in LINES).encode()


def test_schema_99_exit_1_nothing_written(repo, env, store_home):
    (repo / ".loi").mkdir()
    text = 'schema = 99\nid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"\n'
    (repo / ".loi" / "config.toml").write_text(text)
    code, out, err = init(repo)
    assert code == 1 and "99" in err and out == ""
    assert (repo / ".loi" / "config.toml").read_text() == text
    assert sorted(p.name for p in (repo / ".loi").iterdir()) == ["config.toml"]
    assert not (repo / ".gitignore").exists() and not (repo / ".claude").exists()
    assert not (env / ".local").exists() and not store_home.exists()


def test_init_honours_pinned_home(repo, env, monkeypatch):
    pin = env.parent / "pinned-store"
    (repo / ".loi").mkdir()
    (repo / ".loi" / "local.toml").write_text(f'schema = 1\nhome = "{pin}"\n')
    monkeypatch.delenv("LOI_HOME")
    code, out, err = init(repo)
    assert code == 0 and err == ""
    cfg = config.load_config(Project(repo))
    assert (pin / "projects" / cfg.id / "loi.log").exists()  # the pin, not ~/.loi
    assert not (env / ".loi").exists()
    assert config.load_local(Project(repo)).home == str(pin) and "home =" not in out  # pin kept, not re-pinned
    assert Project.open(repo).home_dir == pin / "projects" / cfg.id
    assert_unchanged(init(repo)[1])


def test_home_flag_pins_an_explicit_store(repo, env, tmp_path, store_home, monkeypatch):
    monkeypatch.delenv("LOI_HOME")  # the flag is a pin; a set $LOI_HOME would still win at resolve time
    chosen = tmp_path / "chosen-store"  # no space: the summary row parser splits on the first one
    code, out, err = init(repo, home=str(chosen))
    assert code == 0 and err == ""
    assert config.load_local(Project(repo)).home == str(chosen)
    assert (chosen / "projects" / Project.open(repo).id / "loi.log").exists()
    assert not (env / ".loi").exists() and f"home = {chosen}" in out
    code, out, err = init(repo)  # without the flag the pin holds
    assert code == 0 and config.load_local(Project(repo)).home == str(chosen)
    assert_unchanged(out)
    monkeypatch.setenv("LOI_HOME", str(store_home))  # the env wins again and re-pins
    code, out, err = init(repo)
    assert code == 0 and config.load_local(Project(repo)).home == str(store_home) and "created" in out


def test_from_subdirectory_reinits_nearest_project(repo, env):
    assert init(repo)[0] == 0
    before = snapshot(repo)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    code, out, err = init(sub)
    assert code == 0 and err == "" and f"loi init: {repo}" in out
    assert_unchanged(out)
    assert not (sub / ".loi").exists() and not (repo / "a" / ".loi").exists()
    assert snapshot(repo) == before  # root vs subdir: same result


def test_subdirectory_of_a_git_repo_without_loi_roots_at_cwd(repo, env):
    src = repo / "src"
    src.mkdir()
    code, out, err = init(src)
    assert code == 0 and f"loi init: {src}" in out  # git is not consulted: cwd is the root
    assert (src / ".loi" / "config.toml").is_file() and not (repo / ".loi").exists()
    assert (src / ".gitignore").read_bytes() == "".join(f"{line}\n" for line in LINES).encode()


def test_git_file_root_with_bogus_gitdir(tmp_path, env):
    r = tmp_path / "wt"
    r.mkdir()
    (r / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    code, out, err = init(r)
    assert code == 0 and err == ""  # check-ignore's exit 128 is swallowed silently
    assert (r / ".loi" / "config.toml").exists()
    assert (r / ".gitignore").is_file()  # a .git file is a checkout on disk, whatever git thinks of it


def test_plain_folder_inits_at_cwd(plain, env, store_home):
    code, out, err = init(plain)
    assert code == 0 and err == "", err
    assert f"loi init: {plain}" in out
    assert sorted(x.name for x in (plain / ".loi").iterdir()) == ["config.toml", "local.toml"]
    assert not (plain / ".git").exists() and not (plain / ".gitignore").exists()
    assert rows(out)[".gitignore"] == NO_GIT_ROW
    p = Project.open(plain)
    assert (store_home / "projects" / p.id / "loi.log").exists()
    assert_unchanged(init(plain)[1], {".gitignore": NO_GIT_ROW})


def test_bare_loi_dir_is_completed_by_init(plain, env):
    (plain / ".loi").mkdir()  # half-created: no config.toml, so not a root — until init fills it
    code, out, err = init(plain)
    assert code == 0 and err == "" and f"loi init: {plain}" in out
    assert (plain / ".loi" / "config.toml").is_file()
    sub = plain / "sub"
    sub.mkdir()
    assert init(sub)[1].startswith(f"loi init: {plain}\n")  # …and now it is the root above sub


def test_nested_loi_is_independent(plain, env, store_home):
    assert init(plain)[0] == 0
    outer = Project.open(plain)
    sub = plain / "sub"
    sub.mkdir()
    assert Project.open(sub).id == outer.id  # before: sub is inside the outer project
    code, out, err = init(sub)
    assert code == 0 and f"loi init: {plain}" in out and not (sub / ".loi").exists()  # nearest wins
    (sub / ".loi").mkdir()
    (sub / ".loi" / "config.toml").write_text('schema = 1\nid = "01BX5ZZKBKACTAV9WEVGEMMVRZ"\nname = "sub"\ncreated = "c"\nloi_version = "v"\n')
    code, out, err = init(sub)
    assert code == 0 and err == "" and f"loi init: {sub}" in out and "created" in out
    inner = Project.open(sub)
    assert inner.root == sub and inner.id != outer.id
    (sub / "deep").mkdir()
    (plain / "other").mkdir()
    assert Project.open(sub / "deep").id == inner.id  # nearest wins…
    assert Project.open(plain).id == outer.id and Project.open(plain / "other").id == outer.id  # …no inheritance
    assert sorted(x.name for x in (store_home / "projects").iterdir()) == sorted([outer.id, inner.id])
    before = snapshot(plain / ".loi")
    assert init(sub)[0] == 0 and snapshot(plain / ".loi") == before  # re-init of sub leaves plain alone


def test_check_ignore_warning(repo, env):
    (repo / ".gitignore").write_bytes(b".loi/\n")
    code, out, err = init(repo)
    assert code == 0
    assert ".loi/config.toml is ignored by .gitignore:1 (.loi/)" in err
    assert "commit it or a clone will not be a loi project" in err
    assert (repo / ".gitignore").read_bytes().startswith(b".loi/\n.loi/local.toml\n")


def test_git_missing_from_path_is_silent(repo, env, monkeypatch, tmp_path):
    init(repo)
    monkeypatch.setenv("PATH", str(tmp_path / "fakebin"))  # claude yes, git no
    code, out, err = init(repo)
    assert code == 0 and err == "" and "unchanged" in out


def test_tracked_config_no_warning(repo, env):
    (repo / ".gitignore").write_bytes(b".loi/\n")
    init(repo)
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".loi/config.toml"], check=True)
    _, _, err = init(repo)
    assert err == ""  # tracked → collaborators do see it


def test_moved_checkout_keeps_id_and_updates_registry(repo, env, tmp_path, store_home):
    init(repo)
    id0 = config.load_config(Project(repo)).id
    moved = tmp_path / "moved"
    shutil.copytree(repo, moved, symlinks=True)
    code, out, err = init(moved)
    assert code == 0 and err == ""
    assert config.load_config(Project(moved)).id == id0
    assert_unchanged(out, {"registry": "updated (root)"})
    (e,) = registry.projects()
    assert e["root"] == str(moved) and e["roots"] == [str(repo), str(moved)]
    assert len(list((store_home / "projects").iterdir())) == 1  # one central store
    assert init(moved)[0] == 0 and registry.projects() == [e]


def test_reinit_after_config_deleted_warns_with_old_id_from_the_registry(repo, env, store_home):
    init(repo)
    old = Project.open(repo)
    (repo / ".loi" / "config.toml").unlink()
    code, out, err = init(repo)
    assert code == 0
    new = Project.open(repo)
    assert new.id != old.id
    assert f"previously project {old.id}" in err and new.id in err and "still exists" in err
    assert sorted(x.name for x in (store_home / "projects").iterdir()) == sorted([old.id, new.id])


def test_user_edited_name_survives_reinit(repo, env, store_home):
    init(repo)
    path = repo / ".loi" / "config.toml"
    text = path.read_text().replace('name = "repo"', 'name = "My Idea"')
    path.write_text(text)
    code, out, err = init(repo)
    assert code == 0 and err == "" and path.read_text() == text
    assert_unchanged(out)
    assert config.load_config(Project(repo)).name == "My Idea"


def test_branch_without_config_is_not_a_root(repo, env, store_home):
    init(repo)
    (repo / ".loi" / "config.toml").unlink()  # e.g. a branch that predates `loi init`
    with pytest.raises(LoiError, match="not a loi project"):  # local.toml alone is not the marker
        Project.open(repo)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_unwritable_loi_home_exits_1_after_repo_side(repo, env, store_home):
    store_home.mkdir()
    store_home.chmod(0o500)
    try:
        code, out, err = init(repo)
        assert code == 1 and out == ""
        assert str(store_home) in err and "cannot create" in err
        p = Project.open(repo)  # repo side is done, and pinned
        assert p.id is not None and p.home == store_home
        assert not (repo / ".gitignore").exists() and not (repo / ".claude").exists()
    finally:
        store_home.chmod(0o700)
    code, out, err = init(repo)  # re-run repairs
    assert code == 0 and err == ""
    assert "unchanged" in out and p.log_path.exists() and registry.projects()[0]["id"] == p.id


def test_relative_loi_home_is_pinned_absolute(repo, env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOI_HOME", "rel")
    code, out, err = init(repo)
    assert code == 0 and err == ""
    local = config.load_local(Project(repo))
    assert local.home == str(tmp_path / "rel") and (tmp_path / "rel" / "projects" / Project.open(repo).id).is_dir()
    assert f"home = {tmp_path / 'rel'}" in out
    monkeypatch.setenv("LOI_HOME", str(tmp_path / "other"))  # env wins: re-pinned
    code, out, err = init(repo)
    assert code == 0 and f"home = {tmp_path / 'other'}" in out and "created" in out
    assert config.load_local(Project(repo)).home == str(tmp_path / "other")
    monkeypatch.delenv("LOI_HOME")  # no env: the pin holds
    code, out, err = init(repo)
    assert code == 0
    assert_unchanged(out)
    assert config.load_local(Project(repo)).home == str(tmp_path / "other")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write /.local")
def test_empty_home_and_no_loi_home(repo, env, monkeypatch, tmp_path):
    monkeypatch.delenv("LOI_HOME")
    code, out, err = init(repo)  # default store under a fresh HOME
    assert code == 0 and err == ""
    assert config.load_local(Project(repo)).home == str(env / ".loi")
    assert (env / ".loi" / "projects").is_dir() and "~/.loi/projects/" in out
    monkeypatch.setenv("HOME", "")
    monkeypatch.chdir(tmp_path)
    p = Project.open(repo)
    assert p.home_dir.is_absolute() and p.home == env / ".loi"  # the pin keeps the store where it was
    # HOME="": the pin holds, but there is nowhere to put the shim (`/.local/bin/loi`)
    code, out, err = init(repo)
    assert code == 1 and out == "" and "cannot write /.local/bin/loi" in err
    assert config.load_local(Project(repo)).home == str(env / ".loi")


def test_shim_runs_from_empty_env(repo, env):
    init(repo)
    exe = shlex.split(hook_cmd(repo, "SessionStart"))[0]
    assert exe == str(env / ".local" / "bin" / "loi")
    p = subprocess.run([exe, "--help"], env={}, capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stderr
    p = subprocess.run([exe, "hook", "claude-code", "session-end"], env={}, capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and p.stderr == b"loi hook: claude-code session-end\n"
    p = subprocess.run([exe, "hook", "session-end"], env={}, capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 2  # two words: a usage error


def test_home_with_space_is_quoted_for_sh(tmp_path, monkeypatch):
    home = tmp_path / "sp ace" / "ho'me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{HOST_BIN}")
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    assert init(r)[0] == 0
    cmd = hook_cmd(r, "SessionEnd")
    assert shlex.split(cmd) == [str(home / ".local" / "bin" / "loi"), "hook", "claude-code", "session-end"]
    # exactly how Claude Code runs it
    p = subprocess.run(["sh", "-c", cmd], env={}, capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and p.stderr == b"loi hook: claude-code session-end\n"
    code, out, _ = init(r)  # second run recognises the quoted entry as ours
    assert code == 0 and "hooks installed" not in out and "hooks repaired" not in out
    assert sum("session-end" in e["command"] for g in settings(r)["hooks"]["SessionEnd"] for e in g["hooks"]) == 1


def test_concurrent_inits_register_all(env, tmp_path, store_home):
    roots = []
    for i in range(4):
        r = tmp_path / f"r{i}"
        r.mkdir()
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        roots.append(r)
    procs = [
        subprocess.Popen([sys.executable, "-m", "loi", "init", "--no-backfill"], cwd=r, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for r in roots
    ]
    for pr in procs:
        out, err = pr.communicate()
        assert pr.returncode == 0, err
        assert b"registry                     added" in out
    assert len(registry.projects()) == 4
    assert len(list((store_home / "projects").iterdir())) == 4


def test_cli_subprocess(repo, env):
    py = [sys.executable, "-m", "loi", "init"]
    p = subprocess.run([*py, "--no-backfill"], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "Commit .loi/config.toml" in p.stdout
    p = subprocess.run([*py, "--sync"], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 2 and "--sync" in p.stderr
    p = subprocess.run([*py, "--help"], cwd=repo, capture_output=True, text=True, env={**os.environ, "COLUMNS": "120"})
    assert p.returncode == 0
    for flag in ("--no-backfill", "--home"):
        assert flag in p.stdout
    p = subprocess.run([*py, "--bogus"], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 2


def test_init_inside_a_bare_loi_dir_refuses(tmp_path, env, store_home):
    d = tmp_path / "plain" / ".loi"
    d.mkdir(parents=True)
    code, out, err = init(d)
    assert code == 1 and out == "" and err == f"loi init: run it in the project folder, not inside {d}\n"
    assert list(d.iterdir()) == [] and not store_home.exists()
    assert init(d.parent)[0] == 0 and (d / "config.toml").is_file()  # the folder above is the project
