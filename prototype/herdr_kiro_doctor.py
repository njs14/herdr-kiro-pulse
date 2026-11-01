#!/usr/bin/env python3
"""Report herdr, Kiro, and this hook without printing secrets."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from herdr_kiro_state import __version__


def run(command):
    if shutil.which(command[0]) is None:
        return None
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (completed.stdout or "").strip()


def main() -> int:
    print("herdr-kiro %s" % __version__)
    herdr = run(["herdr", "--version"])
    print("herdr: %s" % (herdr or "not on PATH"))
    kiro = run(["kiro-cli", "--version"])
    print("kiro-cli: %s" % (kiro or "not on PATH"))
    engine = run(["kiro-cli", "settings", "chat.agentEngine"])
    workflows = run(["kiro-cli", "settings", "chat.enableWorkflows"])
    print("chat.agentEngine: %s" % (engine or "unreadable"))
    print("chat.enableWorkflows: %s" % (workflows or "unreadable"))
    engine_value = ((engine or "").split() or [""])[0].lower()
    workflow_value = ((workflows or "").split() or [""])[0].lower()
    config = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "herdr",
        "agent-detection",
        "kiro.toml",
    )
    if os.path.isfile(config):
        body = open(config, encoding="utf-8").read()
        print("rule file: %s" % config)
        print("workflow_requires_approval: %s" % ("yes" if "workflow_requires_approval" in body else "no"))
    else:
        print("rule file: not installed")
    hook = os.path.join(os.environ.get("KIRO_HOOKS_DIR") or os.path.expanduser("~/.kiro/hooks"), "herdr-kiro.json")
    print("hook file: %s" % ("installed" if os.path.isfile(hook) else "not installed"))
    state = os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "herdr-kiro",
        "pids",
    )
    watchers = glob.glob(os.path.join(state, "*")) if os.path.isdir(state) else []
    print("watcher pid files: %s" % len(watchers))
    if engine and engine_value != "v3":
        print("start Kiro with: kiro-cli chat --v3")
    if workflows and workflow_value != "true":
        print("enable workflows with: kiro-cli settings chat.enableWorkflows true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
