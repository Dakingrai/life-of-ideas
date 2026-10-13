"""The data layer is on the hook path: it must never import argparse & co."""

import re
import subprocess
import sys

FORBIDDEN = re.compile(r"\b(argparse|typer|click|rich|importlib\.metadata|loi\.cli)\b")
MODULES = (
    "loi.util, loi.util.ulid, loi.util.gitignore, loi.util.clock, loi.util.fsutil, "
    "loi.errors, loi.adapters, loi.hooks, loi.entry, loi.log, "
    "loi.record, loi.record.project, loi.record.config, loi.record.store, loi.record.registry, "
    "loi.shim, loi.adapters.base, loi.adapters.claude_code, loi.adapters.claude_settings, loi.commands.init, loi.capture, loi.gitstate, loi.snapshots, "
    "loi.commands.capture, loi.commands.ls, loi.commands.verify"
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
