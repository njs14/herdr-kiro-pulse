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
from __future__ import annotations

import fcntl
import json
import datetime
import os
import re
import signal
import subprocess
import sys
import time

SOURCE = "hook:kiro"
AGENT = "kiro"
SETTLE_SECS = 3.0
STALE_SECS = 1800.0
TICK_SECS = 0.2
BUSY_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
# No session id shares one slot. A per-event pid never matches its own Stop.
AGGREGATE = "aggregate"
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
        "watcher_started": "",
        "grace_started": 0.0,
        "owners": {},
        "owner_started": {},
        "aggregate": 0,
    }


def normalize(state: dict, now: float) -> dict:
    busy = state.get("busy") if isinstance(state.get("busy"), dict) else {}
    owners = state.get("owners") if isinstance(state.get("owners"), dict) else {}
    owner_started = state.get("owner_started") if isinstance(state.get("owner_started"), dict) else {}
    kept = {}
    kept_owners = {}
    kept_started = {}
    for session, stamp in busy.items():
        try:
            seen = float(stamp)
        except (TypeError, ValueError):
            continue
        key = str(session)
        if owner_is_dead(owners.get(key), owner_started.get(key)):
            continue
        if now - seen < STALE_SECS:
            kept[key] = seen
            if owners.get(key):
                kept_owners[key] = owners.get(key)
                if owner_started.get(key):
                    kept_started[key] = owner_started.get(key)
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
    try:
        aggregate = int(state.get("aggregate") or 0)
    except (TypeError, ValueError):
        aggregate = 0
    if AGGREGATE not in kept:
        aggregate = 0
    return {
        "busy": kept,
        "seq": seq,
        "grace_until": grace_until,
        "reported": state.get("reported"),
        "watcher": watcher,
        "watcher_at": watcher_at,
        "watcher_started": state.get("watcher_started") or "",
        "grace_started": grace_started,
        "owners": kept_owners,
        "owner_started": kept_started,
        "aggregate": aggregate,
    }


def members_of(busy: dict, key: str) -> list:
    prefix = key + "#"
    return [item for item in busy if item == key or item.startswith(prefix)]


def remember_owner(state: dict, key: str, owner: int, started: str) -> None:
    if not owner:
        return
    state["owners"][key] = int(owner)
    if started:
        state["owner_started"][key] = started


def finish_if_idle(state: dict, now: float) -> None:
    if state["busy"]:
        return
    state["grace_until"] = now + SETTLE_SECS
    state["grace_started"] = now


def apply_event(state: dict, event: str, session: str, now: float, owner: int = 0, owner_started: str = "") -> dict:
    """Fold one hook event into pane state. Pure aside from the clock passed in."""
    nxt = normalize(state, now)
    if session == AGGREGATE:
        count = int(nxt.get("aggregate") or 0)
        if event == "SessionStart":
            count += 1
            nxt["aggregate"] = count
            nxt["busy"][AGGREGATE] = now
            remember_owner(nxt, AGGREGATE, owner, owner_started)
            nxt["grace_until"] = 0.0
        elif event in BUSY_EVENTS:
            nxt["busy"][AGGREGATE] = now
            remember_owner(nxt, AGGREGATE, owner, owner_started)
            nxt["grace_until"] = 0.0
        elif event == "Stop":
            if count > 0:
                count -= 1
            nxt["aggregate"] = count
            if count == 0:
                nxt["busy"].pop(AGGREGATE, None)
                nxt["owners"].pop(AGGREGATE, None)
                nxt["owner_started"].pop(AGGREGATE, None)
                finish_if_idle(nxt, now)
            else:
                nxt["busy"][AGGREGATE] = now
                nxt["grace_until"] = 0.0
        return nxt
    if event in BUSY_EVENTS:
        if event == "SessionStart" and session in nxt["busy"]:
            # Two live sessions collapsed onto one key. Keep both until both stop.
            index = 2
            while f"{session}#{index}" in nxt["busy"]:
                index += 1
            slot = f"{session}#{index}"
            nxt["busy"][slot] = now
            remember_owner(nxt, slot, owner, owner_started)
        elif event == "SessionStart" or not members_of(nxt["busy"], session):
            nxt["busy"][session] = now
            remember_owner(nxt, session, owner, owner_started)
        else:
            for member in members_of(nxt["busy"], session):
                nxt["busy"][member] = now
        nxt["grace_until"] = 0.0
    elif event == "Stop":
        if session in nxt["busy"]:
            nxt["busy"].pop(session, None)
            nxt["owners"].pop(session, None)
            nxt["owner_started"].pop(session, None)
        else:
            members = members_of(nxt["busy"], session)
            if members:
                oldest = min(members, key=lambda item: (nxt["busy"][item], item))
                nxt["busy"].pop(oldest, None)
                nxt["owners"].pop(oldest, None)
                nxt["owner_started"].pop(oldest, None)
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
        elif floor == 0 and state["seq"] == 0:
            # A deleted floor must not restart at seq 1. herdr ignores that.
            state["seq"] = int(time.time())
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


