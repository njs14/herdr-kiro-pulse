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
HERDR="${HERDR_BIN_PATH:-herdr}"
export HERDR_KIRO_HOOK="$HOOK"
export HERDR_KIRO_RULES="$RULES"
export HERDR_KIRO_CONFIG="$CONFIG"
python3 - "$DEST/herdr-kiro.json" "$CONFIG/kiro.toml" << 'PY'
import datetime
import hashlib
import json
import os
import shlex
import sys

MARKER = ".herdr-kiro-owned"


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


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


def write_bytes(path, payload):
    with open(path, "wb") as handle:
        handle.write(payload)


def same_backup(dest, payload):
    directory = os.path.dirname(dest) or "."
    prefix = os.path.basename(dest) + ".user-modified."
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as handle:
                if handle.read() == payload:
                    return True
        except OSError:
            continue
    return False


def unique_backup(dest):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = dest + ".user-modified." + stamp
    index = 0
    while os.path.exists(candidate):
        index += 1
        candidate = dest + ".user-modified." + stamp + "." + str(index)
    return candidate


def install_owned(dest, payload):
    directory = os.path.dirname(dest)
    if directory:
        os.makedirs(directory, exist_ok=True)
    marker = dest + MARKER
    original = dest + ".original"
    existing = None
    if os.path.isfile(dest):
        with open(dest, "rb") as handle:
            existing = handle.read()
    owned_hash = marker_hash(marker) if os.path.isfile(marker) else None
    if existing is not None and owned_hash is None and existing != payload:
        if not os.path.isfile(original):
            write_bytes(original, existing)
        else:
            with open(original, "rb") as handle:
                previous = handle.read()
            if existing != previous:
                write_bytes(unique_backup(dest), existing)
    elif existing is not None and owned_hash == "" and existing != payload:
        if not same_backup(dest, existing):
            kept = unique_backup(dest)
            write_bytes(kept, existing)
        sys.stderr.write(
            "warning: %s has no content hash and differs from this install; left in place\n"
            % dest
        )
        print("Left %s unchanged" % dest)
        return
    elif existing is not None and owned_hash and digest(existing) != owned_hash:
        backup = unique_backup(dest)
        write_bytes(backup, existing)
        sys.stderr.write("warning: %s was edited; kept %s\n" % (dest, backup))
    write_bytes(dest, payload)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("herdr-kiro-owned\nsha256 %s\n" % digest(payload))
    print("Installed %s" % dest)


hook = os.environ["HERDR_KIRO_HOOK"]
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"]
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
base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
directory = os.path.join(base, "herdr-kiro")
os.makedirs(directory, mode=0o700, exist_ok=True)
via = "plugin" if os.environ.get("HERDR_PLUGIN_ID") else "script"
lifecycle = os.path.join(directory, "lifecycle")
with open(lifecycle, "w", encoding="utf-8") as handle:
    handle.write("installed\nvia=%s\n" % via)
os.chmod(lifecycle, 0o600)
PY
reload_one() {
  # $1 is "session" or "default"
  shift
  err=$(mktemp)
  if "$HERDR" "$@" 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  if grep -q server_not_running "$err"; then
    if [ "$1" = "--session" ]; then
      echo "warning: HERDR_SESSION=${HERDR_SESSION:-} is not a running herdr server" >&2
      cat "$err" >&2
      rm -f "$err"
      return 2
    fi
    echo "note: no herdr server is running; reload happens on the next herdr start" >&2
    rm -f "$err"
    return 0
  fi
  cat "$err" >&2
  echo "warning: herdr $* failed" >&2
  rm -f "$err"
  return 1
}
reload_manifests() {
  if ! command -v "$HERDR" >/dev/null 2>&1; then
    echo "herdr is not on PATH, skipped manifest reload" >&2
    return 0
  fi
  if [ -n "${HERDR_SESSION:-}" ]; then
    reload_one session --session "$HERDR_SESSION" server reload-agent-manifests
    return $?
  fi
  reload_one default server reload-agent-manifests
  return $?
}
if [ -f "$RULES" ]; then
  reload_manifests
fi
python3 "$ROOT/prototype/herdr_kiro_doctor.py" --warn-upstream || true
echo "Restart Kiro CLI so it loads the hook. Start it with: kiro-cli chat --v3"
