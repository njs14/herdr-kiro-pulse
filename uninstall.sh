#!/bin/sh
# Remove the herdr Kiro hook, stop its watchers, and delete its state files.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
DEST="${KIRO_HOOKS_DIR:-$HOME/.kiro/hooks}"
HERDR="${HERDR_BIN_PATH:-herdr}"
export HERDR_KIRO_HOME="$HOME"
export HERDR_KIRO_HOOK_DIR="$DEST"
export HERDR_KIRO_ROOT="$ROOT"
python3 - << 'PY'
import datetime
import hashlib
import os
import signal
import subprocess
import sys

root = os.environ.get("HERDR_KIRO_ROOT") or ""
sys.path.insert(0, os.path.join(root, "prototype"))
from herdr_kiro_state import sweep_owned_temp

MARKER = ".herdr-kiro-owned"
home = os.environ.get("HERDR_KIRO_HOME") or os.path.expanduser("~")
hook_dir = os.environ.get("HERDR_KIRO_HOOK_DIR") or os.path.join(home, ".kiro", "hooks")
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


def file_digest(path):
    hashed = hashlib.sha256()
    with open(path, "rb") as handle:
        hashed.update(handle.read())
    return hashed.hexdigest()


def remove_ours(dest):
    marker = dest + MARKER
    original = dest + ".original"
    owned_hash = marker_hash(marker) if os.path.isfile(marker) else None
    owned = owned_hash is not None
    if not owned and os.path.isfile(dest):
        try:
            body = open(dest, encoding="utf-8").read()
        except OSError:
            body = ""
        owned = "workflow_banner_working" in body or "herdr-kiro-SessionStart" in body
        owned_hash = "" if owned else None
    if not owned:
        print("Left %s unchanged" % dest)
        return
    removed = False
    if os.path.isfile(dest):
        changed = bool(owned_hash) and file_digest(dest) != owned_hash
        if changed:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = dest + ".user-modified." + stamp
            index = 0
            while os.path.exists(backup):
                index += 1
                backup = dest + ".user-modified." + stamp + "." + str(index)
            os.replace(dest, backup)
            print("Left %s as %s" % (dest, backup))
        else:
            os.remove(dest)
            removed = True
    if os.path.isfile(marker):
        os.remove(marker)
    if os.path.isfile(original) and not os.path.exists(dest):
        os.replace(original, dest)
        print("Restored %s" % dest)
    elif removed:
        print("Removed %s" % dest)


def process_start(pid):
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
            env=dict(os.environ, LC_ALL="C"),
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
        if not recorded_start:
            continue
        pid = int(name)
        current = process_start(pid)
        if current and recorded_start != current:
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
base = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
directory = os.path.join(base, "herdr-kiro")
os.makedirs(directory, mode=0o700, exist_ok=True)
lifecycle = os.path.join(directory, "lifecycle")
with open(lifecycle, "w", encoding="utf-8") as handle:
    handle.write("uninstalled\n")
os.chmod(lifecycle, 0o600)
sweep_owned_temp()
PY
reload_one() {
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
herdr_api_socket() {
  if [ -n "${HERDR_SOCKET_PATH:-}" ]; then
    printf '%s\n' "$HERDR_SOCKET_PATH"
    return
  fi
  config="${XDG_CONFIG_HOME:-$HOME/.config}/herdr"
  if [ -n "${HERDR_SESSION:-}" ]; then
    printf '%s\n' "$config/sessions/$HERDR_SESSION/herdr.sock"
  else
    printf '%s\n' "$config/herdr.sock"
  fi
}
pending=0
if command -v "$HERDR" >/dev/null 2>&1; then
  list_rc=0
  list_out=$("$HERDR" plugin list 2>&1) || list_rc=$?
  if printf '%s\n' "$list_out" | grep -q herdr-kiro; then
    "$HERDR" plugin unlink herdr-kiro >/dev/null 2>&1 || true
    list_rc=0
    list_out=$("$HERDR" plugin list 2>&1) || list_rc=$?
  fi
  still=0
  if printf '%s\n' "$list_out" | grep -q herdr-kiro; then
    still=1
  fi
  missing_text=0
  if printf '%s\n' "$list_out" | grep -q server_not_running; then
    missing_text=1
  fi
  sock=$(herdr_api_socket)
  sock_present=0
  if [ -e "$sock" ]; then
    sock_present=1
  fi
  # A live server is a successful list plus the API socket. A non-zero exit
  # or a missing socket is pending even when the error text changed.
  if [ "$still" -eq 1 ] || [ "$missing_text" -eq 1 ] || [ "$list_rc" -ne 0 ] || [ "$sock_present" -eq 0 ]; then
    pending=1
  fi
fi
LIFE="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-kiro/lifecycle"
if [ "$pending" = 1 ] && [ -f "$LIFE" ] && ! grep -qx 'pending_unlink=herdr-kiro' "$LIFE"; then
  printf 'pending_unlink=herdr-kiro\n' >> "$LIFE"
fi
if command -v "$HERDR" >/dev/null 2>&1; then
  if [ -n "${HERDR_SESSION:-}" ]; then
    reload_one session --session "$HERDR_SESSION" server reload-agent-manifests || exit $?
  else
    reload_one default server reload-agent-manifests || exit $?
  fi
fi
