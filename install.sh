#!/bin/sh
# Install the Kiro hook that reports herdr pane state during workflow runs.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
HOOK="$ROOT/prototype/herdr_kiro_state.py"
DEST="${KIRO_HOOKS_DIR:-$HOME/.kiro/hooks}"
chmod +x "$HOOK"
mkdir -p "$DEST"
cat > "$DEST/herdr-kiro.json" << EOF
{
  "version": "v1",
  "hooks": [
    {
      "name": "herdr-kiro-SessionStart",
      "trigger": "SessionStart",
      "timeout": 10,
      "action": { "type": "command", "command": "$HOOK SessionStart" }
    },
    {
      "name": "herdr-kiro-UserPromptSubmit",
      "trigger": "UserPromptSubmit",
      "timeout": 10,
      "action": { "type": "command", "command": "$HOOK UserPromptSubmit" }
    },
    {
      "name": "herdr-kiro-PreToolUse",
      "trigger": "PreToolUse",
      "timeout": 10,
      "action": { "type": "command", "command": "$HOOK PreToolUse" }
    },
    {
      "name": "herdr-kiro-PostToolUse",
      "trigger": "PostToolUse",
      "timeout": 10,
      "action": { "type": "command", "command": "$HOOK PostToolUse" }
    },
    {
      "name": "herdr-kiro-Stop",
      "trigger": "Stop",
      "timeout": 10,
      "action": { "type": "command", "command": "$HOOK Stop" }
    }
  ]
}
EOF
echo "Installed $DEST/herdr-kiro.json"
echo "Restart Kiro CLI so it loads the hook. Start it with: kiro-cli chat --v3"
