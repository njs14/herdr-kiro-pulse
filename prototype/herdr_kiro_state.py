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
# Herdr already shows idle within half a second of Stop once the banner is gone.
# A 3 second hook grace only delayed the idle report. Half a second still covers
# the gap before the next step's SessionStart.
SETTLE_SECS = 0.5
STALE_SECS = 1800.0
TICK_SECS = 0.2
# Ctrl-C and a missing Stop leave the idle prompt up with no banner. Clear only
# after the prompt has stayed that way, so one repaint does not end the slot.
IDLE_CLEAR_SECS = 2.0
# Releasing the hook the instant the prompt returns makes herdr report unknown
# for about 3 seconds. Report idle first and release only after this delay, so
# a run can settle idle before that hand-off.
RELEASE_DELAY = 2.0
TAIL_LINES = 30
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
    lines = [line.strip() for line in tail_text(text).splitlines() if line.strip()]
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


def send_report(pane: str, path: str, kind: str, release: bool = True) -> bool:
    if kind not in ("working", "idle", "blocked"):
        raise ValueError(kind)

    def bump(state: dict) -> None:
        state["seq"] += 1
        state["reported"] = kind
        if kind == "idle":
            state["grace_until"] = 0.0

    seq = str(update(path, bump)["seq"])
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
    if kind != "idle" or not release:
        return ok

    return release_authority(pane, path) and ok


def release_authority(pane: str, path: str) -> bool:
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


# Same pattern as workflow_banner_working in kiro-detection.toml. ctrl+g is not
# required: a narrow pane clips or wraps it off the status line.
def tail_text(text: str) -> str:
    """The bottom of the pane. Older banners in the scrollback are not a live run."""
    lines = text.splitlines()
    if len(lines) <= TAIL_LINES:
        return text
    return "\n".join(lines[-TAIL_LINES:])


BANNER_LINE = re.compile(r"(?i)^\s*▶\s+\S+\s+running\b.*\bctrl\+x\b")
IDLE_PROMPT = re.compile(r"(?i)ask a question or describe a task")
RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiro-detection.toml")
_RULES = None


def workflow_banner_from(text: str) -> bool:
    """The Kiro status line, not a mention of a running step in model text."""
    return any(BANNER_LINE.search(line) for line in tail_text(text).splitlines())


