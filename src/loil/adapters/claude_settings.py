"""Install or repair loil's hook entries in a Claude Code-style settings file.

`loil init` writes `.claude/settings.local.json` (per user, gitignored). The file is
merged, never clobbered: anything that is not a loil entry is left exactly as it was.
Stdlib only. Generic over an adapter's `hook_events`, so a later adapter with the same
`hooks.<Event>: [group{hooks: [entry]}]` shape reuses it.

Shape (Claude Code docs): `hooks.<Event>` is a list of groups
`{"matcher"?: str, "hooks": [entry, ...]}`; an entry is `{"type": "command",
"command": str, "timeout"?: int}`. Omitting `matcher` matches everything. Claude Code
runs command hooks under `sh -c`, so the command path is shell-quoted.

A *loil entry* for `(agent, event)` is an entry whose `command` is a str whose last three
shell words are `hook <agent> <event>`. `/x/otherhook session-start`,
`/usr/bin/git hook session-start` and loi's own `… hook claude-code session-start` are
not: loi and loil can share a settings file. After `install` there is exactly one per
event, with the current command; the first one found is rewritten in place (its other
keys kept), the rest are removed, and a group left with no entries is dropped. Non-dict
groups/entries and non-str commands are foreign: untouched, never counted.
`hooks.<Event>: null` counts as absent.
"""

import copy
import json
import shlex
from pathlib import Path

from loil.errors import LoilError
from loil.util.fsutil import atomic_write_bytes

INSTALLED = "installed"
REPAIRED = "repaired"


def hook_command(command: str, agent: str, event: str) -> str:
    """The exact string written into settings for `loil hook <agent> <event>`."""
    return f"{shlex.quote(command)} hook {agent} {event}"


def install(path: Path, command: str, adapter) -> str | None:
    """One loil entry per `adapter.hook_events` in `path`. Returns INSTALLED if an entry
    was added (even if another was repaired), REPAIRED if one was changed or a duplicate
    removed, None if nothing changed. Writes only when the data changed: a hand-formatted
    file that already has the right entries is not re-indented."""
    if not adapter.hook_events:
        return None  # passive adapter: nothing to install — and no empty `{"hooks": {}}` file
    path = Path(path)
    data = _read(path)
    before = copy.deepcopy(data)
    result = _merge_hooks(path, data, command, adapter)
    if data == before:
        return None
    _write(path, data)
    return result


install_hooks = install


def _merge_hooks(path: Path, data: dict, command: str, adapter) -> str | None:
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise LoilError(f"{path}: 'hooks' is not an object; fix it or move the file aside")

    added = repaired = False
    for key, event, timeout in adapter.hook_events:
        groups = hooks.get(key)
        if groups is None:
            groups = hooks[key] = []
        elif not isinstance(groups, list):
            raise LoilError(f"{path}: 'hooks.{key}' is not a list; fix it or move the file aside")
        desired = {"type": "command", "command": hook_command(command, adapter.name, event)}
        if timeout is not None:
            desired["timeout"] = timeout
        a, r = _ensure_entry(groups, adapter.name, event, desired)
        added |= a
        repaired |= r
    if added:
        return INSTALLED
    return REPAIRED if repaired else None


def _ensure_entry(groups: list, agent: str, event: str, desired: dict) -> tuple[bool, bool]:
    """Returns (added, repaired)."""
    first = None
    dupes: list[tuple[list, dict]] = []
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if _is_loil_entry(entry, agent, event):
                if first is None:
                    first = entry
                else:
                    dupes.append((entries, entry))
    if first is None:
        groups.append({"hooks": [dict(desired)]})
        return True, False
    repaired = False
    for k, v in desired.items():
        if first.get(k) != v:
            first[k] = v
            repaired = True
    for entries, entry in dupes:
        entries.remove(entry)
        repaired = True
    if dupes:
        groups[:] = [g for g in groups if not (isinstance(g, dict) and g.get("hooks") == [])]
    return False, repaired


def _is_loil_entry(entry, agent: str, event: str) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    try:
        words = shlex.split(entry["command"])
    except ValueError:  # unbalanced quotes: not ours
        return False
    # ours iff the binary is a `loil` and the last three words are the hook contract;
    # loi's `<…>/loi hook claude-code session-start` stays foreign so both can coexist
    return len(words) >= 4 and words[-3:] == ["hook", agent, event] and words[-4].rsplit("/", 1)[-1] == "loil"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LoilError(f"cannot read {path}: {exc}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LoilError(f"{path} is not valid JSON ({exc}); fix it or move it aside") from None
    if not isinstance(data, dict):
        raise LoilError(f"{path} is not a JSON object; fix it or move it aside")
    return data


def _write(path: Path, data: dict) -> None:
    # Byte-identical to JSON.stringify(x, null, 2): Claude Code's own rewrites won't
    # ping-pong the formatting.
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))
