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
import errno
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
    """Write payload without following a symlink at path.

    The temp file is created with O_CREAT|O_EXCL|O_NOFOLLOW and mode 0600,
    then renamed onto path. A symlink destination is left alone.
    """
    if os.path.islink(path):
        sys.stderr.write("left %s alone\n" % path)
        return False
    directory = os.path.dirname(path) or "."
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = None
    tmp = None
    for attempt in range(128):
        tmp = os.path.join(
            directory, ".%s.%s.%s.tmp" % (os.path.basename(path), os.getpid(), attempt)
        )
        try:
            fd = os.open(tmp, flags, 0o600)
            break
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ELOOP):
                continue
            raise
    else:
        raise OSError("could not create a temp file for %s" % path)
    try:
        view = os.fdopen(fd, "wb")
        fd = None
        view.write(payload)
        view.close()
    except Exception:
        if fd is not None:
            os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    if os.path.islink(path):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        sys.stderr.write("left %s alone\n" % path)
        return False
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


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
    if os.path.islink(dest):
        sys.stderr.write("left %s alone\n" % dest)
        print("Left %s unchanged" % dest)
        return False
    marker = dest + MARKER
    original = dest + ".original"
    existing = None
    if os.path.isfile(dest) and not os.path.islink(dest):
        with open(dest, "rb") as handle:
            existing = handle.read()
    owned_hash = marker_hash(marker) if os.path.isfile(marker) and not os.path.islink(marker) else None
    if existing is not None and owned_hash is None and existing != payload:
        if not os.path.isfile(original):
            if not write_bytes(original, existing):
                print("Left %s unchanged" % dest)
                return False
        else:
            with open(original, "rb") as handle:
                previous = handle.read()
            if existing != previous:
                if not write_bytes(unique_backup(dest), existing):
                    print("Left %s unchanged" % dest)
                    return False
    elif existing is not None and owned_hash == "" and existing != payload:
        if not same_backup(dest, existing):
            kept = unique_backup(dest)
            if not write_bytes(kept, existing):
                print("Left %s unchanged" % dest)
                return False
        sys.stderr.write(
            "warning: %s has no content hash and differs from this install; left in place\n"
            % dest
        )
        print("Left %s unchanged" % dest)
        return True
    elif existing is not None and owned_hash and digest(existing) != owned_hash:
        backup = unique_backup(dest)
        if not write_bytes(backup, existing):
            print("Left %s unchanged" % dest)
            return False
        sys.stderr.write("warning: %s was edited; kept %s\n" % (dest, backup))
    if not write_bytes(dest, payload):
        print("Left %s unchanged" % dest)
        return False
    body = ("herdr-kiro-owned\nsha256 %s\n" % digest(payload)).encode("utf-8")
    if not write_bytes(marker, body):
        print("Left %s unchanged" % dest)
        return False
    print("Installed %s" % dest)
    return True


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
ok = install_owned(sys.argv[1], (json.dumps(document, indent=2) + "\n").encode("utf-8"))
rules = os.environ.get("HERDR_KIRO_RULES") or ""
if ok and rules and os.path.isfile(rules):
    with open(rules, "rb") as handle:
        ok = install_owned(sys.argv[2], handle.read()) and ok
if not ok:
    sys.exit(1)
base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
directory = os.path.join(base, "herdr-kiro")
os.makedirs(directory, mode=0o700, exist_ok=True)
try:
    os.chmod(directory, 0o700)
except OSError:
    pass
via = "plugin" if os.environ.get("HERDR_PLUGIN_ID") else "script"
lifecycle = os.path.join(directory, "lifecycle")
if not write_bytes(lifecycle, ("installed\nvia=%s\n" % via).encode("utf-8")):
    sys.exit(1)
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