def pane_text(pane: str) -> str:
    try:
        return subprocess.check_output(
            ["herdr", "pane", "read", pane, "--source", "visible", "--lines", "80"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


BANNER_LINE = re.compile(r"(?i)^\s*▶\s+\S+\s+running\b.*ctrl\+x\b.*ctrl\+g\b")


def workflow_banner_from(text: str) -> bool:
    """The Kiro status line, not a mention of a running step in model text."""
    return any(BANNER_LINE.search(line) for line in text.splitlines())


def workflow_banner(pane: str) -> bool:
    return workflow_banner_from(pane_text(pane))


def ps_column(pid: int, column: str):
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", column + "="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"error": True}
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip()


def comm_basename(text: str) -> str:
    """macOS ps prints the full path. A path with spaces must stay one field."""
    return os.path.basename((text or "").strip())


def start_token(text: str) -> str:
    """Epoch seconds for a ps lstart value. Day 1-9 may be padded with a space."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    try:
        parsed = datetime.datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return cleaned
    return str(int(parsed.timestamp()))


def process_snapshot(pid: int) -> dict:
    """Return ppid, lstart, and comm basename. None if the pid is gone."""
    if pid <= 0:
        return None
    ppid_text = ps_column(pid, "ppid")
    if isinstance(ppid_text, dict):
        return ppid_text
    if ppid_text is None:
        return None
    lstart = ps_column(pid, "lstart")
    comm = ps_column(pid, "comm")
    if isinstance(lstart, dict) or isinstance(comm, dict):
        return {"error": True}
    try:
        ppid = int(str(ppid_text).strip())
    except ValueError:
        return {"error": True}
    return {
        "ppid": ppid,
        "lstart": start_token(lstart or ""),
        "comm": comm_basename(comm or ""),
    }


def owner_is_dead(owner, started: str) -> bool:
    if not owner:
        return False
    try:
        pid = int(owner)
    except (TypeError, ValueError):
        return False
    snap = process_snapshot(pid)
    if snap is None:
        return True
    if snap.get("error"):
        return False
    if started and snap.get("lstart") != started:
        return True
    return False


def alive(pid: int, started: str = "") -> bool:
    if pid <= 0:
        return False
    snap = process_snapshot(pid)
    if snap is None:
        return False
    if snap.get("error"):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return not started
    if started and snap.get("lstart") != started:
        return False
    return True


def command_name(comm: str) -> str:
    name = os.path.basename(comm or "").lower()
    if name.startswith("-"):
        name = name[1:]
    return name


def kiro_ancestor() -> tuple:
    """Pid and start time of the kiro-cli process above this hook, if any."""
    pid = os.getppid()
    seen = set()
    while pid > 1 and pid not in seen and len(seen) < 15:
        seen.add(pid)
        snap = process_snapshot(pid)
        if not snap or snap.get("error"):
            break
        if "kiro" in command_name(snap.get("comm") or ""):
            return pid, snap.get("lstart") or ""
        pid = int(snap.get("ppid") or 0)
    return 0, ""


def process_chain() -> str:
    """Comms and pids above this process. Comms only, never arguments."""
    pid = os.getppid()
    seen = set()
    parts = []
    while pid > 1 and pid not in seen and len(parts) < 8:
        seen.add(pid)
        snap = process_snapshot(pid)
        if not snap or snap.get("error"):
            parts.append("%s:?" % pid)
            break
        parts.append("%s:%s" % (pid, command_name(snap.get("comm") or "")))
        pid = int(snap.get("ppid") or 0)
    return " ".join(parts)


def write_pidfile(state_file: str, pid: int, started: str) -> None:
    directory = os.path.join(os.path.dirname(state_file), "pids")
    os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(directory, DIR_MODE)
    except OSError:
        pass
    path = os.path.join(directory, str(pid))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getuid()}\n{started}\n")


def remove_pidfile(state_file: str, pid: int) -> None:
    path = os.path.join(os.path.dirname(state_file), "pids", str(pid))
    try:
        os.remove(path)
    except OSError:
        pass


def ensure_watcher(pane: str, path: str) -> None:
    decision = {"spawn": False}

    def claim(state: dict) -> None:
        now = time.time()
        pid = int(state.get("watcher") or 0)
        claimed = pid == -1 and now - float(state.get("watcher_at") or 0) < 2
        if claimed or alive(pid, state.get("watcher_started") or ""):
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
    started = ""
    snap = process_snapshot(proc.pid)
    if snap and not snap.get("error"):
        started = snap.get("lstart") or ""

    def record(state: dict) -> None:
        state["watcher"] = proc.pid
        state["watcher_started"] = started

    update(path, record)
    write_pidfile(path, proc.pid, started)
    log(f"watcher spawned pid={proc.pid}")


def clear_watcher(path: str) -> None:
    def clear(state: dict) -> None:
        if int(state.get("watcher") or 0) == os.getpid():
            state["watcher"] = 0
            state["watcher_started"] = ""

    update(path, clear)
    remove_pidfile(path, os.getpid())


def watch_loop(pane: str, path: str) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    snap = process_snapshot(os.getpid())
    started = "" if not snap or snap.get("error") else snap.get("lstart") or ""

    def claim(state: dict) -> None:
        state["watcher"] = os.getpid()
        state["watcher_started"] = started

    update(path, claim)
    write_pidfile(path, os.getpid(), started)
    log(f"watcher loop pane={pane}")
    reported = None
    misses = 0
    while True:
        time.sleep(TICK_SECS)
        state = update(path, lambda _state: None)
        screen = pane_text(pane)
        if approval_text(screen):
            continue
        banner = workflow_banner_from(screen)
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
        # Hook idle beats a visible working rule, and one dropped working
        # report stays idle. Re-send working while the banner is up.
        if banner:
            ok = send_report(pane, path, True)
            if ok:
                reported = "working"
                misses = 0
            else:
                misses += 1
                if misses >= 30:
                    clear_watcher(path)
                    return
            continue
        if action == reported:
            if action == "idle":
                clear_watcher(path)
                log("watcher exit")
                return
            continue
        ok = send_report(pane, path, action == "working")
        if action == "idle":
            log("watcher idle")
        if ok:
            reported = action
            misses = 0
        else:
            misses += 1
            if misses >= 30:
                log("watcher stopping after herdr failures")
                clear_watcher(path)
                return
        if action == "idle":
            clear_watcher(path)
            log("watcher exit")
            return


def session_of(payload: dict, ppid: int = 0) -> str:
    """Stable id for one Kiro session.

    V3 sends session_id. A stripped payload has no per-session field: each hook
    is a new process, so a parent pid never matches the next event. Those events
    share one aggregate slot. ppid is accepted and ignored.
    """
    del ppid
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("transcript_path", "transcriptPath"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return "transcript:" + value
    return AGGREGATE


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
    owner, owner_started = kiro_ancestor()
    log(f"event={event} session={session} pane={pane} chain={process_chain()}")

    def fold(state: dict) -> None:
        revised = apply_event(state, event, session, now, owner, owner_started)
        state.clear()
        state.update(revised)

    state = update(path, fold)
    log(
        f"state event={event} busy={len(state['busy'])} "
        f"grace_until={state['grace_until']:.3f}"
    )
    ensure_watcher(pane, path)
    screen = pane_text(pane)
    action = next_action(
        len(state["busy"]),
        float(state["grace_until"]),
        workflow_banner_from(screen),
        time.time(),
    )
    if action == "working":
        send_report(pane, path, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
