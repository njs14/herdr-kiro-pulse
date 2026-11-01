#!/bin/sh
# Install the Kiro hook that reports herdr pane state during workflow runs.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
HOOK="$ROOT/prototype/herdr_kiro_state.py"
DEST="${KIRO_HOOKS_DIR:-$HOME/.kiro/hooks}"
RULES="$ROOT/prototype/kiro-detection.toml"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/agent-detection"
chmod +x "$HOOK"
mkdir -p "$DEST"
export HERDR_KIRO_HOOK="$HOOK"
export HERDR_KIRO_RULES="$RULES"
export HERDR_KIRO_CONFIG="$CONFIG"
python3 - "$DEST/herdr-kiro.json" "$CONFIG/kiro.toml" << 'PY'
import json
import os
import shlex
import sys

MARKER = ".herdr-kiro-owned"


def install_owned(dest, payload):
    directory = os.path.dirname(dest)
    if directory:
        os.makedirs(directory, exist_ok=True)
    marker = dest + MARKER
    original = dest + ".original"
    if os.path.isfile(dest) and not os.path.isfile(marker):
        if not os.path.isfile(original):
            with open(dest, "rb") as source, open(original, "wb") as target:
                target.write(source.read())
    with open(dest, "wb") as handle:
        handle.write(payload)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("herdr-kiro-owned\n")


hook = os.environ["HERDR_KIRO_HOOK"]
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
document = {
    "version": "v1",
    "hooks": [
        {
            "name": "herdr-kiro-%s" % event,
            "trigger": event,
            "timeout": 10,
            "action": {
                "type": "command",
                "command": shlex.quote(hook) + " " + event,
            },
        }
        for event in events
    ],
}
install_owned(sys.argv[1], (json.dumps(document, indent=2) + "\n").encode("utf-8"))
rules = os.environ.get("HERDR_KIRO_RULES") or ""
if rules and os.path.isfile(rules):
    with open(rules, "rb") as handle:
        install_owned(sys.argv[2], handle.read())
PY
if [ -f "$RULES" ] && command -v herdr >/dev/null 2>&1; then
  if [ -n "${HERDR_SESSION:-}" ]; then
    herdr --session "$HERDR_SESSION" server reload-agent-manifests >/dev/null 2>&1 || true
  fi
  herdr server reload-agent-manifests >/dev/null 2>&1 || true
fi
echo "Installed $DEST/herdr-kiro.json"
if [ -f "$RULES" ]; then
  echo "Installed $CONFIG/kiro.toml"
fi
echo "Restart Kiro CLI so it loads the hook. Start it with: kiro-cli chat --v3"
