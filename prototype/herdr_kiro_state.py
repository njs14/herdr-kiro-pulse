#!/usr/bin/env python3
"""Keep a herdr pane working for the whole Kiro workflow, then idle after a grace period.

Workflow steps are separate Kiro sessions. The parent pane paints its idle prompt
while they run, so screen detection calls the pane idle. Each step session
inherits HERDR_PANE_ID and fires the same hooks.

Herdr also drops a custom hook's working authority when a foreground turn
repaints from the "Kiro is working" footer to the idle prompt, about 0.2s
after Stop, and it remembers the sequence number. A single idle report 3s
later arrives after the pane is already idle. This process re-sends working
until the grace ends, then reports idle and releases.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

SOURCE = "hook:kiro"
AGENT = "kiro"
SETTLE_SECS = 3.0
STALE_SECS = 1800.0
TICK_SECS = 0.15
BUSY_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
LOG_PATH = os.environ.get("HERDR_KIRO_LOG", "/tmp/herdr-kiro-hook.log")


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def state_path(pane_id: str) -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"herdr-kiro-{pane_id.replace(':', '_')}.json")


def fresh_state() -> dict:
    return {
        "busy": {},
        "seq": 0,
        "grace_until": 0.0,
        "reported": None,
        "watcher": 0,
        "watcher_at": 0.0,
        "grace_started": 0.0,
    }


def normalize(state: dict, now: float) -> dict:
    busy = state.get("busy") if isinstance(state.get("busy"), dict) else {}
    kept = {}
    for session, stamp in busy.items():
        try:
            seen = float(stamp)
        except (TypeError, ValueError):
            continue
        if now - seen < STALE_SECS:
            kept[str(session)] = seen
    try:
        seq = int(state.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    try:
        grace_until = float(state.get("grace_until") or 0)
    except (TypeError, ValueError):
        grace_until = 0.0
    try:
        watcher = int(state.get("watcher") or 0)
    except (TypeError, ValueError):
        watcher = 0
    try:
        watcher_at = float(state.get("watcher_at") or 0)
    except (TypeError, ValueError):
        watcher_at = 0.0
    try:
        grace_started = float(state.get("grace_started") or 0)
    except (TypeError, ValueError):
        grace_started = 0.0
    return {
        "busy": kept,
        "seq": seq,
        "grace_until": grace_until,
        "reported": state.get("reported"),
        "watcher": watcher,
        "watcher_at": watcher_at,
        "grace_started": grace_started,
    }


def apply_event(state: dict, event: str, session: str, now: float) -> dict:
    """Fold one hook event into pane state. Pure aside from the clock passed in."""
    nxt = normalize(state, now)
    if event in BUSY_EVENTS:
        nxt["busy"][session] = now
        nxt["grace_until"] = 0.0
    elif event == "Stop":
        nxt["busy"].pop(session, None)
        if not nxt["busy"]:
            nxt["grace_until"] = now + SETTLE_SECS
            nxt["grace_started"] = now
    return nxt


def note_banner(state: dict, banner: bool, now: float) -> dict:
    """A visible workflow banner means the run is still going, even with no sessions."""
    nxt = normalize(state, now)
    if banner and not nxt["busy"]:
        nxt["grace_until"] = now + SETTLE_SECS
    return nxt


def next_action(busy_count: int, grace_until: float, banner: bool, now: float) -> str:
    if busy_count > 0 or banner or now < grace_until:
        return "working"
    return "idle"


def approval_text(text: str) -> bool:
    """True when the pane is showing a Kiro approval chooser herdr already treats as blocked."""
    lowered = text.lower()
    markers = (
        "requires approval",
        "esc to close",
        "always allow",
        "always deny",
        "yes, single permission",
        "tool approval",
        "approve all pending",
    )
    return any(marker in lowered for marker in markers)


def update(path: str, fn):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        try:
            loaded = json.loads(raw) if raw.strip() else fresh_state()
        except ValueError:
            loaded = fresh_state()
        if not isinstance(loaded, dict):
            loaded = fresh_state()
        state = normalize(loaded, time.time())
        fn(state)
        state = normalize(state, time.time())
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state))
        handle.flush()
        return state


def herdr(*args: str) -> bool:
    log("herdr " + " ".join(args))
    try:
        completed = subprocess.run(
            ["herdr", "pane", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def send_report(pane: str, path: str, working: bool) -> bool:
    def bump(state: dict) -> None:
        state["seq"] += 1
        state["reported"] = "working" if working else "idle"
        if not working:
            state["grace_until"] = 0.0

    seq = str(update(path, bump)["seq"])
    kind = "working" if working else "idle"
    ok = herdr(
        "report-agent",
        pane,
        "--source",
        SOURCE,
        "--agent",
        AGENT,
        "--state",
        kind,
        "--seq",
        seq,
    )
    if working:
        return ok

    def bump_release(state: dict) -> None:
        state["seq"] += 1

    release_seq = str(update(path, bump_release)["seq"])
    return herdr(
        "release-agent",
        pane,
        "--source",
        SOURCE,
        "--agent",
        AGENT,
        "--seq",
        release_seq,
    ) and ok


def pane_text(pane: str) -> str:
    try:
        return subprocess.check_output(
            ["herdr", "pane", "read", pane, "--source", "visible", "--lines", "24"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def workflow_banner_from(text: str) -> bool:
    for line in text.splitlines():
        lowered = line.lower()
        if "running" not in lowered:
            continue
        if "▶" in line or "ctrl+g" in lowered:
            return True
    return False


def workflow_banner(pane: str) -> bool:
    return workflow_banner_from(pane_text(pane))


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ensure_watcher(pane: str, path: str) -> None:
    decision = {"spawn": False}

    def claim(state: dict) -> None:
        now = time.time()
        pid = int(state.get("watcher") or 0)
        claimed = pid == -1 and now - float(state.get("watcher_at") or 0) < 2
        if claimed or alive(pid):
            return
        state["watcher"] = -1
        state["watcher_at"] = now
        decision["spawn"] = True

    update(path, claim)
    if not decision["spawn"]:
        return
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--watch", pane, path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    update(path, lambda state: state.__setitem__("watcher", proc.pid))
    log(f"watcher spawned pid={proc.pid}")


def watch_loop(pane: str, path: str) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    update(path, lambda state: state.__setitem__("watcher", os.getpid()))
    log(f"watcher loop pane={pane}")
    sent_idle = False
    misses = 0
    delay = TICK_SECS
    while True:
        time.sleep(delay)
        state = update(path, lambda _state: None)
        screen = pane_text(pane)
        if approval_text(screen):
            delay = TICK_SECS
            continue
        banner = False if state["busy"] else workflow_banner_from(screen)
        if banner:
            def extend(current: dict) -> None:
                revised = note_banner(current, True, time.time())
                current["grace_until"] = revised["grace_until"]

            state = update(path, extend)
        action = next_action(
            len(state["busy"]),
            float(state["grace_until"]),
            banner,
            time.time(),
        )
        if action == "working":
            sent_idle = False
            started = float(state.get("grace_started") or 0)
            drop_window = not state["busy"] and started <= time.time() < started + 0.5
            delay = 0.05 if drop_window else TICK_SECS
            ok = send_report(pane, path, True)
        elif sent_idle:
            delay = 1.0
            ok = True
        else:
            delay = 1.0
            ok = send_report(pane, path, False)
            sent_idle = True
            log("watcher idle")
        if ok:
            misses = 0
        else:
            misses += 1
            if misses >= 30:
                log("watcher stopping after herdr failures")
                return


def session_of(payload: dict) -> str:
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event == "--watch":
        watch_loop(sys.argv[2], sys.argv[3])
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not pane:
        return 0
    path = state_path(pane)
    now = time.time()
    session = session_of(payload)
    if event not in BUSY_EVENTS and event != "Stop":
        return 0
    log(f"event={event} session={session} pane={pane}")

    def fold(state: dict) -> None:
        revised = apply_event(state, event, session, now)
        state.clear()
        state.update(revised)

    state = update(path, fold)
    log(
        f"state event={event} busy={len(state['busy'])} "
        f"grace_until={state['grace_until']:.3f}"
    )
    ensure_watcher(pane, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
