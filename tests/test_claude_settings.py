"""`claude_settings.install_hooks`: merge/repair of `.claude/settings.local.json`."""

import copy
import json

import pytest

from loil import adapters
from loil.adapters import claude_settings as cs
from loil.adapters.base import Adapter
from loil.errors import LoilError

CMD = "/home/u/.local/bin/loil"
CC = adapters.get("claude-code")
START = {"type": "command", "command": f"{CMD} hook claude-code session-start"}
END = {"type": "command", "command": f"{CMD} hook claude-code session-end", "timeout": 30}
PRE = {"type": "command", "command": f"{CMD} hook claude-code pre-compact", "timeout": 30}
EXPECTED = {"hooks": {"SessionStart": [{"hooks": [START]}], "SessionEnd": [{"hooks": [END]}], "PreCompact": [{"hooks": [PRE]}]}}

# What Claude Code itself writes, plus a hand-added SessionStart matcher group.
REALISTIC = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {"allow": ["Bash(git status)", "Bash(ls *)"], "deny": []},
    "env": {"FOO": "bär ✓"},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/lint-bash"}]}
        ],
        "SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "/usr/bin/motd"}]}
        ],
    },
}


@pytest.fixture
def path(tmp_path):
    return tmp_path / ".claude" / "settings.local.json"


def write(path, obj_or_text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj_or_text, str):
        path.write_text(obj_or_text, encoding="utf-8")
    else:
        path.write_text(json.dumps(obj_or_text, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def loil_entries(data, event, name):
    return [e for g in data["hooks"][event] for e in g["hooks"] if e["command"].endswith(f"hook claude-code {name}")]


def test_hook_command():
    assert cs.hook_command("/x/loil", "claude-code", "session-start") == "/x/loil hook claude-code session-start"
    assert cs.hook_command("/some dir/loil", "codex", "stop") == "'/some dir/loil' hook codex stop"


def test_absent_file_exact_bytes(path):
    assert cs.install_hooks(path, CMD, CC) == "installed"
    assert path.read_bytes() == (json.dumps(EXPECTED, indent=2) + "\n").encode()
    assert sorted(x.name for x in path.parent.iterdir()) == [path.name]  # no temp left


def test_timeout_only_on_end_and_pre_compact(path):
    cs.install_hooks(path, CMD, CC)
    d = load(path)
    assert "timeout" not in d["hooks"]["SessionStart"][0]["hooks"][0]
    assert d["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 30
    assert "matcher" not in d["hooks"]["SessionStart"][0]
    assert "matcher" not in d["hooks"]["SessionEnd"][0]


def test_second_call_is_noop(path):
    cs.install_hooks(path, CMD, CC)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    assert cs.install_hooks(path, CMD, CC) is None
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_command_with_space_is_quoted_and_recognised(path):
    cmd = "/home/some user/.local/bin/loil"
    assert cs.install_hooks(path, cmd, CC) == "installed"
    written = load(path)["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert written == "'/home/some user/.local/bin/loil' hook claude-code session-end"
    assert cs.install_hooks(path, cmd, CC) is None  # recognised as ours via shlex.split
    # a stale quoted command is still ours and gets repaired
    write(path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "'/old dir/loil' hook claude-code session-start"}]}]}})
    assert cs.install_hooks(path, cmd, CC) == "installed"  # SessionEnd added, start repaired
    assert len(load(path)["hooks"]["SessionStart"]) == 1


def test_realistic_file_foreign_preserved(path):
    write(path, REALISTIC)
    assert cs.install_hooks(path, CMD, CC) == "installed"
    d = load(path)
    assert list(d) == ["$schema", "permissions", "env", "hooks"]
    assert d["$schema"] == REALISTIC["$schema"]
    assert d["permissions"] == REALISTIC["permissions"]
    assert d["env"] == {"FOO": "bär ✓"}
    assert "bär ✓" in path.read_text(encoding="utf-8")  # ensure_ascii=False
    assert d["hooks"]["PreToolUse"] == REALISTIC["hooks"]["PreToolUse"]
    assert list(d["hooks"]) == ["PreToolUse", "SessionStart", "SessionEnd", "PreCompact"]
    assert d["hooks"]["PreCompact"] == [{"hooks": [PRE]}]
    assert d["hooks"]["SessionStart"][0] == REALISTIC["hooks"]["SessionStart"][0]
    assert d["hooks"]["SessionStart"][1] == {"hooks": [START]}
    assert d["hooks"]["SessionEnd"] == [{"hooks": [END]}]
    assert cs.install_hooks(path, CMD, CC) is None


def test_stale_command_repaired_in_place_no_duplicate(path):
    write(
        path,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/.venv/bin/loil hook claude-code session-start",
                                "note": "mine",
                            }
                        ],
                    }
                ],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "/old/loil hook claude-code session-end"}]}],
                "PreCompact": [{"hooks": [{"type": "command", "command": "/old/loil hook claude-code pre-compact"}]}],
            }
        },
    )
    assert cs.install_hooks(path, CMD, CC) == "repaired"
    d = load(path)
    assert loil_entries(d, "PreCompact", "pre-compact") == [PRE]
    start = loil_entries(d, "SessionStart", "session-start")
    assert start == [{"type": "command", "command": START["command"], "note": "mine"}]
    assert d["hooks"]["SessionStart"][0]["matcher"] == ""  # group untouched
    assert loil_entries(d, "SessionEnd", "session-end") == [END]  # timeout added on repair
    assert cs.install_hooks(path, CMD, CC) is None


