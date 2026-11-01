#!/usr/bin/env python3
"""Keep a herdr pane working for the whole Kiro workflow, then idle after a grace period.

Workflow steps are separate Kiro sessions. The parent pane paints its idle prompt
while they run, so screen detection calls the pane idle. Each step session
inherits HERDR_PANE_ID and fires the same hooks.

Herdr also drops a custom hook's working authority when a foreground turn
repaints from the "Kiro is working" footer to the idle prompt, about 0.2s
after Stop, and it remembers the sequence number. A single idle report 3s
later arrives after the pane is already idle. This process re-sends working
until the grace ends, then reports idle, releases, and exits.
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
# The footer repaint was measured at 0.18s, and a later parent repaint landed
# about 0.6s after Stop. Keep re-sending through that whole window.
REASSERT_SECS = 1.5
REASSERT_TICK = 0.02
BUSY_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
FILE_MODE = 0o600
DIR_MODE = 0o700
LOG_LIMIT = 200_000
LOG_KEEP = 100_000
CHOOSER_FOOTER = ("esc to close", "to navigate", "to select")


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    path = os.path.join(base, "herdr-kiro")
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass
    return path


def log_path() -> str:
    override = os.environ.get("HERDR_KIRO_LOG")
    if override:
        return override
    return os.path.join(state_dir(), "hook.log")


def state_path(pane_id: str) -> str:
    return os.path.join(state_dir(), f"{pane_id.replace(':', '_')}.json")


def seq_floor_path(path: str) -> str:
    return path + ".seq"


def log(msg: str) -> None:
    path = log_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size > LOG_LIMIT:
                handle.seek(max(0, size - LOG_KEEP))
                tail = handle.read()
                cut = tail.find("\n")
                if cut != -1:
                    tail = tail[cut + 1 :]
                handle.seek(0)
                handle.truncate()
                handle.write(tail)
            handle.write(f"{time.time():.3f} pid={os.getpid()} {msg}\n")
            handle.flush()
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


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


def members_of(busy: dict, key: str) -> list:
    prefix = key + "#"
    return [item for item in busy if item == key or item.startswith(prefix)]


def apply_event(state: dict, event: str, session: str, now: float) -> dict:
    """Fold one hook event into pane state. Pure aside from the clock passed in."""
    nxt = normalize(state, now)
    if event in BUSY_EVENTS:
        if event == "SessionStart" and session in nxt["busy"]:
            # Two live sessions collapsed onto one key. Keep both until both stop.
            index = 2
            while f"{session}#{index}" in nxt["busy"]:
                index += 1
            nxt["busy"][f"{session}#{index}"] = now
        elif event == "SessionStart" or not members_of(nxt["busy"], session):
            nxt["busy"][session] = now
        else:
            for member in members_of(nxt["busy"], session):
                nxt["busy"][member] = now
        nxt["grace_until"] = 0.0
    elif event == "Stop":
        if session in nxt["busy"]:
            nxt["busy"].pop(session, None)
        else:
            members = members_of(nxt["busy"], session)
            if members:
                oldest = min(members, key=lambda item: (nxt["busy"][item], item))
                nxt["busy"].pop(oldest, None)
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
    """True when the bottom of the pane is the Kiro tool chooser.

    The chooser footer is one line: esc to close, navigate, and select.
    The same words inside a model reply are not that footer.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[-8:]:
        lowered = line.lower()
        if all(part in lowered for part in CHOOSER_FOOTER):
            return True
    return False


def read_seq_floor(path: str) -> int:
    try:
        with open(seq_floor_path(path), encoding="utf-8") as handle:
            return max(0, int(handle.read().strip()))
    except (OSError, ValueError):
        return 0


def write_seq_floor(path: str, seq: int) -> None:
    current = read_seq_floor(path)
    if seq <= current:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    fd = os.open(seq_floor_path(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(seq))


def update(path: str, fn):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        try:
            loaded = json.loads(raw) if raw.strip() else fresh_state()
        except ValueError:
            loaded = fresh_state()
        if not isinstance(loaded, dict):
            loaded = fresh_state()
        now = time.time()
        state = normalize(loaded, now)
        floor = read_seq_floor(path)
        if state["seq"] < floor:
            state["seq"] = floor
        fn(state)
        state = normalize(state, time.time())
        if state["seq"] < floor:
            state["seq"] = floor
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state))
        handle.flush()
        write_seq_floor(path, int(state["seq"]))
        os.chmod(path, FILE_MODE)
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


def reassert_working(pane: str, path: str) -> None:
    """Cover the footer repaint. Herdr drops working about 0.18s after Stop."""
    deadline = time.time() + REASSERT_SECS
    while True:
        if not send_report(pane, path, True):
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(REASSERT_TICK, remaining))


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


def clear_watcher(path: str) -> None:
    def clear(state: dict) -> None:
        if int(state.get("watcher") or 0) == os.getpid():
            state["watcher"] = 0

    update(path, clear)


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
            now = time.time()
            until = float(state.get("grace_until") or 0)
            # Fast ticks across the footer repaint, and again at the end of grace
            # so the idle report is not a 0.15s tick late. A 0.4s poll then still
            # lands inside 3.5s of Stop.
            drop_window = (not state["busy"]) and (
                started <= now < started + 1.5 or until - 0.4 <= now <= until
            )
            delay = 0.05 if drop_window else TICK_SECS
            ok = send_report(pane, path, True)
        else:
            if not sent_idle:
                ok = send_report(pane, path, False)
                sent_idle = True
                log("watcher idle")
            else:
                ok = True

            def finish(current: dict) -> None:
                if current["busy"] or time.time() < float(current.get("grace_until") or 0):
                    return
                if int(current.get("watcher") or 0) == os.getpid():
                    current["watcher"] = 0

            state = update(path, finish)
            if int(state.get("watcher") or 0) != os.getpid():
                log("watcher exit")
                return
            delay = TICK_SECS
        if ok:
            misses = 0
        else:
            misses += 1
            if misses >= 30:
                log("watcher stopping after herdr failures")
                clear_watcher(path)
                return


def session_of(payload: dict, ppid: int | None = None) -> str:
    """Stable id for one Kiro session.

    V3's hook builder always sends session_id (plus cwd and hook_event_name).
    It does not send a transcript path. A Stop with those two fields removed is
    identical for every parallel step, so hashing the remaining body merges them.
    A transcript path is used when a caller has one. Otherwise the hook parent
    pid separates step processes, and a second SessionStart on that same key
    keeps its own slot until its own Stop.
    """
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("transcript_path", "transcriptPath"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return "transcript:" + value
    parent = os.getppid() if ppid is None else ppid
    return f"ppid:{parent}"


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
    action = next_action(len(state["busy"]), float(state["grace_until"]), False, time.time())
    if action == "working":
        if event == "Stop" and not state["busy"]:
            reassert_working(pane, path)
        else:
            send_report(pane, path, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
