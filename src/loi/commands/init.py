"""`loi init`: make this folder a loi-tracked project, record its central store in
`local.toml`, install the hooks of every coding-agent adapter that is enabled here (its
binary is on PATH, or its settings file already exists at the root), and import the
sessions the agent still holds for this root (`--no-backfill` skips that).

argparse-free so it runs in-process in tests; `cli.py` only parses the flags.
Idempotent: a second run rewrites nothing, in the repo or in the store.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from loi import adapters, shim
from loi.record import config, registry, store
from loi.util import gitignore
from loi.adapters import claude_settings
from loi.errors import LoiError
from loi.record.project import Project, find_root, loi_home

GITIGNORE_ALWAYS = (".loi/local.toml",)  # plus every known live adapter's settings file
INSTRUCTIONS = "Commit .loi/config.toml and .gitignore. Each collaborator runs `loi init` once per clone."
NO_GIT_ROW = "skipped (not a git checkout)"
NO_AGENT_ROW = ("hooks", "skipped (no coding agent on PATH)")

_BINARIES = {"claude-code": "claude"}  # what `_enabled_adapters` looks for on PATH
_CHECK_IGNORE = re.compile(r"^(?P<src>.+?):(?P<line>\d+):(?P<pattern>.*)$")


def run_init(
    *,
    backfill: bool = True,
    home: str | None = None,
    cwd: Path | str | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout  # resolved per call
    err = err if err is not None else sys.stderr
    root = find_root(cwd)  # the project cwd is in, else cwd becomes one (git is not consulted)
    if root is None:
        root = Path(cwd or os.getcwd()).absolute()
        if ".loi" in root.parts:  # `cd .loi && loi init` would nest a project inside it
            print(f"loi init: run it in the project folder, not inside {root}", file=err)
            return 1
    p = Project(root)
    status: dict[str, str] = {}
    try:
        # 1. config + local (home resolved), saved only when absent or changed; the enabled
        #    adapters are detected on every run: hooks are installed, never uninstalled
        cfg, local = _load(p, home, status, err)
        enabled = _enabled_adapters(root, err)
        p = Project(root, id=cfg.id, home=local.home)
        # 2. the central store
        had_store = p.home_dir.is_dir()
        store.ensure(p)
        status["store"] = "unchanged" if had_store else "created"
        in_git = gitignore.is_git_checkout(root)
        # 3. .gitignore — only inside a git checkout; a plain folder gets none
        status["gitignore"] = _gitignore(root) if in_git else NO_GIT_ROW
        # 4. shim + each enabled adapter's hooks
        cmd = shim.ensure_hook_command()
        settings_rows = _install_hooks(root, cmd, enabled)
        # 5. the registry — last of the setup steps: a failure here leaves everything else
        #    done, so a re-run repairs it
        result = registry.register(p)
        status["registry"] = result or "unchanged"
        # 6. diagnostics
        if in_git:
            _warn_if_ignored(root, err)
    except LoiError as exc:  # SchemaError is a LoiError
        print(f"loi init: {exc}", file=err)
        return 1
    # 7. summary
    _summary(out, p, cmd, status, settings_rows)
    # 8. backfill: the sessions the agents still hold for this root. Its failures are
    #    reported row by row and never change init's exit code: the setup above is done.
    if backfill:
        _backfill(p, enabled, out, err)
    return 0


def _load(p: Project, home: str | None, status, err: TextIO) -> tuple[config.Config, config.Local]:
    had_config = config.config_exists(p)
    cfg = config.load_config(p) if had_config else config.new_config(p.root.name)
    had_local = p.local_path.is_file()
    local = config.load_local(p)

    if not had_config:
        _warn_if_previously_registered(p, cfg.id, local.home, err)  # before the new id is written
        status["config"] = f"created (id {cfg.id})"
        config.save_config(p, cfg)
    else:
        status["config"] = "unchanged"  # never rewritten: `name` is the person's

    changes: list[str] = []
    if home is not None:  # `--home`: an explicit pin for this checkout
        resolved = str(Path(os.path.abspath(os.path.expanduser(home))))
    else:
        resolved = str(loi_home(local.home))  # $LOI_HOME wins and is re-recorded; else the file's; else ~/.loi
    if local.home != resolved:
        local.home = resolved
        changes.append(f"home = {_tilde(resolved)}")
    if not had_local:
        status["local"] = "created" + (f" ({', '.join(changes)})" if changes else "")
    elif changes:
        status["local"] = "updated: " + ", ".join(changes)
    else:
        status["local"] = "unchanged"
    if not had_local or changes:
        config.save_local(p, local)
    return cfg, local


def _warn_if_previously_registered(p: Project, new_id: str, home, err: TextIO) -> None:
    """`config.toml` is being created, but the registry remembers this root under another
    id (a re-init after the file was lost): say so, naming the old store dir. Diagnostics
    only — a malformed entry elsewhere in the store must not stop `init`."""
    try:
        entries = registry.projects(home)
    except LoiError:
        return
    root = str(p.root)
    for e in entries:
        old_id = e.get("id")
        if old_id == new_id or root not in (e.get("roots") or []):
            continue
        old = Project(p.root, id=old_id, home=home)
        where = f"; its store dir {old.home_dir} still exists" if old.home_dir.is_dir() else ""
        print(
            f"loi init: warning: this checkout was previously project {old_id} (now {new_id}, "
            f"per the registry){where}",
            file=err,
        )


def _enabled_adapters(root: Path, err: TextIO) -> list:
    """Every known adapter whose binary is on PATH, plus every one whose settings file
    already exists at `root` (hooks installed earlier are kept repaired even while the
    binary is off PATH). Warns when that leaves nothing."""
    enabled = []
    for name in adapters.KNOWN:
        adapter = adapters.get(name)
        path = adapter.settings_path(root)
        if shutil.which(_BINARIES[name]) or (path is not None and path.is_file()):
            enabled.append(adapter)
    if not enabled:
        print(
            "loi init: warning: no supported coding agent found on PATH (looked for: "
            + ", ".join(_BINARIES[n] for n in adapters.KNOWN)
            + "); no hooks installed. Re-run `loi init` once it is installed",
            file=err,
        )
    return enabled


def _install_hooks(root: Path, cmd: str, enabled: list) -> list[tuple[str, str]]:
    """One summary row per enabled adapter with a settings file: (settings path, what
    happened). Hooks are never uninstalled; nothing enabled → the `hooks skipped` row."""
    rows = []
    for adapter in enabled:
        path = adapter.settings_path(root)
        if path is None:
            continue
        hooks = claude_settings.install(path, cmd, adapter)
        rows.append((str(path.relative_to(root)), f"hooks {hooks}" if hooks else "unchanged"))
    if not rows:  # nothing enabled: say so where the hooks row would be
        rows.append(NO_AGENT_ROW)
    return rows


def _gitignore(root: Path) -> str:
    """The per-user files (machine paths: never right to commit): `local.toml` and every
    known local adapter's settings file, enabled or not."""
    existed = (root / ".gitignore").exists()
    lines = list(GITIGNORE_ALWAYS)
    for name in adapters.KNOWN:
        adapter = adapters.get(name)
        path = adapter.settings_path(root)
        if path is not None and adapter.hook_events and adapter.settings_are_local:
            lines.append(path.relative_to(root).as_posix())
    added = [line for line in lines if gitignore.ensure_line(root, line)]
    if not added:
        return "unchanged"
    return ("created: " if not existed else "added: ") + ", ".join(added)


