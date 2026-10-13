"""Project-level records: what changes *around* a session rather than inside it, kept under
`<home_dir>/`. Stdlib only; runs inside the hooks, never raises out of `run`.

- `memory/<ts>/…` + `memory/index.jsonl`: Claude Code's project memory (the `memory/`
  directory beside the session's transcript), copied whenever its content hash differs from
  the last snapshot. One line per snapshot in the index: `{at, event, session, tree_sha256,
  dir, files: [{path, bytes, sha256}]}`. `diff -r memory/A memory/B` shows how it evolved.
- `repo-config/<ts>/…` + `repo-config/index.jsonl`: the files that shape every session in the
  repo — `CLAUDE.md`, `AGENTS.md`, `CLAUDE.local.md`, `.claude/*.json` (not the per-user
  `settings.local.json`), `.claude/{agents,skills,commands,hooks,rules}/**` — same scheme,
  `FILE_CAP` per file.
- `history.jsonl`: the lines of Claude Code's global `history.jsonl` (every prompt typed,
  with its project path and session id) whose project resolves to this root, appended
  verbatim and deduplicated on `(sessionId, timestamp)`. It outlives the transcripts Claude
  Code deletes, so it is the independent witness of which sessions existed.
- `pastes/<contentHash>.txt`: the `paste-cache` files those lines reference, while they exist.
- `sessions-index.json`: Claude Code's own index of the slug dir, copied when present.

Every write to these shared files happens under the project directory's flock (`_locked`):
two sessions ending at once must not interleave lines or snapshot dirs.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

from loil import log
from loil.util import clock
from loil.util.fsutil import atomic_write_bytes
from loil.record.project import find_root
from loil.record.registry import _locked

FILE_CAP = 1 << 20  # repo-config files over this are left out (a stray binary under .claude/)
MEMORY_CAP = 16 << 20
HISTORY = "history.jsonl"
PASTES = "pastes"
SESSIONS_INDEX = "sessions-index.json"
INDEX = "index.jsonl"

REPO_CONFIG_FILES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")
REPO_CONFIG_DIRS = ("agents", "skills", "commands", "hooks", "rules")
REPO_CONFIG_EXCLUDE = frozenset({"settings.local.json"})


def run(project, session, event: str, *, config_dir: str | None, root_cwd: str | None = None, full: bool) -> None:
    """Everything above for one hook. `full` (end/pre-compact) adds history, pastes and the
    sessions index to the two tree snapshots. Each part is independent and logged on failure."""
    parts = [
        ("memory", lambda: snapshot_memory(project, session, event)),
        ("repo-config", lambda: snapshot_repo_config(project, session, event)),
    ]
    if full and config_dir:
        parts += [
            ("history", lambda: sync_history(project, config_dir)),
            ("sessions-index", lambda: copy_sessions_index(project, session)),
        ]
    for name, fn in parts:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — one part failing must not stop the others
            log.get_logger(project).error("snapshot %s (%s %s): %r", name, session.id, event, exc)


# --- tree snapshots -----------------------------------------------------------------------------


def memory_dir(session) -> str | None:
    """`<config>/projects/<slug>/memory/` for this session's transcript: memory is per slug,
    and a project has one slug per directory sessions were started in."""
    t = session.transcript_path
    if not t:
        return None
    return os.path.join(os.path.dirname(t), "memory")


def snapshot_memory(project, session, event: str) -> str | None:
    src = memory_dir(session)
    if src is None:
        return None
    return snapshot_tree(project, "memory", _walk_files(src, cap=MEMORY_CAP), at=clock.now_iso(), event=event, session=session.id)


def snapshot_repo_config(project, session, event: str) -> str | None:
    return snapshot_tree(project, "repo-config", repo_config_files(project.root), at=clock.now_iso(), event=event, session=session.id)


def repo_config_files(root) -> list[tuple[str, str]]:
    """`(abs_path, rel_posix)` of the agent-configuration files in `root`, allow-listed."""
    root = Path(root)
    out = []
    for name in REPO_CONFIG_FILES:
        p = root / name
        if p.is_file() and not p.is_symlink():
            out.append((str(p), name))
    dot = root / ".claude"
    if dot.is_dir() and not dot.is_symlink():
        for p in sorted(dot.iterdir()):
            if p.suffix == ".json" and p.name not in REPO_CONFIG_EXCLUDE and p.is_file() and not p.is_symlink():
                out.append((str(p), f".claude/{p.name}"))
        for d in REPO_CONFIG_DIRS:
            for abs_path, rel in _walk_files(str(dot / d), cap=FILE_CAP):
                out.append((abs_path, f".claude/{d}/{rel}"))
    return [(a, r) for a, r in out if _size_ok(a, FILE_CAP)]


def snapshot_tree(project, name: str, files, *, at: str, event: str, session: str) -> str | None:
    """Copy `files` (`(abs, rel)` pairs) into `<home_dir>/<name>/<stamp>/` and append an index
    line, unless the tree hash equals the last snapshot's. Returns the new dir name, or None.
    An empty tree with no earlier snapshot records nothing."""
    files = sorted(files, key=lambda f: f[1])
    entries = []
    for abs_path, rel in files:
        try:
            n, digest = _hash_file(abs_path)
        except OSError:
            continue  # vanished between the listing and the read: not in this snapshot
        entries.append({"path": rel, "bytes": n, "sha256": digest, "_abs": abs_path})
    tree = hashlib.sha256("".join(f"{e['path']}\0{e['sha256']}\n" for e in entries).encode("utf-8")).hexdigest()
    base = project.home_dir / name
    with _locked(project):
        last = _last_index_line(base / INDEX)
        if last is not None and last.get("tree_sha256") == tree:
            return None
        if last is None and not entries:
            return None
        stamp = _stamp(at)
        if entries:
            dest = base / stamp
            dest.mkdir(mode=0o700, parents=True, exist_ok=True)
            for e in entries:
                target = dest / e["path"]
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    shutil.copyfile(e["_abs"], target)
                except OSError:
                    continue
                os.chmod(target, 0o600)
        line = {
            "at": at,
            "event": event,
            "session": session,
            "tree_sha256": tree,
            "dir": stamp if entries else None,
            "files": [{k: v for k, v in e.items() if k != "_abs"} for e in entries],
        }
        _append(base / INDEX, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
        return stamp if entries else None


# --- history, pastes, sessions index -------------------------------------------------------------


def sync_history(project, config_dir: str) -> int:
    """Append the lines of `<config>/history.jsonl` that belong to this project and are not
    yet in `<home_dir>/history.jsonl`; copy the pastes they reference. Returns lines added."""
    src = Path(config_dir) / HISTORY
    if not src.is_file():
        return 0
    root = project.root
    dst = project.home_dir / HISTORY
    added = 0
    with _locked(project):
        seen = set()
        if dst.is_file():
            with open(dst, "rb") as f:
                for raw in f:
                    key = _history_key(raw)
                    if key is not None:
                        seen.add(key)
        new: list[bytes] = []
        hashes: list[str] = []
        with open(src, "rb") as f:
            for raw in f:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(d, dict) or not isinstance(d.get("project"), str):
                    continue
                if find_root(d["project"]) != root:
                    continue
                key = _history_key(raw)
                if key is None or key in seen:
                    continue
                seen.add(key)
                new.append(raw if raw.endswith(b"\n") else raw + b"\n")
                pc = d.get("pastedContents")
                if isinstance(pc, dict):
                    for v in pc.values():
                        h = v.get("contentHash") if isinstance(v, dict) else None
                        if isinstance(h, str) and h.isalnum():
                            hashes.append(h)
        if new:
            _append(dst, b"".join(new))
            added = len(new)
        for h in hashes:
            cache = Path(config_dir) / "paste-cache" / f"{h}.txt"
            target = project.home_dir / PASTES / f"{h}.txt"
            if cache.is_file() and not target.exists():
                target.parent.mkdir(mode=0o700, exist_ok=True)
                try:
                    shutil.copyfile(cache, target)
                    os.chmod(target, 0o600)
                except OSError:
                    pass
    return added


def copy_sessions_index(project, session) -> bool:
    """Copy `<slug dir>/sessions-index.json` beside the session's transcript, if present and
    different from the copy we hold."""
    t = session.transcript_path
    if not t:
        return False
    src = Path(os.path.dirname(t)) / SESSIONS_INDEX
    if not src.is_file():
        return False
    data = src.read_bytes()
    dst = project.home_dir / SESSIONS_INDEX
    with _locked(project):
        if dst.is_file() and dst.read_bytes() == data:
            return False
        atomic_write_bytes(dst, data, mode=0o600)
    return True


# --- the `~/.claude.json` project entry ---------------------------------------------------------


def claude_project_entry(config_dir: str | None, paths) -> bytes | None:
    """The `projects[<path>]` entry of Claude Code's global `.claude.json` (looked for in the
    config dir, then in HOME) for the first of `paths` it holds, as JSON bytes
    `{"path", "entry"}`; None when there is none."""
    candidates = []
    if config_dir:
        candidates.append(Path(config_dir) / ".claude.json")
    candidates.append(Path(os.path.expanduser("~")) / ".claude.json")
    for c in candidates:
        try:
            with open(c, "rb") as f:
                d = json.loads(f.read())
        except (OSError, ValueError):
            continue
        projects = d.get("projects") if isinstance(d, dict) else None
        if not isinstance(projects, dict):
            continue
        for p in paths:
            if p and p in projects:
                return (json.dumps({"path": p, "entry": projects[p]}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return None


# --- helpers ----------------------------------------------------------------------------------------


def _walk_files(top: str, *, cap: int):
    """`(abs, rel_posix)` of every regular file under `top`, symlinks skipped, sorted."""
    if not os.path.isdir(top) or os.path.islink(top):
        return
    for dp, dns, fns in os.walk(top, followlinks=False):
        dns[:] = sorted(d for d in dns if not os.path.islink(os.path.join(dp, d)))
        for name in sorted(fns):
            full = os.path.join(dp, name)
            if os.path.islink(full) or not os.path.isfile(full) or not _size_ok(full, cap):
                continue
            yield full, os.path.relpath(full, top).replace(os.sep, "/")


def _size_ok(path: str, cap: int) -> bool:
    try:
        return os.stat(path).st_size <= cap
    except OSError:
        return False


def _hash_file(path: str) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
    return n, h.hexdigest()


def _last_index_line(path: Path) -> dict | None:
    try:
        with open(path, "rb") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        if raw.strip():
            try:
                d = json.loads(raw)
            except ValueError:
                return None
            return d if isinstance(d, dict) else None
    return None


def _history_key(raw: bytes):
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    sid, ts = d.get("sessionId"), d.get("timestamp")
    if sid is None and ts is None:
        return None
    return (str(sid), str(ts), str(d.get("display"))[:200])


def _stamp(iso: str) -> str:
    """`2026-10-12T20:45:30.123Z` → `20261012T204530123Z`: filesystem-safe, sorts as time."""
    return iso.replace("-", "").replace(":", "").replace(".", "")


def _append(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
