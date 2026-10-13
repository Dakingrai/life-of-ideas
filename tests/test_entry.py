"""Subprocess tests pinning the `loil` CLI contract through the real entry point."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only (preexec_fn)")

PY = [sys.executable, "-m", "loil"]
VENV_LOIL = Path(sys.executable).parent / "loil"
FORBIDDEN = re.compile(r"\b(argparse|typer|click|rich|importlib\.metadata|loil\.cli)\b")
STUB_SET = ["loil", "loil.entry", "loil.errors", "loil.adapters", "loil.hooks"]
CC = "claude-code"


def run(args, **kw):
    kw.setdefault("capture_output", True)
    return subprocess.run([*PY, *args], **kw)


@pytest.mark.parametrize("event", ["session-start", "session-end"])
def test_hook_with_payload(event):
    p = run(["hook", CC, event], input=b"{}")
    assert p.returncode == 0
    assert p.stderr == f"loil hook: {CC} {event}\n".encode()
    assert p.stdout == b""


def test_hook_devnull_stdin():
    p = run(["hook", CC, "session-start"], stdin=subprocess.DEVNULL)
    assert p.returncode == 0
    assert p.stderr == b"loil hook: claude-code session-start\n"


def test_hook_closed_stdin():
    p = run(["hook", CC, "session-start"], preexec_fn=lambda: os.close(0))
    assert p.returncode == 0
    assert p.stderr == b"loil hook: claude-code session-start\n"
    assert p.stdout == b""


def test_hook_non_utf8_payload_still_echoes():
    p = run(["hook", CC, "session-start"], input=b"\xff\xfe")
    assert p.returncode == 0
    assert p.stderr == b"loil hook: claude-code session-start\n"


@pytest.mark.parametrize("payload", [None, b"{}", b"  {}\n"])
def test_hook_empty_payload_keeps_the_stub_import_set(payload):
    kw = {"stdin": subprocess.DEVNULL} if payload is None else {"input": payload}
    p = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "loil", "hook", CC, "session-start"],
        capture_output=True,
        text=payload is None,
        **kw,
    )
    assert p.returncode == 0
    err = p.stderr if isinstance(p.stderr, str) else p.stderr.decode()
    lines = [line for line in err.splitlines() if line.startswith("import time:")]
    assert [line for line in lines if FORBIDDEN.search(line)] == []
    assert not any(re.search(r"\b(pathlib|adapters\.claude_code|dataclasses|tomllib)\b", line) for line in lines)
    loil_mods = [line.rsplit("|", 1)[1].strip() for line in lines if "loil" in line]
    assert loil_mods == STUB_SET  # the real-payload import set is pinned in test_hooks.py


@pytest.mark.parametrize("args", [["hook"], ["hook", "session-start"], ["hook", CC]])
def test_hook_with_fewer_than_two_words_is_usage_error(args, tmp_path, store_home):
    p = run(args, stdin=subprocess.DEVNULL, cwd=tmp_path)
    assert p.returncode == 2
    assert not re.search(rb"^loil hook: (?!error:)", p.stderr, re.M)  # never echoed; argparse's "loil hook: error:" is fine
    assert list(tmp_path.iterdir()) == [] and not store_home.exists()


def test_hook_unknown_agent_exits_0_and_logs_centrally(tmp_path, store_home):
    repo = tmp_path / "repo"  # the hook's cwd: a checkout
    (repo / ".loil").mkdir(parents=True)
    p = run(["hook", "bogus-agent", "session-start"], stdin=subprocess.DEVNULL, cwd=repo)
    assert p.returncode == 0 and p.stderr == b"loil hook: bogus-agent session-start\n"
    assert not store_home.exists() and list((repo / ".loil").iterdir()) == []  # no store yet: silent
    store_home.mkdir()
    p = run(["hook", "bogus-agent", "session-start"], stdin=subprocess.DEVNULL, cwd=repo)
    assert p.returncode == 0
    line = (store_home / "loil.log").read_text()
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ hook bogus-agent session-start: LoilError\(", line)
    p = run(["hook", "Bad Name", "session-start"], stdin=subprocess.DEVNULL, cwd=repo)
    assert p.returncode == 0 and p.stderr == b"loil hook: Bad Name session-start\n"
    assert "invalid adapter name 'Bad Name'" in (store_home / "loil.log").read_text()
    assert sorted(x.name for x in repo.rglob("*")) == [".loil"]  # nothing inside the repo, ever


def test_hook_help():
    p = run(["hook", "--help"], text=True)
    assert p.returncode == 0
    assert "agent" in p.stdout.lower() and "event" in p.stdout.lower()
    p = run(["hook", "--bogus", "x"], text=True)
    assert p.returncode == 2


def test_help_lists_commands():
    p = run(["--help"], text=True)
    assert p.returncode == 0
    for name in ("init", "capture", "ls", "verify", "hook"):
        assert name in p.stdout
    p = run([], text=True)
    assert p.returncode == 0 and "init" in p.stdout


def test_version():
    p = run(["--version"], text=True)
    assert p.returncode == 0
    assert p.stdout == "loil 0.1.0\n"


def test_unknown_command_is_usage_error():
    p = run(["bogus"], text=True)
    assert p.returncode == 2


@pytest.mark.skipif(not VENV_LOIL.exists(), reason="no console script in this interpreter's bin")
def test_console_script_hook():
    p = subprocess.run([str(VENV_LOIL), "hook", CC, "session-end"], input=b"{}", capture_output=True)
    assert p.returncode == 0
    assert p.stderr == b"loil hook: claude-code session-end\n"
    assert p.stdout == b""


def test_hook_exception_is_logged_centrally_never_in_repo(tmp_path, monkeypatch, store_home):
    from loil import hooks

    def boom():
        raise RuntimeError("injected")

    monkeypatch.setattr(hooks, "_read_payload", boom)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    repo = tmp_path / "repo"
    (repo / ".loil").mkdir(parents=True)  # an inited-looking repo
    monkeypatch.chdir(repo)
    assert hooks.run([CC, "x"]) == 0  # no store: silent, no crash
    assert sorted(x.name for x in (repo / ".loil").iterdir()) == []
    assert not store_home.exists()

    store_home.mkdir()
    assert hooks.run([CC, "x"]) == 0
    log = store_home / "loil.log"
    assert log.read_text().endswith("hook claude-code x: RuntimeError('injected')\n")
    assert sorted(x.name for x in (repo / ".loil").iterdir()) == []  # never inside the repo

    monkeypatch.delenv("LOIL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h" / ".loil").mkdir(parents=True)
    assert hooks.run([CC, "y"]) == 0
    assert "hook claude-code y: RuntimeError('injected')\n" in (tmp_path / "h" / ".loil" / "loil.log").read_text()
    monkeypatch.setenv("HOME", "")  # HOME="" : still exit 0, nothing in the repo
    assert hooks.run([CC, "z"]) == 0
    assert sorted(x.name for x in (repo / ".loil").iterdir()) == []
