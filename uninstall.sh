#!/bin/sh
# Remove the herdr Kiro hook, stop its watchers, and delete its state files.
set -eu
DEST="${KIRO_HOOKS_DIR:-$HOME/.kiro/hooks}"
rm -f "$DEST/herdr-kiro.json"
export HERDR_KIRO_HOME="$HOME"
python3 - << 'PY'
import glob
import os
import signal

home = os.environ.get("HERDR_KIRO_HOME") or os.path.expanduser("~")
legacy = os.environ.get("HERDR_KIRO_LEGACY_DIR") or "/tmp"
state = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"),
    "herdr-kiro",
)
me = os.getpid()
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) == me:
        continue
    try:
        command = open(f"/proc/{name}/cmdline", "rb").read().replace(b"\0", b" ").decode()
    except OSError:
        continue
    if "herdr_kiro_state.py" in command and "--watch" in command:
        try:
            os.kill(int(name), signal.SIGTERM)
        except OSError:
            pass
paths = []
if os.path.isdir(state):
    for name in os.listdir(state):
        paths.append(os.path.join(state, name))
paths.extend(glob.glob(os.path.join(legacy, "herdr-kiro-*.json")))
paths.extend(glob.glob(os.path.join(legacy, "herdr-kiro-hook.log")))
for path in paths:
    try:
        os.remove(path)
    except OSError:
        pass
PY
echo "Removed $DEST/herdr-kiro.json"
