#!/bin/sh
# Remove the herdr Kiro hook, stop its watchers, and delete its state files.
set -eu
DEST="${KIRO_HOOKS_DIR:-$HOME/.kiro/hooks}"
export HERDR_KIRO_HOME="$HOME"
export HERDR_KIRO_HOOK_DIR="$DEST"
python3 - << 'PY'
import datetime
import glob
import os
import signal
import subprocess

MARKER = ".herdr-kiro-owned"
home = os.environ.get("HERDR_KIRO_HOME") or os.path.expanduser("~")
hook_dir = os.environ.get("HERDR_KIRO_HOOK_DIR") or os.path.join(home, ".kiro", "hooks")
legacy = os.environ.get("HERDR_KIRO_LEGACY_DIR") or "/tmp"
state = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"),
    "herdr-kiro",
)
config = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
    "herdr",
    "agent-detection",
)
uid = os.getuid()


def start_token(text):
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    try:
        parsed = datetime.datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return cleaned
    return str(int(parsed.timestamp()))


def remove_ours(dest):
    marker = dest + MARKER
    original = dest + ".original"
    owned = os.path.isfile(marker)
    if not owned and os.path.isfile(dest):
        try:
            body = open(dest, encoding="utf-8").read()
        except OSError:
            body = ""
        owned = "workflow_banner_working" in body or "herdr-kiro-SessionStart" in body
    if not owned:
        return
    if os.path.isfile(dest):
        os.remove(dest)
    if os.path.isfile(marker):
        os.remove(marker)
    if os.path.isfile(original):
        os.replace(original, dest)


def process_start(pid):
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return start_token(completed.stdout or "")


def stop_tracked_watchers(directory):
    pid_dir = os.path.join(directory, "pids")
    if not os.path.isdir(pid_dir):
        return
    for name in os.listdir(pid_dir):
        path = os.path.join(pid_dir, name)
        if not name.isdigit():
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        recorded_uid = lines[0].strip() if lines else ""
        recorded_start = start_token(lines[1]) if len(lines) > 1 else ""
        if recorded_uid != str(uid):
            continue
        pid = int(name)
        current = process_start(pid)
        if recorded_start and current and recorded_start != current:
            continue
        if current:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            os.remove(path)
        except OSError:
            pass


remove_ours(os.path.join(hook_dir, "herdr-kiro.json"))
remove_ours(os.path.join(config, "kiro.toml"))
stop_tracked_watchers(state)
if os.path.isdir(state):
    for name in os.listdir(state):
        if name == "pids" or name.endswith(".seq"):
            continue
        path = os.path.join(state, name)
        if os.path.isdir(path):
            continue
        try:
            os.remove(path)
        except OSError:
            pass
for path in glob.glob(os.path.join(legacy, "herdr-kiro-*.json")):
    try:
        os.remove(path)
    except OSError:
        pass
legacy_log = os.path.join(legacy, "herdr-kiro-hook.log")
if os.path.isfile(legacy_log):
    try:
        os.remove(legacy_log)
    except OSError:
        pass
PY
if command -v herdr >/dev/null 2>&1; then
  herdr server reload-agent-manifests >/dev/null 2>&1 || true
fi
echo "Removed $DEST/herdr-kiro.json"
