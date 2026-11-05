#!/usr/bin/env python3
"""Report a Kiro CLI V3 pane to the Herdr App, and still to Herdr TUI.

The App reads OSC 7501 from the pane tty. herdr-core sets HC_TERMINAL_ID on
that program. The hook writes state=working, state=blocked, or state=done
there. A finished turn stays state=done after the pane is seen. The Agents
sidebar keeps a focused row under Done until that row is left, and this
hook cannot set that hold. Herdr TUI is separate: HERDR_ENV and HERDR_PANE_ID
still drive `herdr pane report-agent`. A finished turn stays an idle report
on the TUI, which herdr 0.9.5 shows as done until the pane is seen. A TUI
pane that also has HC_TERMINAL_ID reads that pane and does not write OSC.

Workflow steps are separate Kiro sessions. The parent pane paints its idle prompt
while they run, so screen detection calls the pane idle. Each step session
inherits HERDR_PANE_ID and fires the same hooks.

Herdr also drops a custom hook's working authority when a foreground turn
repaints from the "Kiro is working" footer to the idle prompt, about 0.2s
after Stop, and it remembers the sequence number. This process sends one
working report. It does not send that report again for the rest of the grace.
The idle report stays in place while kiro-cli is running. A live pid whose
token is empty, ``sec:`` plus a whole-second lstart, a bare whole-second
lstart, or a fine token that no longer matches is not an exit and is not
painted. The owner is the outer process. A gone chat child is not an exit
and does not release the pane. release-agent runs only after that proven
outer process is gone.
"""

from __future__ import annotations

import atexit
import contextlib
import datetime
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import herdr_kiro_osc

__version__ = "0.1.0"
SOURCE = "hook:kiro"
AGENT = "kiro"
# Herdr already shows idle within half a second of Stop once the banner is gone.
# Half a second still covers the gap before the next step's SessionStart.
SETTLE_SECS = 0.5
STALE_SECS = 1800.0
TICK_SECS = 0.2
# A failed done is retried after a sleep. The cap stops the watcher without
# release-agent and without writing another tty.
EXIT_RETRY_CAP = 30
# Ctrl-C and a missing Stop leave the idle prompt up with no banner. Clear only
# after the prompt has stayed that way, so one repaint does not end the slot.
IDLE_CLEAR_SECS = 2.0
# One agent_not_found reading is not enough to end a live run.
GONE_CONFIRM_SECS = 1.5
GONE_CONFIRM_HITS = 3
IDLE_DELAY: float = 2.0
TAIL_LINES = 30
BUSY_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
# Stop fires after a turn. SessionEnd fires when that loaded session is torn down.
END_EVENTS = {"Stop", "SessionEnd"}
# No session id shares one slot. A per-event pid never matches its own Stop.
AGGREGATE = "aggregate"
FILE_MODE = 0o600
DIR_MODE = 0o700
LOG_LIMIT: int = 200_000
LOG_KEEP: int = 100_000
CHOOSER_FOOTER = ("esc to close", "to navigate", "to select")


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    path = os.path.join(base, "herdr-kiro")
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)
    return path


def log_path() -> str:
    override = os.environ.get("HERDR_KIRO_LOG")
    if override:
        return override
    return os.path.join(state_dir(), "hook.log")


_PANE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def pane_id_ok(pane_id: str) -> bool:
    """True for a short pane id that cannot be a path or a leading-dash option."""
    if not isinstance(pane_id, str) or not _PANE_ID.fullmatch(pane_id):
        return False
    return not (pane_id.startswith("-") or ".." in pane_id)


def inside_state_dir(path: str) -> bool:
    """True when realpath(path) is a file path inside the private state dir."""
    root = os.path.realpath(state_dir())
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    if resolved == root:
        return False
    return resolved.startswith(root + os.sep)


def resolved_state_file(pane_id: str):
    """State file for pane_id, or None when the id or the path is unsafe."""
    if not pane_id_ok(pane_id):
        return None
    path = os.path.join(state_dir(), "{}.json".format(pane_id.replace(":", "_")))
    if os.path.islink(path) or not inside_state_dir(path):
        return None
    return path


def state_path(pane_id: str) -> str:
    path = resolved_state_file(pane_id)
    if path is None:
        raise ValueError("pane id")
    return path


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
        "watcher": 0,
        "watcher_at": 0.0,
        "watcher_started": "",
        "owners": {},
        "owner_ident": {},
        "phase": "",
        "aggregate": 0,
        "generation": 0,
        "retiring": False,
        "owners_died": False,
        "chat_pid": 0,
        "chat_ident": "",
        "proven_pid": 0,
        "proven_ident": "",
    }


def normalize(state: dict, now: float) -> dict:
    raw_busy = state.get("busy")
    raw_owners = state.get("owners")
    raw_ident = state.get("owner_ident")
    busy = raw_busy if isinstance(raw_busy, dict) else {}
    owners = raw_owners if isinstance(raw_owners, dict) else {}
    owner_ident = raw_ident if isinstance(raw_ident, dict) else {}
    kept = {}
    kept_owners = {}
    kept_ident = {}
    for session, stamp in busy.items():
        try:
            seen = float(stamp)
        except (TypeError, ValueError):
            continue
        key = str(session)
        # A live owner stays, including a weak row and a stamp older than
        # STALE_SECS. A dead owner stays until the exit write. A session
        # with no owner pid drops once the stamp is that old.
        dead = owner_is_dead(owners.get(key))
        if owners.get(key) or dead or now - seen < STALE_SECS:
            kept[key] = seen
            if owners.get(key):
                kept_owners[key] = owners.get(key)
                raw_ident = owner_ident.get(key)
                if isinstance(raw_ident, str):
                    kept_ident[key] = raw_ident
    raw_chat = state.get("chat_ident")
    chat_ident = raw_chat if isinstance(raw_chat, str) else ""
    if not fine_start(chat_ident):
        chat_ident = ""
    raw_proven = state.get("proven_ident")
    proven_ident = raw_proven if isinstance(raw_proven, str) else ""
    try:
        proven_pid = int(state.get("proven_pid") or 0)
    except (TypeError, ValueError):
        proven_pid = 0
    if not fine_start(proven_ident) or proven_pid <= 1:
        proven_pid = 0
        proven_ident = ""
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
        aggregate = int(state.get("aggregate") or 0)
    except (TypeError, ValueError):
        aggregate = 0
    if AGGREGATE not in kept:
        aggregate = 0
    # The second normalize in update() sees an already-empty busy map, so a
    # death noticed on the first pass would otherwise disappear. Keep the
    # flag until a live owner is stored again.
    owners_died = bool(state.get("owners_died")) and not kept and not kept_owners
    return {
        "busy": kept,
        "seq": seq,
        "grace_until": grace_until,
        "watcher": watcher,
        "watcher_at": watcher_at,
        "watcher_started": state.get("watcher_started") or "",
        "owners": kept_owners,
        "owner_ident": kept_ident,
        "phase": state.get("phase") if state.get("phase") in OWNER_STATES else "",
        "aggregate": aggregate,
        "generation": int(state.get("generation") or 0),
        "retiring": bool(state.get("retiring")),
        "owners_died": owners_died,
        "chat_pid": int(state.get("chat_pid") or 0),
        "chat_ident": chat_ident,
        "proven_pid": proven_pid,
        "proven_ident": proven_ident,
    }


def note_generation(previous: dict, nxt: dict) -> dict:
    """Every hook event is a new generation, including a same-session prompt."""
    nxt["generation"] = int(previous.get("generation") or 0) + 1
    return nxt


def members_of(busy: dict, key: str) -> list:
    prefix = key + "#"
    return [item for item in busy if item == key or item.startswith(prefix)]


def remember_owner(state: dict, key: str, owner: int, started: str) -> None:
    if not owner:
        return
    state["owners"][key] = int(owner)
    if not started:
        return
    # owner_ident keeps hires_start only when that token is finer than one
    # second. sec: plus whole-second lstart, and a bare whole-second lstart,
    # match a pid reused in that same second, so neither is this owner. The
    # pid file is a separate store and is not this proof.
    idents = state.get("owner_ident")
    if not isinstance(idents, dict):
        idents = {}
        state["owner_ident"] = idents
    # A stored fine token stays. A later fractional start does not replace it.
    # A token that no longer matches the live process stays weak.
    current = idents.get(key) or ""
    if isinstance(current, str) and fine_start(current):
        return
    ident = hires_start(int(owner))
    if fine_start(ident):
        idents[key] = ident
        return
    idents.pop(key, None)


