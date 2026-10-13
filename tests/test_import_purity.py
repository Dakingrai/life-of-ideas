"""The data layer is on the hook path: it must never import argparse & co."""

import re
import subprocess
import sys

FORBIDDEN = re.compile(r"\b(argparse|typer|click|rich|importlib\.metadata|loil\.cli)\b")
MODULES = (
    "loil.util, loil.util.ulid, loil.util.gitignore, loil.util.clock, loil.util.fsutil, "
    "loil.errors, loil.adapters, loil.hooks, loil.entry, loil.log, "
    "loil.record, loil.record.project, loil.record.config, loil.record.store, loil.record.registry, "
    "loil.shim, loil.adapters.base, loil.adapters.claude_code, loil.adapters.claude_settings, loil.commands.init, loil.capture, loil.gitstate, loil.snapshots, "
    "loil.commands.capture, loil.commands.ls, loil.commands.verify"
)


def test_data_layer_imports_nothing_heavy():
    p = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", f"import {MODULES}"],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    bad = [
        line
        for line in p.stderr.splitlines()
        if line.startswith("import time:") and FORBIDDEN.search(line)
    ]
    assert bad == []
