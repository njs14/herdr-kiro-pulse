#!/bin/sh
# Repair the hook only when the last explicit install has not been undone.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BASE="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-kiro"
MARK="$BASE/lifecycle"
mkdir -p "$BASE"
if [ ! -f "$MARK" ]; then
  python3 - "$BASE" << 'PY'
import hashlib
import os
import sys

base = sys.argv[1]
home = os.environ.get("HOME") or ""
hook = os.path.join(os.environ.get("KIRO_HOOKS_DIR") or os.path.join(home, ".kiro", "hooks"), "herdr-kiro.json")
config = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
    "herdr",
    "agent-detection",
    "kiro.toml",
)

def matches(path):
    marker = path + ".herdr-kiro-owned"
    try:
        text = open(marker, encoding="utf-8").read()
        body = open(path, "rb").read()
    except OSError:
        return False
    hashed = ""
    for line in text.splitlines():
        if line.startswith("sha256 "):
            hashed = line.split(" ", 1)[1].strip()
    return bool(hashed) and hashlib.sha256(body).hexdigest() == hashed

if matches(hook) or matches(config):
    path = os.path.join(base, "lifecycle")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("installed\nvia=upgrade\n")
    os.chmod(path, 0o600)
PY
fi
HERDR="${HERDR_BIN_PATH:-herdr}"
if [ -f "$MARK" ] && grep -q '^pending_unlink=' "$MARK" && command -v "$HERDR" >/dev/null 2>&1; then
  "$HERDR" plugin unlink herdr-kiro >/dev/null 2>&1 || true
  if ! "$HERDR" plugin list 2>/dev/null | grep -q herdr-kiro; then
    grep -v '^pending_unlink=' "$MARK" > "$MARK.next" || true
    mv "$MARK.next" "$MARK"
    chmod 600 "$MARK" || true
  fi
fi
if [ -f "$MARK" ] && [ "$(head -n 1 "$MARK")" = "installed" ]; then
  exec sh "$ROOT/install.sh"
fi
echo "startup: lifecycle is not installed; leaving the Kiro hook and kiro.toml alone" >&2
exit 0