def _keep_proven_pane(state: dict, key: str) -> None:
    """Save a proven pane owner before END drops its row.

    The idle report runs after that drop. state=done still has to reach the
    tty this token proved. Stop and SessionEnd both drop the row. An empty
    token or a sec: token is not saved.
    """
    owners = state.get("owners")
    if not isinstance(owners, dict):
        return
    try:
        pid = int(owners.get(key) or 0)
    except (TypeError, ValueError):
        return
    idents = state.get("owner_ident")
    raw = idents.get(key) if isinstance(idents, dict) else ""
    token = raw if isinstance(raw, str) else ""
    if not identity_proven(pid, token):
        return
    state["proven_pid"] = pid
    state["proven_ident"] = token


def _pop_session(state: dict, key: str) -> None:
    _keep_proven_pane(state, key)
    state["busy"].pop(key, None)
    state["owners"].pop(key, None)
    state["owner_ident"].pop(key, None)


def _owner_is_live_weak(state: dict) -> bool:
    """The pane owner is alive and not identity_proven."""
    owner = _pane_owner(state)
    if owner is None:
        return False
    pid, token = owner
    return alive(pid) and not identity_proven(pid, token)


def _held_proves(token: str) -> bool:
    """The held fd was opened for this fine token while the owner was proven."""
    return bool(fine_start(token) and _pane_fd >= 0 and _held_ident == token)


def _proven_exit_owed(state: dict) -> bool:
    """A proven outer is already gone and still owes state=done.

    The proof is a phase stored while the pid was proven, or the fd held
    for that fine token. A weak row, including a fine token that never
    matched, does not owe that write.
    """
    owner = _pane_owner(state)
    if owner is None:
        return False
    pid, token = owner
    if alive(pid) or not fine_start(token):
        return False
    stored = state.get("phase") or ""
    if stored in ("weak", "exited"):
        return False
    if stored in ("proven", "idle_clear"):
        return True
    return _held_proves(token)


def finish_if_idle(state: dict, now: float) -> None:
    """Arm the 0.5s grace. Call this only when a SessionStart count hits zero."""
    if state["busy"]:
        return
    state["grace_until"] = now + SETTLE_SECS


def apply_event(
    state: dict,
    event: str,
    session: str,
    now: float,
    owner: int = 0,
    owner_started: str = "",
    chat_pid: int = 0,
) -> dict:
    """Fold one hook event into pane state. Pure aside from the clock passed in."""
    nxt = normalize(state, now)
    if chat_pid:
        nxt["chat_pid"] = int(chat_pid)
        token = hires_start(int(chat_pid))
        nxt["chat_ident"] = token if fine_start(token) else ""
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
        elif event in END_EVENTS:
            previous = count
            if count > 0:
                count -= 1
            nxt["aggregate"] = count
            # The grace follows only this count returning to zero. A Stop
            # whose count is already zero does not arm it. A live weak owner
            # keeps the row until that pid is gone.
            if previous > 0 and count == 0 and not _owner_is_live_weak(nxt):
                _pop_session(nxt, AGGREGATE)
                finish_if_idle(nxt, now)
            elif count == 0:
                if _owner_is_live_weak(nxt):
                    nxt["busy"][AGGREGATE] = now
            else:
                nxt["busy"][AGGREGATE] = now
                nxt["grace_until"] = 0.0
        return note_generation(state, nxt)
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
            # A later busy event records a fine token when none is stored yet.
            for member in members_of(nxt["busy"], session):
                nxt["busy"][member] = now
                remember_owner(nxt, member, owner, owner_started)
        nxt["grace_until"] = 0.0
    elif event in END_EVENTS:
        # A named Stop or SessionEnd does not arm the grace. A live weak
        # owner keeps its row until that pid is gone.
        if not _owner_is_live_weak(nxt):
            if session in nxt["busy"]:
                _pop_session(nxt, session)
            else:
                members = members_of(nxt["busy"], session)
                if members:
                    oldest = min(members, key=lambda item: (nxt["busy"][item], item))
                    _pop_session(nxt, oldest)
    return note_generation(state, nxt)


def note_banner(state: dict, now: float) -> dict:
    """A visible workflow banner means the run is still going.

    next_action treats that banner as working. The banner does not start
    the settle grace. That grace follows only a SessionStart count
    returning to zero.
    """
    return normalize(state, now)


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
        died = bool(state.get("owners_died")) and not state.get("busy")
        state = normalize(state, time.time())
        if died and not state["busy"]:
            state["owners_died"] = True
        if state["seq"] < floor:
            state["seq"] = floor
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state))
        handle.flush()
        write_seq_floor(path, int(state["seq"]))
        os.chmod(path, FILE_MODE)
        return state


def in_tui() -> bool:
    """True when this process was started inside a Herdr TUI pane."""
    pane = os.environ.get("HERDR_PANE_ID") or ""
    return os.environ.get("HERDR_ENV") == "1" and pane_id_ok(pane)


def in_app() -> bool:
    """True when this process was started inside a herdr-core terminal."""
    return pane_id_ok(os.environ.get("HC_TERMINAL_ID") or "")


