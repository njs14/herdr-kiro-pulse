#!/bin/sh
# Install the Kiro hook that reports herdr pane state during workflow runs.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
HOOK="$ROOT/prototype/herdr_kiro_state.py"
DEST="${KIRO_HOOKS_DIR:-$HOME/.kiro/hooks}"
chmod +x "$HOOK"
mkdir -p "$DEST"
TARGET="$DEST/herdr-kiro.json"
if [ -f "$TARGET" ]; then
  cp -p "$TARGET" "$TARGET.bak.$(date +%Y%m%d%H%M%S)"
fi
export HERDR_KIRO_HOOK="$HOOK"
python3 - "$TARGET" << 'PY'
import json
import os
import shlex
import sys

hook = os.environ["HERDR_KIRO_HOOK"]
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
document = {
    "version": "v1",
    "hooks": [
        {
            "name": f"herdr-kiro-{event}",
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
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
PY
RULES="$ROOT/prototype/kiro-detection.toml"
if [ -f "$RULES" ]; then
  CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/agent-detection"
  mkdir -p "$CONFIG"
  if [ -f "$CONFIG/kiro.toml" ]; then
    cp -p "$CONFIG/kiro.toml" "$CONFIG/kiro.toml.bak.$(date +%Y%m%d%H%M%S)"
  fi
  cp "$RULES" "$CONFIG/kiro.toml"
  if command -v herdr >/dev/null 2>&1; then
    if [ -n "${HERDR_SESSION:-}" ]; then
      herdr --session "$HERDR_SESSION" server reload-agent-manifests >/dev/null 2>&1 || true
    fi
    herdr server reload-agent-manifests >/dev/null 2>&1 || true
  fi
  echo "Installed $CONFIG/kiro.toml"
fi
echo "Installed $TARGET"
echo "Restart Kiro CLI so it loads the hook. Start it with: kiro-cli chat --v3"
