#!/usr/bin/env python3
"""Report herdr, Kiro, and this hook. Exit non-zero when something is wrong."""

from __future__ import annotations

import errno
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from herdr_kiro_state import __version__, commit_pane, fresh_state, herdr_bin, ownership

BUILT_AGAINST = "2026.09.19.1"
SETTINGS_TIMEOUT = 2


def version_key(text):
    parts = []
    for piece in (text or "").split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def cli_version(text):
    """The first dotted number in `kiro-cli 3.0.0`."""
    for token in (text or "").split():
        key = version_key(token)
        if key is not None:
            return key
    return None


def cli_supports_v3_default(cli_key):
    """CLI 3.0 starts interactive chat on V3 when chat.agentEngine is unset."""
    return cli_key is not None and cli_key >= (3,)


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
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
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
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return "", "", ""
    state = lines[0].strip() if lines else ""
    via = ""
    pending = ""
    for line in lines[1:]:
        if line.startswith("via="):
            via = line.split("=", 1)[1].strip()
        elif line.startswith("pending_unlink="):
            pending = line.split("=", 1)[1].strip()
    return state, via, pending


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
        f"WARNING: upstream Kiro detection {remote} is newer than {BUILT_AGAINST}, the version this override was built against.\n"
    )
    sys.stderr.write(
        f"local_override_shadowing_remote={shadowing}. The installed kiro.toml replaces the remote manifest.\n"
    )
    sys.stderr.write(
        "Resolve it by moving ~/.config/herdr/agent-detection/kiro.toml aside and running "
        f"herdr server reload-agent-manifests, or by rebuilding this override against {remote} and reinstalling.\n"
    )
    return 1


def plugin_registered():
    code, text = run([herdr_bin(), "plugin", "list"], 5)
    if code != 0:
        return None
    return "herdr-kiro" in text


def dead_pidfiles(directory):
    """Pid files whose process is gone. EPERM means the process is still there."""
    found = []
    if not os.path.isdir(directory):
        return found
    for name in os.listdir(directory):
        if not name.isdigit():
            continue
        try:
            os.kill(int(name), 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                found.append(os.path.join(directory, name))
    return found


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--warn-upstream":
        return warn_upstream()
    problems = []
    print(f"herdr-kiro {__version__}")
    herdr_code, herdr_text = run([herdr_bin(), "--version"], 3)
    print("herdr: %s" % (herdr_text if herdr_code == 0 and herdr_text else "not on PATH"))
    if herdr_code != 0 or not herdr_text:
        problems.append("herdr is not on PATH")
    code, kiro_text = run(["kiro-cli", "--version"], 3)
    print("kiro-cli: %s" % (kiro_text if code == 0 and kiro_text else "not on PATH"))
    if code != 0 or not kiro_text:
        problems.append("kiro-cli is not on PATH")
    cli_key = cli_version(kiro_text) if code == 0 and kiro_text else None
    if code == 0 and kiro_text and not cli_supports_v3_default(cli_key):
        problems.append("kiro-cli is older than 3.0, the supported CLI")
    engine, engine_from = read_setting("chat.agentEngine")
    workflows, workflows_from = read_setting("chat.enableWorkflows")
    # A saved chat.agentEngine still selects the harness. Unset on 3.0 does not:
    # interactive chat starts on V3.
    if cli_supports_v3_default(cli_key) and engine == "unknown":
        print(f"chat.agentEngine: unset ({engine_from}); CLI 3.0 interactive default is V3")
    else:
        print(f"chat.agentEngine: {engine} ({engine_from})")
        if engine != "v3":
            problems.append(f"chat.agentEngine is {engine}, want v3")
    print(f"chat.enableWorkflows: {workflows} ({workflows_from})")
    if workflows != "true":
        problems.append(f"chat.enableWorkflows is {workflows}, want true")
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
    state_name, via, pending = read_lifecycle()
    print("lifecycle: {} via={} pending_unlink={}".format(state_name or "absent", via or "absent", pending or "no"))
    owned_paths = []
    for path in (hook, config):
        if not os.path.isfile(path):
            print(f"file: {path} absent")
            continue
        kind = ownership(path)
        if kind == "absent":
            print(f"info: {path} is a user file, not removed by this hook")
        elif kind == "nohash":
            print(f"info: {path} has an owned marker with no content hash")
            if state_name == "installed":
                problems.append(f"{path} has an owned marker with no content hash")
        elif kind == "differs":
            problems.append(f"{path} was hand edited")
            print(f"hand edit: {path} hash does not match the marker")
        elif kind == "match":
            owned_paths.append(path)
            print(f"hand edit: {path} matches the marker")
        else:
            raise RuntimeError(f"unknown ownership {kind}")
    if state_name == "uninstalled" and owned_paths:
        problems.append("lifecycle is uninstalled but our files remain: {}".format(", ".join(owned_paths)))
    registered = plugin_registered() if herdr_code == 0 else None
    if pending or (state_name == "uninstalled" and registered is True):
        problems.append(
            "herdr-kiro is still registered. startup.sh finishes that unlink when a server is up. "
            "doctor does not change the registration."
        )
    if state_name == "installed" and via == "plugin" and registered is False and owned_paths:
        problems.append(
            "herdr plugin unlink does not delete files. Our files are still present: {}. Run ./uninstall.sh".format(
                ", ".join(owned_paths)
            )
        )
    if os.path.isfile(config) and config in owned_paths:
        with open(config, encoding="utf-8") as handle:
            body = handle.read()
        print(f"rule file: {config}")
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
        problems.append("stale watcher pid files: {}".format(", ".join(stale)))
        commit_pane(fresh_state(), "reap")
    server_code, server_text = run([herdr_bin(), "pane", "list"], 3)
    if server_code != 0 or "server_not_running" in server_text:
        problems.append("herdr server is not running")
        print("server: not running")
    else:
        print("server: running")
    data = explain_upstream()
    remote = str(data.get("cached_remote_version") or "")
    print("cached remote kiro.toml: %s" % (remote or "unknown"))
    print(f"override built against: {BUILT_AGAINST}")
    print("local_override_shadowing_remote: {}".format(data.get("local_override_shadowing_remote")))
    if newer_upstream(remote):
        problems.append(f"upstream Kiro detection {remote} is newer than {BUILT_AGAINST}")
        warn_upstream()
    if problems:
        print(f"problems: {len(problems)}")
        for item in problems:
            print(f"problem: {item}")
        return 1
    print("problems: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