def herdr_bin() -> str:
    """The herdr that launched this plugin, or `herdr` on PATH."""
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr(*args: str) -> bool:
    log("herdr " + " ".join(args))
    try:
        completed = subprocess.run(
            [herdr_bin(), "pane", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# Fd this process opened for the owner's stdout while that owner was alive.
# The watcher opens it in this interpreter. The exit writes this fd.
# The token is the identity that proved the owner when the fd was opened.
_pane_fd = -1
_held_ident = ""
_HELD_FLAGS = (
    os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
    os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
)


def _open_held(open_path: str) -> int:
    for flag in _HELD_FLAGS:
        try:
            return os.open(open_path, flag)
        except OSError:
            continue
    return -1


def _hold_live_owner(pid: int, ident: str = "") -> None:
    """Dup the tty fd this owner still has open.

    An empty token does not identify the owner. sec: plus whole-second lstart
    and a bare whole-second lstart match a pid reused in that same second, so
    neither is proof and this function does not hold that tty. The hold uses
    identity_proven, not the pid file. The fd has to be the named tty. Any
    other character device, including a replaced fd 1, is closed.
    """
    global _pane_fd, _held_ident
    # An empty token is not proof. fine_start rejects sec: and a bare
    # whole-second lstart, so identity_proven does not hold that tty.
    if _pane_fd >= 0 or not identity_proven(pid, ident):
        return
    opened = list(herdr_kiro_osc._tty_opens(pid))
    if len(opened) != 1:
        return
    open_path, named = opened[0]
    device = str(named)
    if not herdr_kiro_osc._TTY.fullmatch(device):
        return
    try:
        expected = os.stat(device)
    except OSError:
        return
    if not stat.S_ISCHR(expected.st_mode):
        return
    fd = _open_held(str(open_path))
    if fd < 0:
        return
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return
    if not stat.S_ISCHR(info.st_mode) or info.st_rdev != expected.st_rdev:
        os.close(fd)
        return
    _pane_fd = fd
    _held_ident = ident


def _close_held_fd() -> None:
    global _pane_fd, _held_ident
    fd = _pane_fd
    _pane_fd = -1
    _held_ident = ""
    if fd >= 0:
        with contextlib.suppress(OSError):
            os.close(fd)


def identity_proven(pid: int, token: object) -> bool:
    """True when `token` is finer than this pid's whole-second start and names it.

    An empty token does not prove the pid. ``sec:`` plus lstart is that
    whole-second start, so it does not prove the pid either.
    """
    if not isinstance(token, str) or not fine_start(token):
        return False
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 1:
        return False
    if not alive(number):
        return False
    return hires_start(number) == token


def paint_status(kind: str, pid: int, token: str, *, held_fd: int = -1, rule_id: str = "") -> bool:
    """Write state=working or state=done for one proven owner.

    commit_pane is the path that decides to paint. This writes the bytes on
    that pane's one outer stdout tty, or on the fd opened for that stdout
    while the token proved it. A missing token, sec: plus whole-second
    lstart, a bare whole-second lstart, or a fine token that no longer
    matches is not eligible, including on that held fd.
    """
    if in_tui():
        return False
    fields = herdr_kiro_osc.fields_for(kind, rule_id)
    if fields is None or fields.get("state") not in ("working", "done"):
        return False
    # A held fd was opened only after identity_proven succeeded. The process
    # may already be gone, so the stored token is the proof.
    proven = fine_start(token) if held_fd >= 0 else identity_proven(pid, token)
    if not proven:
        return False
    try:
        payload = herdr_kiro_osc.encode(fields)
    except ValueError:
        log("refusing osc")
        return False
    if held_fd >= 0:
        try:
            herdr_kiro_osc._write_all(held_fd, payload)
        except OSError:
            log("osc missed done")
            return False
        log("osc done via held")
        return True
    device = herdr_kiro_osc.write_pid(payload, pid)
    if not device:
        return False
    log(f"osc {fields['state']} via {device}")
    return True


def _write_held_fd() -> bool:
    """Write idle's wire state=done through the fd opened while the owner lived."""
    fd = _pane_fd
    if fd < 0:
        return False
    return paint_status("idle", 0, _held_ident, held_fd=fd)


def _watcher_still_us(path: str) -> bool:
    """True when this pid is still the watcher recorded in the state file.

    Read the file directly. update() normalizes, and a dead owner stays until the exit write.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.loads(handle.read() or "{}")
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(loaded, dict):
        return False
    try:
        watcher = int(loaded.get("watcher") or 0)
    except (TypeError, ValueError):
        return False
    return watcher == os.getpid()


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.loads(handle.read() or "{}")
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(state, dict):
        return {}
    return state


def emit_osc(path: str, kind: str, rule_id: str = "") -> bool:
    """Write blocked on the pane's one outer tty.

    working and done are painted only by commit_pane. A chat pty, a second
    recorded pid, and this process's tty are not targets. Unknown is not a
    protocol state, so it is a no-op.
    """
    fields = herdr_kiro_osc.fields_for(kind, rule_id)
    if fields is None:
        return True
    if fields.get("state") != "blocked":
        return True
    if in_tui():
        return False
    owner = _pane_owner(_load_state(path))
    if owner is None:
        return False
    pid, token = owner
    if not identity_proven(pid, token):
        return False
    try:
        payload = herdr_kiro_osc.encode(fields)
    except ValueError:
        log("refusing osc")
        return False
    written = herdr_kiro_osc.write_pid(payload, pid)
    if written:
        log(f"osc {fields['state']} via {written}")
        return True
    log(f"osc missed {fields['state']}")
    return False


def send_report(pane: str, path: str, kind: str) -> bool:
    if not pane_id_ok(pane):
        log("refusing report")
        return False
    if kind not in ("working", "idle", "blocked"):
        raise ValueError(kind)
    # herdr 0.9.5 stores an unseen Working or Blocked -> Idle report as done.
    # agent explain still returns the screen rule. Leave this idle report in
    # place while kiro-cli is running. release-agent after it drops done:
    # an idle screen settles to idle with no completion, and a working
    # footer returns to working before that later idle screen also settles idle.
    tui_ok = emit_report(pane, path, kind)
    # A TUI pane can inherit HC_TERMINAL_ID. Return herdr's result before OSC.
    # working and done are painted only by commit_pane. Blocked, when it is
    # painted, goes to the one outer tty and not to a chat pty.
    if in_tui() or not in_app() or kind != "blocked":
        return tui_ok
    blocked_screen = pane_text(pane) or ""
    rule_id = str(screen_view(blocked_screen).get("id") or "")
    emit_osc(path, kind, rule_id)
    return tui_ok


def emit_report(pane: str, path: str, kind: str) -> bool:
    def bump(state: dict) -> None:
        state["seq"] += 1
        if kind == "idle":
            state["grace_until"] = 0.0

    seq = str(update(path, bump)["seq"])
    return herdr(
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


def release_authority(pane: str, path: str) -> bool:
    if not pane_id_ok(pane):
        log("refusing release")
        return False

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
    )


def _hc_bin() -> str:
    return os.environ.get("HC_BIN") or shutil.which("hc") or shutil.which("herdr-core") or ""


def _hc_screen() -> str | None:
    """The pane text from `hc read`, or None when core cannot be asked."""
    terminal = os.environ.get("HC_TERMINAL_ID") or ""
    binary = _hc_bin()
    if not pane_id_ok(terminal) or not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "--json", "read", terminal],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        return None
    try:
        data = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return None
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    return text


def pane_text(pane: str) -> str | None:
    """Visible text. None when an App read fails, which is not an empty screen.

    A successful empty `hc read` is "" and can still end the turn. TUI reads
    stay strings, including "" when the pane read fails. A TUI pane does not
    call hc read, even when HC_TERMINAL_ID is set.
    """
    if in_app() and not in_tui():
        return _hc_screen()
    try:
        return subprocess.check_output(
            [herdr_bin(), "pane", "read", pane, "--source", "visible", "--lines", "80"],
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
# The Kiro 3.0 chevron line from kiro-detection.toml (live_prompt_idle).
IDLE_PROMPT = re.compile(r"(?i)^\s*[>›]\s*ask a question or describe a task(?:\s+(?:enter|↵))?\s*$")
RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiro-detection.toml")
_EXPLAIN_ENV: dict | None = None
_RULES_DIR: str | None = None
_SCREEN_CACHE: dict = {}


def workflow_banner_from(text: str) -> bool:
    """The Kiro status line, not a mention of a running step in model text."""
    return any(BANNER_LINE.search(line) for line in tail_text(text).splitlines())


def temp_roots():
    """$TMPDIR on macOS, and /tmp when that is a different directory."""
    roots = [tempfile.gettempdir()]
    if os.path.abspath("/tmp") not in [os.path.abspath(root) for root in roots]:
        roots.append("/tmp")
    return roots


_OWNED_RULES = re.compile(r"^herdr-kiro-rules-(\d+)-")


def _rules_dir_ours(path: str):
    """Pid from a rules dir this uid created, or None when we must not touch it."""
    name = os.path.basename(path)
    matched = _OWNED_RULES.match(name)
    if not matched or not os.path.isdir(path):
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    if info.st_uid != os.getuid():
        return None
    marker = os.path.join(path, "herdr-kiro-owned")
    try:
        with open(marker, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    if (f"uid {os.getuid()}") not in lines:
        return None
    return int(matched.group(1))


def remove_tree(path: str) -> None:
    """Delete a directory tree without following a symlinked directory.

    Opens each directory with O_NOFOLLOW and unlinks names through that
    descriptor. A symlink is removed as a link. Errors are ignored.
    """
    parent = os.path.dirname(path) or "."
    name = os.path.basename(path)
    if not name or name in (".", ".."):
        return
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        _remove_dir_at(parent_fd, name)
    finally:
        os.close(parent_fd)


def _dir_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _remove_dir_at(parent_fd: int, name: str) -> None:
    try:
        fd = os.open(name, _dir_flags(), dir_fd=parent_fd)
    except OSError:
        return
    try:
        _clear_dir_fd(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=parent_fd)


def _clear_dir_fd(fd: int) -> None:
    try:
        names = os.listdir(fd)
    except OSError:
        return
    for name in names:
        if name in (".", ".."):
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            _remove_dir_at(fd, name)
            continue
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=fd)


def _sweep_rules_in(roots) -> None:
    """Remove rules dirs this package created after their process is gone.

    A directory is removed only when its name is herdr-kiro-rules-<pid>-<token>,
    it contains a herdr-kiro-owned marker for this uid, and the directory is
    owned by this uid. EPERM on the pid means the process is alive or foreign.
    """
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "herdr-kiro-rules-*")):
            pid = _rules_dir_ours(path)
            if pid is None or pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    continue
                remove_tree(path)


def sweep_stale_rules() -> None:
    _sweep_rules_in(temp_roots())


_LEGACY_JSON = re.compile(r"^herdr-kiro-.+\.json$")
_SIDECAR = ".herdr-kiro-owned"


def _private_regular(info) -> bool:
    """True when lstat shows a regular file this uid owns that others cannot write."""
    mode = info.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return False
    if info.st_uid != os.getuid():
        return False
    return not mode & (stat.S_IWGRP | stat.S_IWOTH)


def _read_owned_sidecar(marker_path: str):
    """Text of a sidecar this uid owns, or None when it must not be trusted.

    lstat of the sidecar must show a regular file owned by this uid that is
    not group- or world-writable. The protected file must be a regular file
    owned by this uid. The read uses O_NOFOLLOW so a symlink is not followed.
    """
    if not isinstance(marker_path, str) or not marker_path.endswith(_SIDECAR):
        return None
    protected = marker_path[: -len(_SIDECAR)]
    if not protected:
        return None
    try:
        marker_info = os.lstat(marker_path)
        protected_info = os.lstat(protected)
    except OSError:
        return None
    if not _private_regular(marker_info):
        return None
    if stat.S_ISLNK(protected_info.st_mode) or not stat.S_ISREG(protected_info.st_mode):
        return None
    if protected_info.st_uid != os.getuid():
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(marker_path, flags)
    except OSError:
        return None
    try:
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _legacy_file_ours(path: str) -> bool:
    """A herdr-kiro-*.json or hook log this uid marked.

    The sidecar must be a regular file owned by this uid, not group- or
    world-writable, and not a symlink. The file itself must be owned by
    this uid. No such sidecar, or one that does not say "uid <this uid>",
    is not ours.
    """
    name = os.path.basename(path)
    if name != "herdr-kiro-hook.log" and _LEGACY_JSON.fullmatch(name) is None:
        return False
    text = _read_owned_sidecar(path + _SIDECAR)
    if text is None:
        return False
    return (f"uid {os.getuid()}") in text.splitlines()


def sweep_owned_temp() -> None:
    """Drop our temp dirs and marked legacy files under TMPDIR and /tmp.

    Rules directories use the sweep's name, uid, and marker proof. User
    directories such as herdr-kiro-notes are left in place. An unmarked
    herdr-kiro-*.json or hook log is left in place.
    """
    roots = temp_roots()
    _sweep_rules_in(roots)
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if not _legacy_file_ours(path):
                continue
            try:
                os.remove(path)
            except OSError:
                continue
            marker = path + ".herdr-kiro-owned"
            if os.path.islink(marker):
                continue
            with contextlib.suppress(OSError):
                os.remove(marker)


def sweep_stale_pidfiles() -> None:
    """Drop a pid file when the process is gone, its start moved, or its comm basename differs."""
    directory = os.path.join(state_dir(), "pids")
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        if not name.isdigit():
            continue
        path = os.path.join(directory, name)
        pid = int(name)
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                continue
            with contextlib.suppress(OSError):
                os.remove(path)
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        recorded = lines[1].strip() if len(lines) > 1 else ""
        recorded_comm = lines[2].strip() if len(lines) > 2 else ""
        if not recorded:
            continue
        snap = process_snapshot(pid)
        if not snap or snap.get("error"):
            continue
        current = hires_start(pid)
        current_comm = snap.get("comm") or ""
        stale = bool(current) and current != recorded
        if recorded_comm and current_comm and recorded_comm != current_comm:
            stale = True
        if stale:
            with contextlib.suppress(OSError):
                os.remove(path)


def cleanup_rules() -> None:
    if _RULES_DIR and os.path.isdir(_RULES_DIR):
        remove_tree(_RULES_DIR)
    sweep_stale_rules()


def _on_term(signum, _frame):
    cleanup_rules()
    raise SystemExit(128 + signum)


def explain_env() -> dict:
    """Point herdr at the toml next to this script. The temp dir is removed on exit."""
    global _EXPLAIN_ENV
    if _EXPLAIN_ENV is not None:
        return _EXPLAIN_ENV
    global _RULES_DIR
    sweep_stale_rules()
    sweep_stale_pidfiles()
    base = tempfile.mkdtemp(prefix=f"herdr-kiro-rules-{os.getpid()}-")
    _RULES_DIR = base
    marker = os.path.join(base, "herdr-kiro-owned")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(f"uid {os.getuid()}\n")
    with contextlib.suppress(OSError):
        os.chmod(marker, FILE_MODE)
    atexit.register(remove_tree, base)
    dest = os.path.join(base, "herdr", "agent-detection")
    os.makedirs(dest, exist_ok=True)
    try:
        with open(RULES_PATH, "rb") as source, open(os.path.join(dest, "kiro.toml"), "wb") as target:
            target.write(source.read())
    except OSError:
        _EXPLAIN_ENV = dict(os.environ)
        return _EXPLAIN_ENV
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = base
    _EXPLAIN_ENV = env
    return env


def _unknown_screen() -> dict:
    return {"state": "unknown", "id": None, "ok": False}


def screen_view(text: str) -> dict:
    """Winning herdr rule for this screen. A failed explain is not cached."""
    cached = _SCREEN_CACHE.get(text)
    if cached is not None:
        return cached
    path = None
    view = _unknown_screen()
    try:
        fd, path = tempfile.mkstemp(prefix="herdr-kiro-screen-", suffix=".txt", dir=state_dir())
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text or "")
        completed = subprocess.run(
            [herdr_bin(), "agent", "explain", "--file", path, "--agent", "kiro", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            env=explain_env(),
            check=False,
        )
    except FileNotFoundError:
        return _unknown_screen()
    except (OSError, subprocess.SubprocessError):
        return _unknown_screen()
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.remove(path)
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        return _unknown_screen()
    try:
        data = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return _unknown_screen()
    rule = data.get("matched_rule") or {}
    view = {"state": data.get("state") or "idle", "id": rule.get("id"), "ok": True}
    if len(_SCREEN_CACHE) > 8:
        _SCREEN_CACHE.clear()
    _SCREEN_CACHE[text] = view
    return view


def blocker_visible(text: str) -> bool:
    """True when approval text is on screen, or herdr's winning rule is blocked.

    approval_text returns first. A chooser footer is blocked here when explain is not.
    """
    if approval_text(text):
        return True
    return screen_view(text).get("state") == "blocked"


def screen_is_settled(text: str) -> bool:
    """No workflow banner, no approval, and no working footer.

    The kiro idle prompt qualifies. So does the shell prompt left behind after
    the outer kiro-cli exits, which does not contain that prompt.
    """
    if workflow_banner_from(text) or blocker_visible(text) or approval_text(text):
        return False
    return "kiro is working" not in tail_text(text).lower()


def _idle_prompt_line(text: str) -> bool:
    """True when one tail line is the Kiro 3.0 chevron prompt."""
    return any(IDLE_PROMPT.search(line) for line in tail_text(text).splitlines())


def screen_is_quiet(text: str) -> bool:
    """The kiro idle prompt, with nothing else running."""
    if not _idle_prompt_line(text):
        return False
    return screen_is_settled(text)


def prompt_visible(text: str) -> bool:
    """The Kiro 3.0 chevron prompt.

    ``the price is $`` and a sentence that merely contains the words are
    model text. They are not the prompt.
    """
    return _idle_prompt_line(text)


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


def release_if_current(state: dict, generation: int, keys) -> bool:
    """Clear only the snapshotted slots when no newer hook event has landed."""
    if int(state.get("generation") or 0) != int(generation):
        state["retiring"] = False
        return False
    for key in keys:
        state["busy"].pop(key, None)
        state["owners"].pop(key, None)
    if AGGREGATE not in state["busy"]:
        state["aggregate"] = 0
    if not state["busy"]:
        state["grace_until"] = 0.0
    state["retiring"] = True
    return True


def slot_holder_gone(state: dict) -> bool:
    """True when some other pid holds the watcher slot and that pid is gone.

    EPERM means the holder is alive or foreign, so the slot is still taken.
    """
    try:
        holder = int(state.get("watcher") or 0)
    except (TypeError, ValueError):
        return False
    if holder in (os.getpid(), 0, -1):
        return False
    try:
        os.kill(holder, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def _ident_map(state: dict) -> dict:
    raw = state.get("owner_ident")
    return raw if isinstance(raw, dict) else {}


OWNER_STATES = ("no_owner", "proven", "weak", "exited", "idle_clear")
OWNER_EVENTS = ("start", "prompt", "stop", "idle_clear", "process_exit", "pid_reuse", "piped_chat")
_PANE_WRITES = {
    ("proven", "start"): "working",
    ("proven", "prompt"): "working",
    ("proven", "stop"): "done",
    ("proven", "idle_clear"): "done",
    ("proven", "process_exit"): "done",
    ("proven", "piped_chat"): "done",
    ("idle_clear", "stop"): "done",
    ("idle_clear", "idle_clear"): "done",
    ("idle_clear", "process_exit"): "done",
    ("idle_clear", "piped_chat"): "done",
}


def pane_transition(state: str, event: str) -> str:
    """The only choice of working, done, or none for one owner state and event."""
    if state not in OWNER_STATES:
        raise ValueError(state)
    if event not in OWNER_EVENTS:
        raise ValueError(event)
    return _PANE_WRITES.get((state, event), "none")


_PANE_NEXT = {
    ("proven", "stop"): "idle_clear",
    ("proven", "idle_clear"): "idle_clear",
    ("proven", "piped_chat"): "idle_clear",
    ("proven", "process_exit"): "exited",
    ("proven", "pid_reuse"): "weak",
    ("weak", "process_exit"): "exited",
    ("idle_clear", "process_exit"): "exited",
}


def pane_next(state: str, event: str) -> str:
    """The state after one event. The default is to stay."""
    if state not in OWNER_STATES:
        raise ValueError(state)
    if event not in OWNER_EVENTS:
        raise ValueError(event)
    return _PANE_NEXT.get((state, event), state)


def _chat_pid_of(state: dict) -> int:
    try:
        chat = int(state.get("chat_pid") or 0)
    except (TypeError, ValueError):
        return 0
    return chat if chat > 1 else 0


def _outer_pairs(state: dict) -> list[tuple[int, str]]:
    """One record for the pane: the outer pid, plus a saved proven pid.

    The chat child is not an owner and is not a second pane.
    """
    found: list[tuple[int, str]] = []
    idents = _ident_map(state)
    chat = _chat_pid_of(state)
    for _key, owner in (state.get("owners") or {}).items():
        try:
            pid = int(owner)
        except (TypeError, ValueError):
            continue
        if pid <= 1 or pid == chat:
            continue
        raw = idents.get(_key) or ""
        token = raw if isinstance(raw, str) else ""
        if any(known == pid for known, _token in found):
            continue
        found.append((pid, token))
    raw_pane = state.get("proven_ident") or ""
    pane_token = raw_pane if isinstance(raw_pane, str) else ""
    try:
        pane_pid = int(state.get("proven_pid") or 0)
    except (TypeError, ValueError):
        pane_pid = 0
    if pane_pid > 1 and pane_pid != chat and not any(known == pane_pid for known, _token in found):
        found.append((pane_pid, pane_token))
    return found


def _pane_owner(state: dict) -> tuple[int, str] | None:
    """The pane's one outer pid. A later recorded pid is not the owner."""
    pairs = _outer_pairs(state)
    if not pairs:
        return None
    return pairs[0]


def _pid_gone(pid: int) -> bool:
    """True only when kill reports ESRCH.

    EPERM means this process cannot see the pid. That pid is not gone.
    A live pid, including one whose token changed, is not gone.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 1:
        return False
    try:
        os.kill(number, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def _owner_exited(state: dict, remembered: list) -> bool:
    """The pane's outer process is gone.

    A second recorded pid does not answer this. A live pid, including one
    whose token changed, does not. A dropped row still counts when the last
    outer pid is gone.
    """
    owner = _pane_owner(state)
    if owner is not None:
        return _pid_gone(owner[0])
    if not remembered:
        return False
    try:
        pid = int(remembered[0])
    except (TypeError, ValueError, IndexError):
        return False
    return _pid_gone(pid)


def chat_is_piped(state: dict) -> bool:
    """True when the chat child's stdout is a pipe.

    A host without /proc cannot see that fd, so the ending stays idle_clear
    or stop.
    """
    pid = _chat_pid_of(state)
    if not pid:
        return False
    try:
        info = os.stat(f"/proc/{pid}/fd/1")
    except OSError:
        return False
    return stat.S_ISFIFO(info.st_mode)


def hook_event(state: dict, event: str) -> str:
    """The pane event for one hook callback."""
    if event in END_EVENTS:
        return "piped_chat" if chat_is_piped(state) else "stop"
    if event == "UserPromptSubmit":
        return "prompt"
    return "start"


def screen_event(state: dict, which: str) -> str:
    """The pane event for one watcher observation."""
    if which == "timer":
        return "piped_chat" if chat_is_piped(state) else "idle_clear"
    if which == "settled":
        return "piped_chat" if chat_is_piped(state) else "stop"
    if which == "banner":
        return "start"
    if which == "continue":
        return "prompt"
    if which == "reuse":
        return "pid_reuse"
    if which == "exit":
        return "process_exit"
    raise ValueError(which)


def owner_phase(state: dict) -> str:
    """The stored ending, or the outer pid's live class.

    idle_clear stays until process_exit. A chat child does not change it.
    A live outer that is not identity_proven is weak, including a fine token
    that no longer matches. A gone outer is exited.
    """
    stored = state.get("phase") or ""
    if stored == "idle_clear":
        return "idle_clear"
    if stored == "exited":
        return "exited"
    owner = _pane_owner(state)
    if owner is None:
        return "no_owner"
    pid, token = owner
    if alive(pid) and identity_proven(pid, token):
        return "proven"
    if alive(pid):
        return "weak"
    return "exited"


def _phase_for_event(state: dict, event: str) -> str:
    """The stored class for the one process_exit that still owes state=done.

    owner_phase reports exited once the outer pid is gone. A phase stored
    while that pid was proven, or the fd held for that fine token, makes
    the exit write done. A fine token that never matched is weak, and that
    exit writes none.
    """
    phase = owner_phase(state)
    if event != "process_exit" or phase == "idle_clear":
        return phase
    stored = state.get("phase") or ""
    if stored == "proven" or _proven_exit_owed(state):
        return "proven"
    if stored == "weak":
        return "weak"
    return phase


def _save_proven_owner(state: dict) -> None:
    owner = _pane_owner(state)
    if owner is None:
        return
    pid, token = owner
    stored = state.get("phase") or ""
    proven_now = identity_proven(pid, token)
    proven_exit = fine_start(token) and not alive(pid) and (stored == "proven" or _held_proves(token))
    if proven_now or proven_exit:
        state["proven_pid"] = pid
        state["proven_ident"] = token


def _paint_one_owner(state: dict, kind: str) -> bool:
    """Write working or done on the pane's one outer stdout tty.

    A TUI pane, including one inside the App, gets no OSC. A chat pty and a
    second recorded pid are not targets. An exit that already holds the
    stdout fd writes that fd only. A failed hold is a failed write.
    """
    if in_tui():
        return True
    if kind == "idle" and _pane_fd >= 0:
        return _write_held_fd()
    owner = _pane_owner(state)
    if owner is None:
        return False
    pid, token = owner
    return paint_status(kind, pid, token)


def commit_pane(state: dict, event: str) -> tuple[str, bool]:
    """Choose the pane write, paint that one tty, and store the next state.

    pane_transition chooses working, done, or none. This is the only path
    that paints working or done. A proven outer that is already gone is
    process_exit, so Stop, SessionEnd, and a prompt still write done on the
    tty it held. A failed done write does not change the phase. The caller
    drops the row after a done write has succeeded.
    """
    if event != "process_exit" and _proven_exit_owed(state):
        event = "process_exit"
    phase = _phase_for_event(state, event)
    write = pane_transition(phase, event)
    ok = True
    if write == "done":
        _save_proven_owner(state)
        ok = _paint_one_owner(state, "idle")
    elif write == "working":
        ok = _paint_one_owner(state, "working")
    if write == "done" and not ok:
        return write, ok
    state["phase"] = pane_next(phase, event)
    return write, ok


def gone_confirmed(since, now: float, hits: int) -> bool:
    if since is None:
        return False
    return hits >= GONE_CONFIRM_HITS and now - since >= GONE_CONFIRM_SECS


_TABLE = None
_PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+\s+\S+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.*?)\s*$")


def begin_tick() -> None:
    """Drop the process table so this tick pays for one ps, not one per pid."""
    global _TABLE
    _TABLE = None


def process_table():
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        completed = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,lstart=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        _TABLE = "error"
        return _TABLE
    if completed.returncode != 0:
        _TABLE = "error"
        return _TABLE
    rows = {}
    for line in (completed.stdout or "").splitlines():
        matched = _PS_LINE.match(line)
        if not matched:
            continue
        rows[int(matched.group(1))] = {
            "ppid": int(matched.group(2)),
            "lstart": start_token(matched.group(3)),
            "comm": comm_basename(matched.group(4)),
        }
    if (completed.stdout or "").strip() and not rows:
        _TABLE = "error"
        return _TABLE
    _TABLE = rows
    return _TABLE


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


def process_snapshot(pid: int) -> dict | None:
    """Return ppid, lstart, and comm basename. None if the pid is gone.

    One ps fills the table for the tick. Later lookups do not call ps again.
    """
    if pid <= 0:
        return None
    table = process_table()
    if table == "error":
        return {"error": True}
    return table.get(int(pid))


def _linux_start_ticks(pid: int):
    """Clock ticks since boot from the process stat, or None."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError):
        return None
    end = raw.rfind(b")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    # starttime is field 22. Fields after comm begin at field 3.
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _linux_boot_time():
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def hires_start(pid: int) -> str:
    """Start time from /proc ticks, or sec: plus whole-second lstart.

    Where /proc start ticks are present, the token is fractional seconds.
    Where those ticks are absent, including on Linux, the token is sec: plus
    the ps lstart. That form matches a pid reused in the same second.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    ticks = _linux_start_ticks(pid)
    boot = _linux_boot_time() if ticks is not None else None
    if ticks is not None and boot is not None:
        try:
            clk = float(os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, TypeError):
            clk = 0.0
        if clk > 0:
            return "%.6f" % (boot + (ticks / clk))
    snap = process_snapshot(pid)
    if not snap or snap.get("error"):
        return ""
    token = snap.get("lstart") or ""
    if not token:
        return ""
    return "sec:" + token


_FINE_START = re.compile(r"^\d+\.\d+$")


def fine_start(token: str) -> bool:
    """True when this token is stronger than a whole-second start.

    identity_proven is the owner check. This is the token shape it requires.
    sec: plus whole-second lstart is unproven. A bare whole-second lstart is
    unproven. Neither paints, and neither is an exit while the pid is live.
    """
    return isinstance(token, str) and _FINE_START.fullmatch(token) is not None


def watcher_identity(pid: int):
    """None when the pid is gone. error is set when ps cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    snap = process_snapshot(pid)
    if snap is None:
        return None
    if snap.get("error"):
        return {"start": "", "comm": "", "error": True}
    return {"start": hires_start(pid), "comm": snap.get("comm") or "", "error": False}


def read_owned_hash(marker_path: str):
    """sha256 from an owned sidecar, '' when the marker has no hash, else None.

    A symlink, a sidecar owned by another uid, a group- or world-writable
    sidecar, or a protected file owned by another uid is not owned.
    """
    text = _read_owned_sidecar(marker_path)
    if text is None:
        return None
    if not text.startswith("herdr-kiro-owned"):
        return None
    for line in text.splitlines():
        if line.startswith("sha256 "):
            return line.split(" ", 1)[1].strip()
    return ""


def file_sha256(path: str) -> str:
    hashed = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def ownership(dest: str) -> str:
    """How doctor and uninstall classify dest: match, differs, nohash, or absent.

    A symlink is absent. A file is ours only when its sidecar is a private
    regular file owned by this uid and the sidecar sha256 matches.
    """
    if os.path.islink(dest):
        return "absent"
    kind = read_owned_hash(dest + ".herdr-kiro-owned")
    if kind is None:
        return "absent"
    if kind == "":
        return "nohash"
    try:
        digest = file_sha256(dest)
    except OSError:
        return "differs"
    if digest == kind:
        return "match"
    return "differs"


def owner_is_dead(owner: object) -> bool:
    """True only when this pid is absent from the process table.

    A live pid stays when its token is empty, ``sec:`` plus whole-second
    lstart, a bare whole-second lstart, or a fine token that names a
    different start. A ps error is not death.
    """
    if not isinstance(owner, (int, str)) or not owner:
        return False
    try:
        pid = int(owner)
    except ValueError:
        return False
    return process_snapshot(pid) is None


def _process_matches(pid: int, started: str) -> bool:
    """True when this pid exists and its hires token equals `started`.

    This includes a ``sec:`` token. The watcher slot uses it. An owner does not.
    """
    if pid <= 0 or not isinstance(started, str) or not started:
        return False
    snap = process_snapshot(pid)
    if snap is None:
        return False
    if snap.get("error"):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return False
    return hires_start(pid) == started


def alive(pid: int) -> bool:
    """True when this pid is still running.

    An empty token, sec: plus whole-second lstart, a bare whole-second
    lstart, or a fine token that no longer matches does not turn a live pid
    into an exit. A ps error is not death. EPERM is not death.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 1:
        return False
    snap = process_snapshot(number)
    if snap is None:
        return False
    if snap.get("error"):
        try:
            os.kill(number, 0)
        except OSError as exc:
            return exc.errno != errno.ESRCH
        return True
    return True


def agent_lookup(pane: str) -> str:
    """herdr's view of the pane. 'gone' when the agent process is not there."""
    command = [herdr_bin()]
    session = os.environ.get("HERDR_SESSION")
    if session:
        command.extend(["--session", session])
    command.extend(["agent", "get", pane])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    raw = (completed.stdout or "") + (completed.stderr or "")
    if "agent_not_found" in raw:
        return "gone"
    if completed.returncode != 0:
        return "unknown"
    try:
        data = json.loads(completed.stdout or "{}")
    except ValueError:
        return "unknown"
    if (data.get("error") or {}).get("code") == "agent_not_found":
        return "gone"
    agent = (data.get("result") or {}).get("agent") or {}
    return agent.get("agent_status") or "unknown"


def command_name(comm: str) -> str:
    name = os.path.basename(comm or "").lower()
    if name.startswith("-"):
        name = name[1:]
    return name


def kiro_lineage() -> tuple:
    """Outer kiro-cli and the kiro-cli-chat under it. Hook shells are not owners."""
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
        elif name == "kiro-cli-chat" and chat == (0, ""):
            chat = (pid, snap.get("lstart") or "")
        pid = int(snap.get("ppid") or 0)
    return outer[0], outer[1], chat[0], chat[1]


def process_chain() -> str:
    """Comms and pids above this process. Comms only, never arguments."""
    pid = os.getppid()
    seen = set()
    parts = []
    while pid > 1 and pid not in seen and len(parts) < 8:
        seen.add(pid)
        snap = process_snapshot(pid)
        if not snap or snap.get("error"):
            parts.append(f"{pid}:?")
            break
        parts.append("{}:{}".format(pid, command_name(snap.get("comm") or "")))
        pid = int(snap.get("ppid") or 0)
    return " ".join(parts)


def write_pidfile(state_file: str, pid: int) -> None:
    directory = os.path.join(os.path.dirname(state_file), "pids")
    os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, DIR_MODE)
    # The pid file stores hires_start. A finer token does not match a pid reused
    # in that same second. Where /proc start ticks are absent, including on
    # Linux, the token is sec: plus whole-second lstart, and a same-second
    # reuse matches it. That unproven form is not a signal to send.
    token = hires_start(pid)
    snap = process_snapshot(pid)
    comm = ""
    if snap and not snap.get("error"):
        comm = snap.get("comm") or ""
    path = os.path.join(directory, str(pid))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getuid()}\n{token}\n{comm}\n")


def remove_pidfile(state_file: str, pid: int) -> None:
    path = os.path.join(os.path.dirname(state_file), "pids", str(pid))
    with contextlib.suppress(OSError):
        os.remove(path)


def ensure_watcher(pane: str, path: str) -> None:
    decision = {"spawn": False}

    def claim(state: dict) -> None:
        now = time.time()
        pid = int(state.get("watcher") or 0)
        claimed = pid == -1 and now - float(state.get("watcher_at") or 0) < 2
        retiring = bool(state.get("retiring"))
        if claimed and retiring:
            return
        if not retiring and (claimed or _process_matches(pid, state.get("watcher_started") or "")):
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
    watcher_token = hires_start(proc.pid)
    if not watcher_token:
        time.sleep(0.05)
        begin_tick()
        watcher_token = hires_start(proc.pid)

    def record(state: dict) -> None:
        state["watcher"] = proc.pid
        if watcher_token:
            state["watcher_started"] = watcher_token
        state["retiring"] = False

    update(path, record)
    write_pidfile(path, proc.pid)
    log(f"watcher spawned pid={proc.pid}")


def clear_watcher(path: str) -> None:
    def clear(state: dict) -> None:
        if int(state.get("watcher") or 0) == os.getpid():
            state["watcher"] = 0
            state["watcher_started"] = ""

    update(path, clear)
    remove_pidfile(path, os.getpid())


def commit_if_current(current: dict, event: str, generation: int) -> tuple[str, bool]:
    """Paint only when this generation is still the current one.

    A prompt that landed in the tick must not be overwritten with done.
    """
    if int(current.get("generation") or 0) != int(generation):
        return "none", False
    return commit_pane(current, event)


def clear_idle_if_current(current: dict, generation: int, keys: list) -> bool:
    """Write idle_clear only when this generation is still the current one.

    A newer prompt must not be painted done. A failed done leaves the row.
    """
    write, ok = commit_if_current(current, screen_event(current, "timer"), generation)
    if write == "done" and not ok:
        return False
    if current.get("phase") != "idle_clear":
        return False
    return release_if_current(current, generation, keys)


def apply_owner_exit(pane: str, path: str) -> bool:
    """Paint process_exit. Release only after a proven done write succeeds.

    A failed done leaves the row and does not change the phase. The watcher
    retries that write after a sleep. A weak exit writes none and does not
    call release-agent. The watcher stops after a weak exit, or after the
    retry cap, and that stop does not release.
    """
    decision = {"go": False, "stop": False}
    snap: dict = {}

    def apply(current: dict) -> None:
        if not _owner_exited(current, []):
            return
        write, ok = commit_pane(current, screen_event(current, "exit"))
        if write == "done" and not ok:
            return
        if write != "done":
            decision["stop"] = True
            return
        decision["go"] = True
        snap["generation"] = int(current.get("generation") or 0)
        snap["busy"] = dict(current.get("busy") or {})

    update(path, apply)
    if decision["stop"]:
        clear_watcher(path)
        log("watcher exit weak owner")
        return True
    if not decision["go"]:
        return False
    return finish_if_unchanged(pane, path, snap, "owner exit")


def finish_if_unchanged(pane: str, path: str, state: dict, reason: str) -> bool:
    """Stop the watcher only while this process still owns its slot.

    This does not write working or done. The caller paints before it asks.
    """
    me = os.getpid()
    generation = int(state.get("generation") or 0)
    keys = list(state.get("busy") or {})
    decision = {"ok": False}

    def apply(current: dict) -> None:
        watcher = int(current.get("watcher") or 0)
        if watcher not in (me, 0):
            return
        if int(current.get("generation") or 0) != generation:
            current["retiring"] = False
            return
        if reason == "idle" and current.get("busy"):
            current["retiring"] = False
            return
        decision["ok"] = release_if_current(current, generation, keys)

    update(path, apply)
    if not decision["ok"]:
        return False
    still = {"ok": False}

    def confirm(current: dict) -> None:
        watcher = int(current.get("watcher") or 0)
        if watcher not in (me, 0) or int(current.get("generation") or 0) != generation:
            current["retiring"] = False
            return
        if reason == "idle" and current.get("busy"):
            current["retiring"] = False
            return
        still["ok"] = True

    update(path, confirm)
    if not still["ok"]:
        return False
    # Idle while kiro is still running keeps the hook report. release-agent
    # after that idle drops done on herdr 0.9.5. Only a proven outer exit
    # reaches this with a reason other than idle.
    if reason != "idle":
        release_authority(pane, path)
    clear_watcher(path)
    log("watcher exit " + reason)
    return True


def note_report(pane: str, path: str, kind: str, reported, misses: int):
    ok = send_report(pane, path, kind)
    if ok:
        return kind, 0
    misses += 1
    if misses >= 30:
        clear_watcher(path)
        log("watcher stopping after herdr failures")
        return None, misses
    return reported, misses


class OwnerExitWatch:
    """Wake when an owner pid exits. kqueue on macOS, pidfd on Linux, kill -0 otherwise."""

    def __init__(self):
        self._fds = {}
        self._kq = None

    def close(self) -> None:
        for fd in self._fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds.clear()
        if self._kq is not None:
            with contextlib.suppress(OSError):
                self._kq.close()
            self._kq = None

    def wait(self, pids, timeout: float) -> bool:
        wanted = []
        for pid in pids:
            try:
                number = int(pid)
            except (TypeError, ValueError):
                continue
            if number > 1:
                wanted.append(number)
        if not wanted:
            time.sleep(timeout)
            return False
        if hasattr(select, "kqueue") and hasattr(select, "KQ_FILTER_PROC") and hasattr(select, "KQ_NOTE_EXIT"):
            return self._wait_kqueue(wanted, timeout)
        if hasattr(os, "pidfd_open"):
            try:
                return self._wait_pidfd(wanted, timeout)
            except OSError:
                time.sleep(max(0.0, timeout))
                return False
        return self._wait_kill(wanted, timeout)

    def _wait_pidfd(self, pids, timeout: float) -> bool:
        for pid in list(self._fds):
            if pid not in pids:
                try:
                    os.close(self._fds.pop(pid))
                except OSError:
                    self._fds.pop(pid, None)
        for pid in pids:
            if pid in self._fds:
                continue
            try:
                self._fds[pid] = os.pidfd_open(pid)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return True
                # EPERM is not a wake. Sleep the timeout instead of polling.
                time.sleep(max(0.0, timeout))
                return False
        poller = select.poll()
        for fd in self._fds.values():
            poller.register(fd, select.POLLIN)
        return bool(poller.poll(max(0, int(timeout * 1000))))

    def _wait_kqueue(self, pids, timeout: float) -> bool:
        # kqueue is macOS and BSD. Linux stdlib select does not define it.
        kqueue = getattr(select, "kqueue", None)
        kevent = getattr(select, "kevent", None)
        kq_filter = getattr(select, "KQ_FILTER_PROC", None)
        kq_add = getattr(select, "KQ_EV_ADD", None)
        kq_enable = getattr(select, "KQ_EV_ENABLE", None)
        kq_note = getattr(select, "KQ_NOTE_EXIT", None)
        if (
            kqueue is None
            or kevent is None
            or kq_filter is None
            or kq_add is None
            or kq_enable is None
            or kq_note is None
        ):
            return self._wait_kill(pids, timeout)
        if self._kq is None:
            self._kq = kqueue()
        changes = []
        for pid in pids:
            try:
                changes.append(kevent(pid, kq_filter, kq_add | kq_enable, kq_note))
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return True
                time.sleep(max(0.0, timeout))
                return False
        try:
            events = self._kq.control(changes, 1, timeout)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return True
            time.sleep(max(0.0, timeout))
            return False
        return bool(events)

    def _wait_kill(self, pids, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            for pid in pids:
                try:
                    os.kill(pid, 0)
                except OSError as exc:
                    if exc.errno == errno.ESRCH:
                        return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


def watch_loop(pane: str, path: str) -> None:
    try:
        _watch_loop(pane, path)
    except Exception as exc:
        log(f"watcher error {type(exc).__name__}: {exc}")
        clear_watcher(path)


def _watch_loop(pane: str, path: str) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _on_term)
    watcher_token = hires_start(os.getpid())

    def claim(state: dict) -> None:
        state["watcher"] = os.getpid()
        state["watcher_started"] = watcher_token

    update(path, claim)
    write_pidfile(path, os.getpid())
    log(f"watcher loop pane={pane}")
    reported = None
    misses = 0
    quiet_since = None
    held = None
    owner_watch = OwnerExitWatch()
    # SessionEnd drops the owner from the state file before the process exits.
    # Keep the last pid so that exit still wakes the watcher.
    known_owners = []
    known_outers = []
    gone_since = None
    gone_hits = 0
    exit_misses = 0
    try:
        while True:
            owner_watch.wait(known_owners, TICK_SECS)
            begin_tick()
            state = update(path, lambda _state: None)
            if slot_holder_gone(state):
                log("watcher exit slot holder gone")
                return
            looked = agent_lookup(pane)
            now = time.time()
            current_owners = []
            for owner in (state.get("owners") or {}).values():
                try:
                    current_owners.append(int(owner))
                except (TypeError, ValueError):
                    continue
            chat_pid = int(state.get("chat_pid") or 0)
            # A gone chat leaves the wait set. It is not an exit of the outer.
            if chat_pid and not _pid_gone(chat_pid):
                current_owners.append(chat_pid)
            outers_now = []
            for owner in (state.get("owners") or {}).values():
                try:
                    outers_now.append(int(owner))
                except (TypeError, ValueError):
                    continue
            if outers_now:
                known_outers = outers_now
            live_wait = [pid for pid in current_owners if not _pid_gone(pid)]
            if live_wait:
                known_owners = live_wait
            elif current_owners:
                known_owners = []
            pane_owner = _pane_owner(state)
            owner_alive = False
            # kill is the exit check. A process table can still name a reaped
            # pid, and that must not replace a stored proven phase with weak.
            if pane_owner is not None and not _pid_gone(pane_owner[0]) and alive(pane_owner[0]):
                owner_alive = True
                if identity_proven(pane_owner[0], pane_owner[1]):
                    _hold_live_owner(pane_owner[0], pane_owner[1])
                if state.get("phase") not in ("idle_clear", "exited"):
                    pid, token = pane_owner

                    def remember_phase(current: dict, pid: int = pid, token: str = token) -> None:
                        if current.get("phase") in ("idle_clear", "exited"):
                            return
                        if not alive(pid):
                            return
                        current["phase"] = "proven" if identity_proven(pid, token) else "weak"

                    state = update(path, remember_phase)
            elif chat_pid and alive(chat_pid):
                owner_alive = True
            if not known_owners and looked == "gone":
                if gone_since is None:
                    gone_since = now
                gone_hits += 1
                if gone_confirmed(gone_since, now, gone_hits):
                    # An empty owner table is not an outer exit. Do not release.
                    clear_watcher(path)
                    log("watcher exit agent gone")
                    return
            else:
                gone_since = None
                gone_hits = 0
            # A live weak owner stays in the table. The outer process is the
            # only exit, and that exit is process_exit.
            if _owner_exited(state, known_outers):
                if _pane_owner(state) is None:
                    clear_watcher(path)
                    log("watcher exit owner already gone")
                    return
                if apply_owner_exit(pane, path):
                    return
                if not _watcher_still_us(path):
                    log("watcher exit slot held")
                    return
                exit_misses += 1
                if exit_misses >= EXIT_RETRY_CAP:
                    clear_watcher(path)
                    log("watcher exit done missed")
                    return
                time.sleep(min(TICK_SECS * exit_misses, 2.0))
                continue
            if owner_phase(state) == "weak":
                state = update(path, lambda current: commit_pane(current, screen_event(current, "reuse")))
            screen = pane_text(pane)
            # None is a failed App read. Explain would call that an idle screen.
            app_read_failed = in_app() and screen is None
            if screen is None:
                screen = ""
                view = _unknown_screen()
            else:
                view = screen_view(screen)
            if not view.get("ok"):
                blocked = approval_text(screen) or held == "blocked"
                banner = workflow_banner_from(screen) or (held == "working" and not blocked)
            else:
                blocked = view.get("state") == "blocked" or approval_text(screen)
                banner = view.get("id") == "workflow_banner_working"
            if blocked:
                held = "blocked"
            elif banner:
                held = "working"
            if blocked:
                quiet_since = None
                if reported != "blocked":
                    reported, misses = note_report(pane, path, "blocked", reported, misses)
                    if reported is None and misses >= 30:
                        return
                continue
            if banner:
                quiet_since = None

                def extend(current: dict) -> None:
                    revised = note_banner(current, time.time())
                    current["grace_until"] = revised["grace_until"]

                state = update(path, extend)
            elif prompt_visible(screen) and screen_is_settled(screen):
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= IDLE_CLEAR_SECS and state["busy"]:
                    generation = int(state.get("generation") or 0)
                    keys = list(state["busy"])
                    decision = {"ok": False}

                    def apply_clear(
                        current: dict,
                        decision: dict = decision,
                        generation: int = generation,
                        keys: list = keys,
                    ) -> None:
                        decision["ok"] = clear_idle_if_current(current, generation, keys)

                    update(path, apply_clear)
                    if decision["ok"]:
                        log("screen idle clear")
                        state = update(path, lambda _current: None)
                    else:
                        quiet_since = None
            else:
                quiet_since = None
            action = next_action(
                len(state["busy"]),
                float(state["grace_until"]),
                banner,
                time.time(),
            )
            owners_gone = bool(known_owners) and not owner_alive
            if banner and not state.get("owners_died") and not owners_gone:
                chosen = {"write": "none"}

                def apply_banner(current: dict, chosen: dict = chosen) -> None:
                    write, _ok = commit_pane(current, screen_event(current, "banner"))
                    chosen["write"] = write

                state = update(path, apply_banner)
                if chosen["write"] == "working":
                    reported, misses = note_report(pane, path, "working", reported, misses)
                    if reported is None and misses >= 30:
                        return
                continue
            # The App has no herdr pane to explain. An empty read still ends the
            # turn once the grace has passed. A failed read is not that screen:
            # skip app_quiet and the idle report so a held working banner counts.
            # A TUI pane keeps the TUI quiet rule, even when HC_TERMINAL_ID is set.
            # A failed pane read is "" and must not count as that empty App screen.
            app_quiet = in_app() and not in_tui() and not app_read_failed and not screen.strip() and not banner
            if not app_read_failed and action == "idle" and (screen_is_quiet(screen) or app_quiet):
                if reported != "idle":
                    chosen = {"write": "none", "ok": False}
                    generation = int(state.get("generation") or 0)

                    def apply_settled(
                        current: dict,
                        chosen: dict = chosen,
                        generation: int = generation,
                    ) -> None:
                        write, ok = commit_if_current(
                            current,
                            screen_event(current, "settled"),
                            generation,
                        )
                        chosen["write"] = write
                        chosen["ok"] = ok

                    state = update(path, apply_settled)
                    if chosen["write"] == "done" and not chosen["ok"]:
                        # A failed done is not a finished idle. Leave the row.
                        exit_misses += 1
                        if exit_misses >= EXIT_RETRY_CAP:
                            clear_watcher(path)
                            log("watcher exit done missed")
                            return
                        time.sleep(min(TICK_SECS * exit_misses, 2.0))
                        continue
                    if chosen["write"] == "done":
                        exit_misses = 0
                        reported, misses = note_report(pane, path, "idle", reported, misses)
                        if reported is None and misses >= 30:
                            return
                        log("watcher idle")
                    else:
                        reported = "idle"
                held = quiet_since is not None and now - quiet_since >= IDLE_DELAY
                if reported == "idle" and not state["busy"] and held and finish_if_unchanged(pane, path, state, "idle"):
                    return
                continue
            if action == reported:
                continue
            if action != "working" or state.get("owners_died") or owners_gone:
                continue
            chosen = {"write": "none"}

            def apply_prompt(current: dict, chosen: dict = chosen) -> None:
                write, _ok = commit_pane(current, screen_event(current, "continue"))
                chosen["write"] = write

            state = update(path, apply_prompt)
            if chosen["write"] != "working":
                continue
            reported, misses = note_report(pane, path, "working", reported, misses)
            if reported is None and misses >= 30:
                return
    finally:
        owner_watch.close()
        _close_held_fd()


def session_of(payload: dict) -> str:
    """Stable id for one Kiro session.

    V3 sends session_id. A stripped payload has no per-session field: each hook
    is a new process, so a parent pid never matches the next event. Those events
    share one aggregate slot.
    """
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
        pane = sys.argv[2] if len(sys.argv) > 2 else ""
        given = sys.argv[3] if len(sys.argv) > 3 else ""
        expected = resolved_state_file(pane)
        if (
            expected is None
            or not given
            or os.path.islink(given)
            or os.path.realpath(given) != os.path.realpath(expected)
        ):
            log("refusing watch")
            return 1
        watch_loop(pane, expected)
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    pane = os.environ.get("HERDR_PANE_ID") or ""
    if not in_tui():
        pane = os.environ.get("HC_TERMINAL_ID") or ""
        if not pane_id_ok(pane):
            return 0
    try:
        path = state_path(pane)
    except ValueError:
        return 0
    now = time.time()
    session = session_of(payload)
    if event not in BUSY_EVENTS and event not in END_EVENTS:
        return 0
    begin_tick()
    owner, owner_started, chat_pid, _chat_lstart = kiro_lineage()
    log(f"event={event} session={session} pane={pane} chain={process_chain()}")

    chosen = {"write": "none", "ok": False, "release": False}

    def fold(state: dict) -> None:
        ev = hook_event(state, event)
        if event in END_EVENTS:
            write, ok = commit_pane(state, ev)
            chosen["write"] = write
            chosen["ok"] = ok
            if write == "done" and not ok:
                return
            owner_now = _pane_owner(state)
            if write == "done" and ok and owner_now is not None and not alive(owner_now[0]):
                chosen["release"] = True
        revised = apply_event(state, event, session, now, owner, owner_started, chat_pid)
        if event in END_EVENTS:
            revised["phase"] = state.get("phase") or revised.get("phase") or ""
            if state.get("proven_pid"):
                revised["proven_pid"] = state.get("proven_pid")
                revised["proven_ident"] = state.get("proven_ident") or ""
        else:
            write, ok = commit_pane(revised, ev)
            chosen["write"] = write
            chosen["ok"] = ok
            owner_now = _pane_owner(revised)
            if write == "done" and ok and owner_now is not None and not alive(owner_now[0]):
                chosen["release"] = True
                owners = revised.get("owners")
                if isinstance(owners, dict):
                    for key, pid in list(owners.items()):
                        try:
                            number = int(pid)
                        except (TypeError, ValueError):
                            continue
                        if number == owner_now[0]:
                            _pop_session(revised, key)
                            break
        state.clear()
        state.update(revised)

    state = update(path, fold)
    log(f"state event={event} busy={len(state['busy'])} grace_until={state['grace_until']:.3f}")
    ensure_watcher(pane, path)
    screen = pane_text(pane) or ""
    action = next_action(
        len(state["busy"]),
        float(state["grace_until"]),
        workflow_banner_from(screen),
        time.time(),
    )
    kind = report_for(screen, action)
    if chosen["write"] == "done" and chosen["ok"] and (event in END_EVENTS or chosen["release"]):
        send_report(pane, path, "idle")
    elif kind == "working" and chosen["write"] == "working":
        send_report(pane, path, "working")
    elif kind == "blocked":
        send_report(pane, path, kind)
    if chosen["release"]:
        release_authority(pane, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