def _warn_if_ignored(root: Path, err: TextIO) -> None:
    """`git check-ignore -v`: diagnostics only. Any git failure (128, no git) is skipped:
    the ignore lines were already written without git, and this can't change them. A
    *tracked* file is reported as not ignored, so the warning goes quiet once the user
    commits `config.toml` — the right default."""
    paths = [".loi/config.toml"]
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-v", "--", *paths],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):  # git absent, or cwd/root vanished
        return
    if r.returncode != 0:  # 1: nothing ignored; 128 etc.: not our problem here
        return
    for line in r.stdout.splitlines():
        rest, _, path = line.rpartition("\t")
        m = _CHECK_IGNORE.match(rest)
        where = f"{m['src']}:{m['line']} ({m['pattern']})" if m else rest
        if path == ".loi/config.toml":
            print(
                f"loi init: warning: {path} is ignored by {where}; commit it or a clone "
                "will not be a loi project",
                file=err,
            )


def _summary(out: TextIO, p: Project, cmd: str, status: dict[str, str], settings_rows: list[tuple[str, str]]) -> None:
    rows = [
        (".loi/config.toml", status["config"]),
        (".loi/local.toml", status["local"]),
        (".gitignore", status["gitignore"]),
        *settings_rows,
        (_tilde(str(p.home_dir)) + "/", status["store"]),
        ("registry", status["registry"]),
    ]
    print(f"loi init: {p.root}", file=out)
    for name, what in rows:
        print(f"  {name:<28} {what}", file=out)
    print(f"  hook command: {_tilde(cmd)}", file=out)
    print(INSTRUCTIONS, file=out)


def _backfill(p: Project, enabled: list, out: TextIO, err: TextIO) -> None:
    """Import the sessions each enabled adapter still holds for this root (M7 wires the
    real import; until then nothing is printed)."""
    try:
        from loi.commands.capture import backfill_rows
    except ImportError:  # the capture command is not built yet
        return
    backfill_rows(p, [a.name for a in enabled], out, err)


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path
