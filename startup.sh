#!/bin/sh
# Repair the hook only when the last explicit install has not been undone.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BASE="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-kiro/lifecycle"
if [ -f "$BASE" ] && [ "$(head -n 1 "$BASE")" = "installed" ]; then
  exec sh "$ROOT/install.sh"
fi
echo "startup: lifecycle is not installed; leaving the Kiro hook and kiro.toml alone" >&2
exit 0