def test_duplicates_collapse_and_empty_group_removed(path):
    write(
        path,
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "/a/loil hook claude-code session-start"}]},
                    {"hooks": [{"type": "command", "command": "/b/loil hook claude-code session-start"}]},
                    {
                        "matcher": "resume",
                        "hooks": [
                            {"type": "command", "command": "/c/loil hook claude-code session-start"},
                            {"type": "command", "command": "/usr/bin/other"},
                        ],
                    },
                ],
                "SessionEnd": [{"hooks": [END]}],
                "PreCompact": [{"hooks": [PRE]}],
            }
        },
    )
    assert cs.install_hooks(path, CMD, CC) == "repaired"
    d = load(path)
    assert d["hooks"]["SessionStart"] == [
        {"hooks": [START]},
        {"matcher": "resume", "hooks": [{"type": "command", "command": "/usr/bin/other"}]},
    ]
    assert d["hooks"]["SessionEnd"] == [{"hooks": [END]}]


def test_invalid_json_untouched(path):
    write(path, "{\n")
    with pytest.raises(LoilError, match=r"settings\.local\.json"):
        cs.install_hooks(path, CMD, CC)
    assert path.read_bytes() == b"{\n"


def test_not_an_object_untouched(path):
    write(path, "[]\n")
    with pytest.raises(LoilError, match="object"):
        cs.install_hooks(path, CMD, CC)
    assert path.read_bytes() == b"[]\n"


@pytest.mark.parametrize(
    "bad",
    [{"hooks": []}, {"hooks": "x"}, {"hooks": {"SessionStart": {}}}, {"hooks": {"SessionEnd": "x"}}],
)
def test_wrong_types_raise_and_untouched(path, bad):
    write(path, bad)
    before = path.read_bytes()
    with pytest.raises(LoilError):
        cs.install_hooks(path, CMD, CC)
    assert path.read_bytes() == before


def test_null_event_treated_as_absent(path):
    write(path, {"hooks": {"SessionStart": None}})
    assert cs.install_hooks(path, CMD, CC) == "installed"
    assert load(path) == EXPECTED


def test_foreign_shapes_inside_lists_skipped(path):
    weird = {
        "hooks": {
            "SessionStart": [
                "not a group",
                {"matcher": "x"},
                {"hooks": "not a list"},
                {"hooks": ["not an entry", {"type": "command", "command": 42}, {"type": "command"}]},
            ]
        }
    }
    write(path, weird)
    assert cs.install_hooks(path, CMD, CC) == "installed"
    d = load(path)
    assert d["hooks"]["SessionStart"][:4] == weird["hooks"]["SessionStart"]
    assert d["hooks"]["SessionStart"][4] == {"hooks": [START]}


@pytest.mark.parametrize(
    "lookalike",
    [
        "/usr/bin/otherhook session-start",
        "/x/loil hook claude-code session-start --extra",
        "/x/loil 'hook claude-code session-start",
        "/x/loil hook codex session-start",  # another adapter's entry: not ours
        "/x/loil hook claude-code session-end",  # ours, but for the other event
        "/x/loil hook session-end",
        "/x/loi hook claude-code session-start",  # loi's entry (another tool): never ours
        "/x/other hook claude-code session-start",  # right words, not a loil binary
        "/x/loil hook claude-code session-end --x",  # options after the words: not ours
    ],
)
def test_lookalike_commands_are_foreign_to_session_start(path, lookalike):
    write(path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": lookalike}]}]}})
    cs.install_hooks(path, CMD, CC)
    d = load(path)
    assert d["hooks"]["SessionStart"][0]["hooks"][0]["command"] == lookalike
    assert len(d["hooks"]["SessionStart"]) == 2


def test_hand_formatted_correct_file_not_rewritten(path):
    text = json.dumps(EXPECTED, indent=4) + "\n"  # different indentation, same data
    write(path, text)
    assert cs.install_hooks(path, CMD, CC) is None
    assert path.read_text() == text


def test_mixed_repair_and_add_reports_installed(path):
    write(path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/old/loil hook claude-code session-start"}]}]}})
    assert cs.install_hooks(path, CMD, CC) == "installed"
    assert load(path)["hooks"]["SessionStart"] == [{"hooks": [START]}]


def test_semantics_kept_after_reindent(path):
    data = copy.deepcopy(REALISTIC)
    write(path, json.dumps(data))  # compact, one line
    cs.install_hooks(path, CMD, CC)
    d = load(path)
    for k in REALISTIC:
        if k != "hooks":
            assert d[k] == REALISTIC[k]


def test_generic_over_adapter(path):
    class Zed(Adapter):
        name = "zed"
        hook_events = (("Start", "start", None), ("Stop", "stop", 5), ("Compact", "pre-compact", 5))

    assert cs.install_hooks(path, CMD, Zed()) == "installed"
    d = load(path)
    assert list(d["hooks"]) == ["Start", "Stop", "Compact"]
    assert d["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": f"{CMD} hook zed stop", "timeout": 5}]}]
    assert cs.install_hooks(path, CMD, Zed()) is None
    assert cs.install_hooks(path, CMD, CC) == "installed"  # two adapters can share a file
    assert len(load(path)["hooks"]) == 6
    assert cs.install_hooks(path, CMD, Adapter()) is None  # passive: nothing to install


def test_passive_adapter_writes_no_file(path):
    assert cs.install_hooks(path, CMD, Adapter()) is None
    assert not path.exists() and not path.parent.exists()
