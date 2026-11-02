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
import os
import signal
import sys

root = os.environ.get("HERDR_KIRO_ROOT") or ""
sys.path.insert(0, os.path.join(root, "prototype"))
from herdr_kiro_state import begin_tick, ownership, sweep_owned_temp, watcher_identity

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


def remove_ours(dest):
    """Delete only when the marker sha256 matches. Otherwise leave the bytes.

    A different hash is renamed to *.user-modified.<ts> and *.original is
    restored. No marker, or a marker with no hash, leaves the file unchanged.
    doctor uses the same ownership() check.
    """
    marker = dest + MARKER
    original = dest + ".original"
    kind = ownership(dest)
    if kind in ("absent", "nohash"):
        print("Left %s unchanged" % dest)
        return
    removed = False
    if kind == "differs" and os.path.lexists(dest) and not os.path.islink(dest):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = dest + ".user-modified." + stamp
        index = 0
        while os.path.exists(backup):
            index += 1
            backup = dest + ".user-modified." + stamp + "." + str(index)
        os.replace(dest, backup)
        print("Left %s as %s" % (dest, backup))
    elif kind == "match" and os.path.lexists(dest) and not os.path.islink(dest):
        os.remove(dest)
        removed = True
    else:
        print("Left %s unchanged" % dest)
        return
    if os.path.isfile(marker) and not os.path.islink(marker):
        os.remove(marker)
    if (
        os.path.isfile(original)
        and not os.path.islink(original)
        and not os.path.lexists(dest)
    ):
        os.replace(original, dest)
        print("Restored %s" % dest)
    elif removed:
        print("Removed %s" % dest)


def stop_tracked_watchers(directory):
    """Stop a watcher only when uid, high-resolution start, and comm all match."""
    pid_dir = os.path.join(directory, "pids")
    if not os.path.isdir(pid_dir):
        return
    begin_tick()
    for name in os.listdir(pid_dir):
        path = os.path.join(pid_dir, name)
        if not name.isdigit():
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        recorded_uid = lines[0].strip() if lines else ""
        recorded_start = lines[1].strip() if len(lines) > 1 else ""
        recorded_comm = lines[2].strip() if len(lines) > 2 else ""
        if recorded_uid != str(uid):
            continue
        pid = int(name)
        ident = watcher_identity(pid)
        if ident is None:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if ident.get("error"):
            continue
        if not recorded_start or not recorded_comm:
            continue
        if ident["start"] != recorded_start or ident["comm"] != recorded_comm:
            continue
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