def _quoted_list(chunk: str, key: str) -> list:
    matched = re.search(r"(?m)^%s\s*=\s*\[(.*?)\]" % key, chunk, re.S)
    if not matched:
        return []
    found = []
    for double, single in re.findall(r'"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'', matched.group(1)):
        found.append(double or single)
    return found


def detection_rules() -> list:
    """Blocked and working rules from the toml herdr loads. Flat gates only."""
    global _RULES
    if _RULES is not None:
        return _RULES
    try:
        with open(RULES_PATH, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        _RULES = []
        return _RULES
    rules = []
    for chunk in re.split(r"(?m)^\[\[rules\]\]\s*$", raw)[1:]:
        chunk = re.split(r"(?m)^\[\[", chunk)[0]
        if re.search(r"(?m)^(any|all|not)\s*=", chunk):
            continue
        identity = re.search(r'(?m)^id\s*=\s*"([^"]+)"', chunk)
        state = re.search(r'(?m)^state\s*=\s*"([^"]+)"', chunk)
        priority = re.search(r"(?m)^priority\s*=\s*(\d+)", chunk)
        region = re.search(r'(?m)^region\s*=\s*"([^"]+)"', chunk)
        if not (identity and state and priority):
            continue
        rules.append(
            {
                "id": identity.group(1),
                "state": state.group(1),
                "priority": int(priority.group(1)),
                "region": region.group(1) if region else "whole_recent",
                "contains": _quoted_list(chunk, "contains"),
                "line_regex": _quoted_list(chunk, "line_regex"),
                "regex": _quoted_list(chunk, "regex"),
            }
        )
    _RULES = rules
    return _RULES


def region_text(text: str, spec: str) -> str:
    matched = re.match(r"bottom_non_empty_lines\((\d+)\)", spec or "")
    if not matched:
        return text
    count = int(matched.group(1))
    lines = text.splitlines()
    indexes = [index for index, line in enumerate(lines) if line.strip()]
    if not indexes:
        return ""
    start = indexes[-count] if len(indexes) >= count else indexes[0]
    return "\n".join(lines[start:])


def rule_matches(rule: dict, text: str) -> bool:
    if not (rule["contains"] or rule["line_regex"] or rule["regex"]):
        return False
    region = region_text(text, rule["region"])
    lowered = region.lower()
    if rule["contains"] and not all(needle.lower() in lowered for needle in rule["contains"]):
        return False
    lines = region.splitlines() or [region]
    for pattern in rule["line_regex"]:
        try:
            compiled = re.compile(pattern)
        except re.error:
            return False
        if not any(compiled.search(line) for line in lines):
            return False
    for pattern in rule["regex"]:
        try:
            compiled = re.compile(pattern)
        except re.error:
            return False
        if compiled.search(region) is None:
            return False
    return True


def highest_rule(text: str):
    best = None
    text = tail_text(text)
    for rule in detection_rules():
        if not rule_matches(rule, text):
            continue
        if best is None or rule["priority"] > best["priority"]:
            best = rule
    return best


def blocker_visible(text: str) -> bool:
    """True when the winning detection rule is blocked, or the chooser footer is up.

    The rule text comes from kiro-detection.toml, which is the file herdr evaluates.
    A workflow banner is a lower priority, so it loses while a blocker is visible.
    """
    if approval_text(text):
        return True
    winner = highest_rule(text)
    return bool(winner and winner["state"] == "blocked")


def screen_is_settled(text: str) -> bool:
    """No workflow banner, no approval, and no working footer.

    The kiro idle prompt qualifies. So does the shell prompt left behind after
    the outer kiro-cli exits, which does not contain that prompt.
    """
    if workflow_banner_from(text) or blocker_visible(text) or approval_text(text):
        return False
    return "kiro is working" not in tail_text(text).lower()


def screen_is_quiet(text: str) -> bool:
    """The kiro idle prompt, with nothing else running."""
    if not IDLE_PROMPT.search(tail_text(text) or ""):
        return False
    return screen_is_settled(text)


def report_for(screen: str, action: str):
    """What this event should tell herdr. None means send nothing.

    A visible blocker is blocked. The idle prompt gets no working report, so a
    late tool event cannot cover it. A banner or a live turn still reports working.
    """
    if blocker_visible(screen):
        return "blocked"
    if screen_is_quiet(screen):
        return None
    if action == "working":
        return "working"
    return None


def release_stuck(state: dict) -> None:
    """End every slot. Used when the screen has been idle, or the owner has died."""
    state["busy"] = {}
    state["owners"] = {}
    state["owner_started"] = {}
    state["aggregate"] = 0
    state["grace_until"] = 0.0
    state["grace_started"] = 0.0


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
    """Pid and start time of the outer kiro-cli above this hook.

    The walk meets kiro-cli-chat first. That child stays alive after SIGTERM to
    the outer kiro-cli, so the owner has to be the outermost kiro-cli.
    """
    pid = os.getppid()
    seen = set()
    outer = (0, "")
    chat = (0, "")
    while pid > 1 and pid not in seen and len(seen) < 15:
        seen.add(pid)
        snap = process_snapshot(pid)
        if not snap or snap.get("error"):
            break
        name = command_name(snap.get("comm") or "")
        if name == "kiro-cli":
            outer = (pid, snap.get("lstart") or "")
        elif "kiro" in name and chat == (0, ""):
            chat = (pid, snap.get("lstart") or "")
        pid = int(snap.get("ppid") or 0)
    if outer[0]:
        return outer
    return chat


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
    quiet_since = None
    while True:
        time.sleep(TICK_SECS)
        state = update(path, lambda _state: None)
        screen = pane_text(pane)
        now = time.time()
        # A newer working report beats a visible blocker until the next screen
        # scan. Report blocked, and do not report working, while one is up.
        if blocker_visible(screen):
            quiet_since = None
            ok = send_report(pane, path, "blocked")
            if ok:
                reported = "blocked"
                misses = 0
            else:
                misses += 1
                if misses >= 30:
                    clear_watcher(path)
                    return
            continue
        banner = workflow_banner_from(screen)
        if banner:
            quiet_since = None

            def extend(current: dict) -> None:
                revised = note_banner(current, True, time.time())
                current["grace_until"] = revised["grace_until"]

            state = update(path, extend)
        elif screen_is_settled(screen):
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= IDLE_CLEAR_SECS and state["busy"]:
                state = update(path, release_stuck)
                log("screen idle clear")
        else:
            quiet_since = None
        action = next_action(
            len(state["busy"]),
            float(state["grace_until"]),
            banner,
            time.time(),
        )
        # Hook idle beats a visible working rule, and one dropped working
        # report stays idle. Re-send working while the banner is up.
        if banner:
            ok = send_report(pane, path, "working")
            if ok:
                reported = "working"
                misses = 0
            else:
                misses += 1
                if misses >= 30:
                    clear_watcher(path)
                    return
            continue
        if action == "idle" and not state["busy"] and not screen_is_quiet(screen):
            # The outer kiro-cli is gone and the shell prompt is back. A kiro
            # working footer is a live turn, so that one stays with screen detection.
            if "kiro is working" not in screen.lower():
                release_authority(pane, path)
                clear_watcher(path)
                log("watcher exit after owner")
                return
        if action == "idle" and screen_is_quiet(screen):
            if reported != "idle":
                ok = send_report(pane, path, "idle", release=False)
                if ok:
                    reported = "idle"
                    misses = 0
                    log("watcher idle")
                else:
                    misses += 1
                    if misses >= 30:
                        log("watcher stopping after herdr failures")
                        clear_watcher(path)
                        return
            held = quiet_since is not None and now - quiet_since >= RELEASE_DELAY
            if reported == "idle" and not state["busy"] and held:
                release_authority(pane, path)
                clear_watcher(path)
                log("watcher exit")
                return
            continue
        if action == reported:
            continue
        if action != "working":
            continue
        ok = send_report(pane, path, "working")
        if ok:
            reported = "working"
            misses = 0
        else:
            misses += 1
            if misses >= 30:
                log("watcher stopping after herdr failures")
                clear_watcher(path)
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
    kind = report_for(screen, action)
    if kind:
        send_report(pane, path, kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
