#!/usr/bin/env python3
"""Report herdr, Kiro, and this hook. Exit non-zero when something is wrong."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from herdr_kiro_state import __version__, herdr_bin, sweep_stale_pidfiles

BUILT_AGAINST = "2026.09.19.1"
SETTINGS_TIMEOUT = 2


def version_key(text):
    parts = []
    for piece in (text or "").split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def newer_upstream(remote, baseline=BUILT_AGAINST):
    remote_key = version_key(remote)
    base_key = version_key(baseline)
    if remote_key is None or base_key is None:
        return False
    return remote_key > base_key


def run(command, timeout):
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout", ""
    except (OSError, subprocess.SubprocessError):
        return "error", ""
    return completed.returncode, (completed.stdout or "").strip()


def settings_file():
    return os.path.join(os.path.expanduser("~/.kiro"), "settings", "cli.json")


def setting_from_file(key):
    path = settings_file()
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return None
    value = data.get(key)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return None
    return str(value)


def read_setting(name):
    """CLI first, with a short timeout. The settings file covers a hung CLI."""
    code, text = run(["kiro-cli", "settings", name], SETTINGS_TIMEOUT)
    if code == 0 and text:
        return text.split()[0].lower(), "kiro-cli"
    filed = setting_from_file(name)
    if filed:
        return filed.lower(), "settings file"
    return "unknown", "unavailable"


def lifecycle_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "herdr-kiro", "lifecycle")


def read_lifecycle():
    path = lifecycle_path()
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return "", ""
    state = lines[0].strip() if lines else ""
    via = ""
    for line in lines[1:]:
        if line.startswith("via="):
            via = line.split("=", 1)[1].strip()
    return state, via


def marker_hash(path):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    if not text.startswith("herdr-kiro-owned"):
        return None
    for line in text.splitlines():
        if line.startswith("sha256 "):
            return line.split(" ", 1)[1].strip()
    return ""


def file_digest(path):
    hashed = hashlib.sha256()
    with open(path, "rb") as handle:
        hashed.update(handle.read())
    return hashed.hexdigest()


def explain_upstream():
    code, text = run(
        [herdr_bin(), "agent", "explain", "--file", os.devnull, "--agent", "kiro", "--json"],
        5,
    )
    if code != 0 or not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def warn_upstream():
    """Print a loud warning when the cached Kiro manifest is newer. Exit 1 then."""
    data = explain_upstream()
    remote = str(data.get("cached_remote_version") or "")
    if not newer_upstream(remote):
        return 0
    shadowing = data.get("local_override_shadowing_remote")
    sys.stderr.write(
        "WARNING: upstream Kiro detection %s is newer than %s, the version this override was built against.\n"
        % (remote, BUILT_AGAINST)
    )
    sys.stderr.write(
        "local_override_shadowing_remote=%s. The installed kiro.toml replaces the remote manifest.\n"
        % shadowing
    )
    sys.stderr.write(
        "Resolve it by moving ~/.config/herdr/agent-detection/kiro.toml aside and running "
        "herdr server reload-agent-manifests, or by rebuilding this override against %s and reinstalling.\n"
        % remote
    )
    return 1


def plugin_registered():
    code, text = run([herdr_bin(), "plugin", "list"], 5)
    if code != 0:
        return None
    return "herdr-kiro" in text


def dead_pidfiles(directory):
    found = []
    if not os.path.isdir(directory):
        return found
    for name in os.listdir(directory):
        if not name.isdigit():
            continue
        try:
            os.kill(int(name), 0)
        except OSError:
            found.append(os.path.join(directory, name))
    return found


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--warn-upstream":
        return warn_upstream()
    problems = []
    print("herdr-kiro %s" % __version__)
    herdr_code, herdr_text = run([herdr_bin(), "--version"], 3)
    print("herdr: %s" % (herdr_text if herdr_code == 0 and herdr_text else "not on PATH"))
    if herdr_code != 0 or not herdr_text:
        problems.append("herdr is not on PATH")
    code, kiro_text = run(["kiro-cli", "--version"], 3)
    print("kiro-cli: %s" % (kiro_text if code == 0 and kiro_text else "not on PATH"))
    if code != 0 or not kiro_text:
        problems.append("kiro-cli is not on PATH")
    engine, engine_from = read_setting("chat.agentEngine")
    workflows, workflows_from = read_setting("chat.enableWorkflows")
    print("chat.agentEngine: %s (%s)" % (engine, engine_from))
    print("chat.enableWorkflows: %s (%s)" % (workflows, workflows_from))
    if engine != "v3":
        problems.append("chat.agentEngine is %s, want v3" % engine)
    if workflows != "true":
        problems.append("chat.enableWorkflows is %s, want true" % workflows)
    config = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "herdr",
        "agent-detection",
        "kiro.toml",
    )
    hook = os.path.join(
        os.environ.get("KIRO_HOOKS_DIR") or os.path.expanduser("~/.kiro/hooks"),
        "herdr-kiro.json",
    )
    state_name, via = read_lifecycle()
    print("lifecycle: %s via=%s" % (state_name or "absent", via or "absent"))
    ours = []
    for path in (hook, config):
        if os.path.isfile(path):
            ours.append(path)
            digest = marker_hash(path + ".herdr-kiro-owned")
            if digest is None:
                print("owned marker: missing for %s" % path)
            elif digest == "":
                problems.append("%s has an owned marker with no content hash" % path)
                print("hand edit: %s marker has no hash" % path)
            elif file_digest(path) != digest:
                problems.append("%s was hand edited" % path)
                print("hand edit: %s hash does not match the marker" % path)
            else:
                print("hand edit: %s matches the marker" % path)
        else:
            print("file: %s absent" % path)
    if state_name == "uninstalled" and ours:
        problems.append("lifecycle is uninstalled but files remain: %s" % ", ".join(ours))
    registered = plugin_registered() if herdr_code == 0 else None
    if state_name == "installed" and via == "plugin" and registered is False and ours:
        problems.append(
            "herdr plugin unlink/uninstall does not remove files. Still present: %s. Run ./uninstall.sh"
            % ", ".join(ours)
        )
    if os.path.isfile(config):
        body = open(config, encoding="utf-8").read()
        print("rule file: %s" % config)
        if "workflow_requires_approval" not in body:
            problems.append("installed kiro.toml has no workflow_requires_approval rule")
    elif state_name == "installed":
        problems.append("lifecycle is installed but kiro.toml is missing")
    if state_name == "installed" and not os.path.isfile(hook):
        problems.append("lifecycle is installed but the Kiro hook is missing")
    pid_dir = os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "herdr-kiro",
        "pids",
    )
    stale = dead_pidfiles(pid_dir)
    print("watcher pid files: %s" % (len(glob.glob(os.path.join(pid_dir, "*"))) if os.path.isdir(pid_dir) else 0))
    if stale:
        problems.append("stale watcher pid files: %s" % ", ".join(stale))
        sweep_stale_pidfiles()
    server_code, server_text = run([herdr_bin(), "pane", "list"], 3)
    if server_code != 0 or "server_not_running" in server_text:
        problems.append("herdr server is not running")
        print("server: not running")
    else:
        print("server: running")
    data = explain_upstream()
    remote = str(data.get("cached_remote_version") or "")
    print("cached remote kiro.toml: %s" % (remote or "unknown"))
    print("override built against: %s" % BUILT_AGAINST)
    print("local_override_shadowing_remote: %s" % data.get("local_override_shadowing_remote"))
    if newer_upstream(remote):
        problems.append("upstream Kiro detection %s is newer than %s" % (remote, BUILT_AGAINST))
        warn_upstream()
    if problems:
        print("problems: %s" % len(problems))
        for item in problems:
            print("problem: %s" % item)
        return 1
    print("problems: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
