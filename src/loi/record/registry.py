"""The registry of inited projects: one `<loi_home>/projects/<id>/project.json` each.

`{"schema": 1, "id", "name", "inited_at", "root", "roots": [path, ...]}` — `root` is the
last inited path, `roots` every path ever inited (a moved checkout, two clones), `name` a
mirror of `config.toml` for the cross-project index. The store directory itself is the
index: `projects()` globs it. No central list, so concurrent `loi init`s in different
repos never touch the same file; two inits of the *same* id serialise on an exclusive
`flock` of the project's directory. `register` is idempotent: a re-init from the same
root changes nothing. Stdlib only.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from loi.util import clock
from loi.errors import LoiError
from loi.util.fsutil import atomic_write_bytes
from loi.record.project import Project, ensure_loi_home, loi_home

SCHEMA = 1
FILE = "project.json"
ADDED = "added"
UPDATED = "updated"


def entry_path(project: Project) -> Path:
    return project.home_dir / FILE


def entry(project: Project) -> dict | None:
    """This project's registry entry (`project.json`), or None when it was never registered."""
    return _load(entry_path(project))


def register(project: Project) -> str | None:
    """Add or update the entry for `project.id`. Returns ADDED, `updated (root)`, or None
    (unchanged, file untouched)."""
    if project.id is None:
        raise LoiError("project id unknown; run `loi init`")
    path = entry_path(project)
    root = str(project.root)
    with _locked(project):
        entry = _load(path)
        if entry is None:
            _save(
                path,
                {
                    "schema": SCHEMA,
                    "id": project.id,
                    "name": project.root.name,
                    "inited_at": clock.now_iso(),
                    "root": root,
                    "roots": [root],
                },
            )
            return ADDED
        changed: list[str] = []
        if entry.get("root") != root:
            entry["root"] = root
            entry["name"] = project.root.name  # follows the root; not reported on its own
            changed.append("root")
        roots = entry.setdefault("roots", [])
        if root not in roots:
            roots.append(root)
            if "root" not in changed:
                changed.append("root")
        if not changed:
            return None
        _save(path, entry)
        return f"{UPDATED} ({', '.join(changed)})"


def unregister(project_id: str, home: Path | str | None = None) -> bool:
    """Forget the project; its store directory (captures, log) is left alone."""
    path = loi_home(home) / "projects" / project_id / FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LoiError(f"cannot remove {path}: {exc}") from None
    return True


def projects(home: Path | str | None = None) -> list[dict]:
    """Every registered project under the store root, sorted by name (then id). A store
    dir without a `project.json` is not an entry; a malformed one raises (loudly)."""
    out = []
    for path in sorted((loi_home(home) / "projects").glob(f"*/{FILE}")):
        entry = _load(path)
        if entry is not None:
            out.append(entry)
    return sorted(out, key=lambda e: (str(e.get("name", "")), str(e.get("id", ""))))


# --- helpers -------------------------------------------------------------------


@contextmanager
def _locked(project: Project):
    ensure_loi_home(project.home)
    home_dir = project.home_dir
    try:
        home_dir.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(home_dir, os.O_RDONLY)  # flock on the directory itself: no lock file
    except OSError as exc:
        raise LoiError(f"cannot lock {home_dir}: {exc}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LoiError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise LoiError(f"{path}: not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("id"), str):
        raise LoiError(f"{path}: not a loi project entry (schema {SCHEMA}); fix it or move it aside")
    if data["id"] != path.parent.name:
        raise LoiError(f"{path}: id {data['id']!r} does not match its directory")
    if not isinstance(data.get("roots", []), list):
        raise LoiError(f"{path}: 'roots' must be an array; fix it or move it aside")
    return data


def _save(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))
