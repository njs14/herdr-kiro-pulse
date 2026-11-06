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

Workflow steps are separate Kiro sessions. A workflow banner wins over the
idle prompt, so screen detection does not call the pane idle while that
banner is up. Each step session inherits HERDR_PANE_ID and fires the same hooks.

Herdr also drops a custom hook's working authority when a foreground turn
repaints from the "Kiro is working" footer to the idle prompt, about 0.2s
after Stop, and it remembers the sequence number. This process sends one
working report. It does not send that report again for the rest of the grace.
The idle report stays in place while kiro-cli is running. A live pid whose
token is empty, ``sec:`` plus a whole-second lstart, a bare whole-second
lstart, or a fine token that no longer matches is not an exit and is not
painted. The owner is the outer process. A gone chat child is not an exit
and does not release the pane. release-agent runs only after that proven
outer process is gone. A screen read returns a result and does not touch
the filesystem, the tty, or OSC. A failed App read is none. It is not an
empty screen, a prompt, a banner, a start, or aggregate_zero, and it is
not refresh. It is not explained and it is not painted, including when a
banner or blocker was already held. An unconfirmed held pid writes
nothing, including a working paint and blocked, and does not open the
live stdout. reap unlinks a pid file only for a confirmed dead pid or a
confirmed token mismatch. A live pid whose token matches is not reaped
when only the comm name differs. A none result unlinks nothing.
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import dataclasses
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
# A pidfd or kqueue exit that is already visible must not spin the watcher.
# The sleep grows with consecutive wakes and never exceeds this cap.
POLL_BACKOFF_CAP = 2.0
POLL_SPIN_CAP = 30
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
        "chat_pid": 0,
        "chat_ident": "",
        "proven_pid": 0,
        "proven_ident": "",
    }


def note_generation(previous: dict, nxt: dict) -> dict:
    """Advance generation for a pane write other than refresh.

    Refresh changes only the busy timestamp, so it does not call this.
    A same-session prompt still advances generation when it is not refresh.
    """
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

    identity_proven is required. An empty token or a sec: token is not
    saved. Stop and SessionEnd drop a recorded owner only when
    pane_transition returns done. A slot with no owner pid still ends.
    A weak owner and a dead sec: owner stay. A none result leaves the row.
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


def _drop_done_owner(state: dict, pid: int) -> None:
    """Drop the owner a successful done write already ended.

    Call this only when pane_transition returned done and the paint
    succeeded. A none result does not call it, and it does not run from
    phase exited alone.
    """
    owners = state.get("owners")
    if not isinstance(owners, dict):
        return
    for key, owner in list(owners.items()):
        try:
            number = int(owner)
        except (TypeError, ValueError):
            continue
        if number == pid:
            _pop_session(state, key)
            return


def _owner_is_live_weak(state: dict) -> bool:
    """The pane owner is alive and not identity_proven.

    kill is the exit check. A process table can still name a reaped pid,
    and that pid is not a live weak owner.
    """
    owner = _pane_owner(state)
    if owner is None:
        return False
    pid, token = owner
    if _pid_gone(pid):
        return False
    return alive(pid) and not identity_proven(pid, token)


def _chooser_event(state: dict, event: str) -> str:
    """The event pane_transition sees. A proven exit is process_exit."""
    if event != "process_exit" and _proven_exit_owed(state):
        return "process_exit"
    return event


def _chooser_drops_row(state: dict, event: str) -> bool:
    """True when Stop or SessionEnd may drop a busy slot.

    A pane with no owner pid still ends the slot. A recorded owner is
    dropped only when pane_transition returns done. none leaves that row.
    A failed done paint returns before the row drop, so the row stays.
    A live owner is not dropped. A gone fine token stored as proven, or a
    held token that _held_proves, becomes process_exit and the table returns
    done, so that dead row can be dropped. A weak process_exit is none, so
    the phase stays weak.
    """
    if event not in END_EVENTS:
        return False
    if _pane_owner(state) is None:
        return True
    if _owner_is_live_weak(state):
        return False
    ev = _chooser_event(state, hook_event(state, event))
    return pane_transition(_phase_for_event(state, ev), ev) == "done"


def _held_proves(token: str, pid: int) -> bool:
    """True when the held fd was opened for this owner.

    The fd is open, the token is that fine token, and `_held_pid` is this
    pid. Zero and any other pid are not proof and do not write.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    return bool(fine_start(token) and number > 1 and _pane_fd >= 0 and _held_ident == token and _held_pid == number)


def _proven_exit_owed(state: dict) -> bool:
    """A proven outer is already gone and still owes state=done.

    The proof is a phase stored while the pid was proven, or the fd held
    for that fine token when `_held_pid` is this owner. A stored weak or
    exited phase does not owe that write. A gone fine token stored as
    proven or idle_clear does. A mismatched or unconfirmed held pid does not.
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
    return _held_proves(token, pid)


def finish_if_idle(state: dict, now: float) -> None:
    """Arm the 0.5s grace. Call this only when a SessionStart count hits zero."""
    if state["busy"]:
        return
    state["grace_until"] = now + SETTLE_SECS


def choose_pane_event(state: dict, event: str, session: str) -> str:
    """The one table event for this hook callback. No pane write happens here."""
    if session == AGGREGATE:
        count = int(state.get("aggregate") or 0)
        if event == "SessionStart":
            return "record_aggregate"
        if event in BUSY_EVENTS:
            if count > 0:
                return "touch_aggregate"
            return "aggregate_zero"
        if event in END_EVENTS:
            if count == 0:
                return "aggregate_zero"
            if count > 1:
                return "touch_partial"
            return hook_event(state, event)
    if event in BUSY_EVENTS:
        return "record_named"
    return hook_event(state, event)


def _record_named(state: dict, event: str, session: str, now: float, owner: int, owner_started: str) -> None:
    """Apply record_named: one busy key and that owner, whichever key it is."""
    busy = state["busy"]
    if event == "SessionStart" and session in busy:
        index = 2
        while f"{session}#{index}" in busy:
            index += 1
        slot = f"{session}#{index}"
        state["busy"][slot] = now
        remember_owner(state, slot, owner, owner_started)
        return
    if event == "SessionStart" or not members_of(busy, session):
        state["busy"][session] = now
        remember_owner(state, session, owner, owner_started)
        return
    for member in members_of(busy, session):
        state["busy"][member] = now
        remember_owner(state, member, owner, owner_started)


def _finish_slot(state: dict, session: str, now: float) -> None:
    """Drop the slot a done result ended. Named sessions do not arm grace."""
    if session == AGGREGATE:
        count = int(state.get("aggregate") or 0)
        if count > 0:
            count -= 1
            state["aggregate"] = count
        if count == 0 and AGGREGATE in state["busy"]:
            _pop_session(state, AGGREGATE)
        if count == 0:
            finish_if_idle(state, now)
        return
    if session in state["busy"]:
        _pop_session(state, session)
        return
    members = members_of(state["busy"], session)
    if not members:
        return
    oldest = min(members, key=lambda item: (state["busy"][item], item))
    _pop_session(state, oldest)


_PANE_OUTCOME = {"write": "none", "ok": True, "event": "", "release": False}
# Preview folds classify a tick without painting, logging, or touching the pane.
_COMMIT_DRY = False
_WRITE_DEPTH = 0


@contextlib.contextmanager
def _applying():
    """Count a write commit_pane is applying. A classifier never enters."""
    global _WRITE_DEPTH
    _WRITE_DEPTH += 1
    try:
        yield
    finally:
        _WRITE_DEPTH -= 1


def _in_apply():
    """The apply gate, or nothing while a preview is only classifying."""
    if _COMMIT_DRY:
        return contextlib.nullcontext()
    return _applying()


def apply_event(
    state: dict,
    event: str,
    session: str,
    now: float,
    owner: int = 0,
    owner_started: str = "",
    chat_pid: int = 0,
) -> dict:
    """Apply the one table result for this hook callback.

    none returns the same state. refresh changes only the aggregate timestamp.
    A Stop at aggregate zero is aggregate_zero only for the aggregate session.
    A named Stop stays stop except when the chat child's stdout is a pipe,
    where the event is piped_chat.
    """
    owner_before = _pane_owner(state)
    ev = choose_pane_event(state, event, session)
    write, ok = commit_pane(
        state,
        ev,
        now=now,
        owner=owner,
        owner_started=owner_started,
        session=session,
        hook_name=event,
    )
    release = bool(write == "done" and ok and owner_before is not None and not alive(owner_before[0]))
    _PANE_OUTCOME["write"] = write
    _PANE_OUTCOME["ok"] = ok
    _PANE_OUTCOME["event"] = ev
    _PANE_OUTCOME["release"] = release
    if write == "none" or not ok or write == "refresh":
        return state
    if chat_pid:
        state["chat_pid"] = int(chat_pid)
        token = hires_start(int(chat_pid))
        state["chat_ident"] = token if fine_start(token) else ""
    note_generation(state, state)
    return copy.deepcopy(state)


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
    existed = os.path.isfile(path)
    silent = False
    fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        parsed = None
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        readable = isinstance(parsed, dict)
        loaded = parsed if readable else fresh_state()
        before = copy.deepcopy(loaded)
        _PANE_OUTCOME["write"] = ""
        fn(loaded)
        floor = read_seq_floor(path)
        try:
            seq = int(loaded.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        changed = loaded != before
        write = _PANE_OUTCOME.get("write") or ""
        # none, and a read that did not change the pane, leave the file and
        # the seq floor untouched. refresh may change the busy timestamp and
        # does not raise seq or create the floor file. A missing file is
        # removed afterwards. Unreadable bytes stay. A real mutation still
        # raises seq to the floor.
        if write == "refresh":
            if not changed:
                silent = True
            else:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(loaded))
                handle.flush()
                os.chmod(path, FILE_MODE)
        elif not changed and write in ("", "none"):
            silent = True
        else:
            if seq < floor:
                loaded["seq"] = floor
            elif floor == 0 and seq == 0 and (changed or not readable):
                # A deleted floor must not restart at seq 1. herdr ignores that.
                loaded["seq"] = int(time.time())
            if readable and loaded == before:
                silent = True
            else:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(loaded))
                handle.flush()
                try:
                    written = int(loaded.get("seq") or 0)
                except (TypeError, ValueError):
                    written = 0
                write_seq_floor(path, written)
                os.chmod(path, FILE_MODE)
    if silent and not existed:
        with contextlib.suppress(OSError):
            os.unlink(path)
    return loaded


def _load_for_commit(path: str):
    """Read a pane without creating it or copying the seq floor into it."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return fresh_state(), False, False
    if not raw.strip():
        return fresh_state(), True, False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return fresh_state(), True, False
    if not isinstance(parsed, dict):
        return fresh_state(), True, False
    return parsed, True, True


def _write_refresh(path: str, state: dict) -> None:
    """Replace the pane json. Seq, the floor, and every other file stay."""
    fd = os.open(path, os.O_RDWR, FILE_MODE)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state))
        handle.flush()
        os.chmod(path, FILE_MODE)


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
# The watcher opens it in this interpreter. The exit writes this fd only
# when _held_pid is that owner. A mismatched or unconfirmed pid writes
# nothing. The token is the identity that proved the owner when the fd
# was opened.
_pane_fd = -1
_held_ident = ""
_held_pid = 0
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
    global _pane_fd, _held_ident, _held_pid
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
    _held_pid = pid


def _close_held_fd() -> None:
    global _pane_fd, _held_ident, _held_pid
    fd = _pane_fd
    _pane_fd = -1
    _held_ident = ""
    _held_pid = 0
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


def paint_status(kind: str, pid: int, token: str, *, held_fd: int = -1) -> bool:
    """Write state=working or state=done for one proven owner.

    commit_pane is the path that decides to paint. This writes the bytes on
    that pane's one outer stdout tty, or on the held fd after the caller
    has confirmed `_held_pid` is the owner. A missing token, sec: plus
    whole-second lstart, or a bare whole-second lstart is not eligible.
    A live fine token that does not match the live start is not eligible,
    including on that held fd. A gone pid has no live start, so the held
    token is written when it is fine and equals the token stored for the fd.
    """
    if in_tui():
        return False
    fields = herdr_kiro_osc.fields_for(kind)
    if fields is None or fields.get("state") not in ("working", "done"):
        return False
    # The held fd is the tty opened for this fine token. While the pid is
    # alive, the token still has to match the live start. A gone pid has no
    # live start; the held token is the one captured when it matched.
    if held_fd >= 0:
        if held_fd != _pane_fd or not fine_start(token) or token != _held_ident:
            return False
        # A live pid has to match the start captured when the fd was opened.
        # A reaped pid has no live start; kill is that check.
        if pid > 1 and not _pid_gone(pid) and hires_start(pid) != token:
            return False
    elif not identity_proven(pid, token):
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
    """Write idle's wire state=done through the held fd.

    Call this only when `_held_pid` is the owner. This does not open another tty.
    """
    fd = _pane_fd
    if fd < 0 or not fine_start(_held_ident):
        return False
    return paint_status("idle", _held_pid, _held_ident, held_fd=fd)


def _watcher_still_us(path: str) -> bool:
    """True when this pid is still the watcher recorded in the state file.

    Read the file directly. A weak or sec: exit writes none and leaves the row.
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


def emit_osc(state: dict | str, kind: str, rule_id: str = "") -> bool:
    """Write blocked on the pane's one outer tty.

    commit_pane is the only caller in this module. working and done are
    painted there through paint_status. A chat pty, a second recorded pid,
    and this process's tty are not targets. Unknown is not a protocol
    state, so it is a no-op. An unconfirmed held pid, including zero,
    writes nothing and does not open the live stdout. A path loads the
    saved pane.
    """
    if isinstance(state, str):
        state = _load_state(state)
    if not isinstance(state, dict):
        return False
    fields = herdr_kiro_osc.fields_for(kind, rule_id)
    if fields is None:
        return True
    if fields.get("state") != "blocked":
        return True
    if in_tui():
        return True
    owner = _pane_owner(state)
    if owner is None:
        return False
    pid, token = owner
    if not identity_proven(pid, token):
        return False
    if _held_pid_unconfirmed(pid, token):
        return False
    try:
        payload = herdr_kiro_osc.encode(fields)
    except ValueError:
        if not _COMMIT_DRY:
            log("refusing osc")
        return False
    if _COMMIT_DRY:
        return True
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
    # OSC is commit_pane's paint. This report is the Herdr TUI only.
    return emit_report(pane, path, kind)


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

    A successful empty `hc read` is "". That is not the Kiro CLI 3.0 chevron
    prompt, so it does not end the turn. TUI reads stay strings, including
    "" when the pane read fails. A TUI pane does not call hc read, even when
    HC_TERMINAL_ID is set.
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


@dataclasses.dataclass(frozen=True)
class PidFileView:
    """One pid file, read before anyone decides to unlink it."""

    pid: int
    kill_errno: int | None
    recorded_start: str
    recorded_comm: str
    live_start: str
    live_comm: str
    snap_error: bool


def classify_pidfile(view: PidFileView) -> str:
    """Return reap or keep. The view is data. This does not unlink the file.

    reap is a confirmed death (kill errno ESRCH) or a confirmed token
    mismatch. EPERM, a snapshot error, and an empty recorded start stay.
    A live pid whose token matches is kept when only the comm name differs.
    """
    if view.kill_errno == errno.ESRCH:
        return "reap"
    if view.kill_errno is not None or view.snap_error or not view.recorded_start:
        return "keep"
    if view.live_start and view.live_start != view.recorded_start:
        return "reap"
    return "keep"


def sweep_stale_pidfiles() -> None:
    """Unlink pid files classify_pidfile calls reap.

    commit_pane applies this for the reap result, after a fresh kill and
    snapshot. none and refresh do not call it. A screen read does not either.
    """
    directory = os.path.join(state_dir(), "pids")
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        if not name.isdigit():
            continue
        path = os.path.join(directory, name)
        pid = int(name)
        kill_errno = None
        try:
            os.kill(pid, 0)
        except OSError as exc:
            kill_errno = exc.errno
        recorded = ""
        recorded_comm = ""
        live_start = ""
        live_comm = ""
        snap_error = False
        if kill_errno is None:
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                continue
            recorded = lines[1].strip() if len(lines) > 1 else ""
            recorded_comm = lines[2].strip() if len(lines) > 2 else ""
            snap = process_snapshot(pid)
            snap_error = not snap or bool(snap.get("error"))
            if not snap_error:
                live_start = hires_start(pid)
                live_comm = (snap or {}).get("comm") or ""
        view = PidFileView(pid, kill_errno, recorded, recorded_comm, live_start, live_comm, snap_error)
        if classify_pidfile(view) != "reap":
            continue
        remove_pidfile(os.path.join(state_dir(), "pane.json"), pid)


def cleanup_rules() -> None:
    if _RULES_DIR and os.path.isdir(_RULES_DIR):
        remove_tree(_RULES_DIR)
    sweep_stale_rules()


def _on_term(signum, _frame):
    cleanup_rules()
    raise SystemExit(128 + signum)


def explain_env() -> dict:
    """Point herdr at the toml next to this script. The temp dir is removed on exit.

    This does not sweep pid files. A pid file is removed only when commit_pane
    applies reap.
    """
    global _EXPLAIN_ENV
    if _EXPLAIN_ENV is not None:
        return _EXPLAIN_ENV
    global _RULES_DIR
    sweep_stale_rules()
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
    """Winning herdr rule for this screen. A failed explain is not cached.

    This is the herdr reader. explain, screen_is_settled, and report_for do
    not call it. commit_pane calls it only while applying a write.
    """
    cached = _SCREEN_CACHE.get(text)
    if cached is not None:
        return cached
    path = None
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


@dataclasses.dataclass(frozen=True)
class ScreenSnapshot:
    """Text already read, plus an explain result when the caller has one.

    text is None when the App read failed. That is not an empty screen.
    Classifiers take this and return a result. They do not receive a writer.
    """

    text: str | None
    explained: dict | None = None


def _as_snapshot(screen: ScreenSnapshot | str | None) -> ScreenSnapshot:
    if isinstance(screen, ScreenSnapshot):
        return screen
    if screen is None:
        return ScreenSnapshot(None, None)
    return ScreenSnapshot(screen, None)


def explain(screen: ScreenSnapshot | str | None) -> dict:
    """The explain result already on the snapshot. This does not call herdr.

    A failed App read (text is None) is unknown. It is not explained here.
    A string with no explained result is unknown too: the text rules are
    separate, and this function does not invent a herdr state.
    """
    snap = _as_snapshot(screen)
    if snap.text is None:
        return _unknown_screen()
    view = snap.explained
    if isinstance(view, dict) and view.get("ok"):
        return {"state": view.get("state") or "idle", "id": view.get("id"), "ok": True}
    return _unknown_screen()


def blocker_visible(screen: ScreenSnapshot | str | None) -> bool:
    """True when approval text is on screen, or the snapshot's explain result is blocked.

    approval_text returns first. A chooser footer is blocked here when explain is not.
    A failed App read is not blocked. This does not call screen_view.
    """
    snap = _as_snapshot(screen)
    if snap.text is None:
        return False
    if approval_text(snap.text):
        return True
    view = explain(snap)
    return bool(view.get("ok")) and view.get("state") == "blocked"


def screen_is_settled(screen: ScreenSnapshot | str | None) -> bool:
    """No workflow banner, no blocked explain result, and no working footer.

    The inputs are the snapshot. This does not touch the filesystem, the tty,
    or OSC. Settled is also false when approval_text matches and explain is
    not blocked. A question panel is a blocked explain result with no approval
    text, so it is not settled. A failed App read is not settled and is not
    an empty screen. The kiro idle prompt qualifies. So does the shell prompt
    left behind after the outer kiro-cli exits, which does not contain that
    prompt.
    """
    snap = _as_snapshot(screen)
    if snap.text is None:
        return False
    if workflow_banner_from(snap.text) or blocker_visible(snap):
        return False
    return "kiro is working" not in tail_text(snap.text).lower()


def _idle_prompt_line(text: str) -> bool:
    """True when one tail line is the Kiro 3.0 chevron prompt."""
    return any(IDLE_PROMPT.search(line) for line in tail_text(text).splitlines())


def screen_is_quiet(screen: ScreenSnapshot | str | None) -> bool:
    """The kiro idle prompt, with nothing else running.

    A failed App read is not quiet. This does not read the pane again.
    """
    snap = _as_snapshot(screen)
    if snap.text is None or not _idle_prompt_line(snap.text):
        return False
    return screen_is_settled(snap)


def prompt_visible(text: str) -> bool:
    """The Kiro 3.0 chevron prompt.

    ``the price is $`` and a sentence that merely contains the words are
    model text. They are not the prompt.
    """
    return _idle_prompt_line(text)


def report_for(screen: ScreenSnapshot | str | None, action: str):
    """What this event should tell herdr. None means send nothing.

    A visible blocker is blocked. The idle prompt gets no working report, so a
    late tool event cannot cover it. A banner or a live turn still reports
    working. A failed App read sends nothing and is not explained.
    """
    snap = _as_snapshot(screen)
    if snap.text is None:
        return None
    if blocker_visible(snap):
        return "blocked"
    if screen_is_quiet(snap):
        return None
    if action == "working":
        return "working"
    return None


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
OWNER_EVENTS = (
    "start",
    "prompt",
    "stop",
    "idle_clear",
    "process_exit",
    "pid_reuse",
    "piped_chat",
    "blocked",
    "aggregate_zero",
    "record_aggregate",
    "touch_aggregate",
    "touch_partial",
    "record_named",
    "reap",
)
_DIRECT_EVENTS = (
    "aggregate_zero",
    "record_aggregate",
    "touch_aggregate",
    "touch_partial",
    "record_named",
    "reap",
)
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
    ("proven", "blocked"): "paint_blocked",
    ("proven", "aggregate_zero"): "refresh",
    ("no_owner", "stop"): "done",
    ("no_owner", "piped_chat"): "done",
    ("no_owner", "record_aggregate"): "record_aggregate",
    ("proven", "record_aggregate"): "record_aggregate",
    ("weak", "record_aggregate"): "record_aggregate",
    ("exited", "record_aggregate"): "record_aggregate",
    ("idle_clear", "record_aggregate"): "record_aggregate",
    ("no_owner", "touch_aggregate"): "touch_aggregate",
    ("proven", "touch_aggregate"): "touch_aggregate",
    ("weak", "touch_aggregate"): "touch_aggregate",
    ("exited", "touch_aggregate"): "touch_aggregate",
    ("idle_clear", "touch_aggregate"): "touch_aggregate",
    ("no_owner", "touch_partial"): "touch_partial",
    ("proven", "touch_partial"): "touch_partial",
    ("weak", "touch_partial"): "touch_partial",
    ("exited", "touch_partial"): "touch_partial",
    ("idle_clear", "touch_partial"): "touch_partial",
    ("no_owner", "record_named"): "record_named",
    ("proven", "record_named"): "record_named",
    ("weak", "record_named"): "record_named",
    ("exited", "record_named"): "record_named",
    ("idle_clear", "record_named"): "record_named",
}
for _phase in OWNER_STATES:
    _PANE_WRITES[(_phase, "reap")] = "reap"


def pane_transition(state: str, event: str) -> str:
    """The table result for one owner state and one event.

    The result is working, done, paint_blocked, refresh, none, reap,
    record_aggregate, touch_aggregate, touch_partial, or record_named.
    reap is the only result that unlinks a pid file, and only commit_pane
    applies it.
    """
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
    ("idle_clear", "pid_reuse"): "weak",
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
    """The pane's one outer pid.

    The chat child is not an owner. A second distinct pid is not a second
    record. A live pid is that record, so a dead earlier pid does not exit
    the pane while a later outer pid is still alive.
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
    if not found:
        return []
    for pair in found:
        if not _pid_gone(pair[0]):
            return [pair]
    return [found[0]]


def _pane_owner(state: dict) -> tuple[int, str] | None:
    """The pane's one outer pid."""
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
    """The pane's one outer process is gone.

    A later recorded pid that is still alive is that owner, so the pane has
    not exited. A dropped row still counts when every remembered outer pid
    is gone.
    """
    owner = _pane_owner(state)
    if owner is not None:
        return _pid_gone(owner[0])
    pids = []
    for item in remembered:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 1:
            pids.append(number)
    if not pids:
        return False
    return all(_pid_gone(pid) for pid in pids)


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

    A stored idle_clear phase stays idle_clear for a gone fine token, and
    for a live owner that is identity_proven. A live unproven token is
    weak, including empty, sec: plus whole-second lstart, and a fine token
    that does not match, even when the stored phase is idle_clear. A gone
    unproven token is weak only while the stored phase is idle_clear. A
    stored exited phase stays exited, including when that owner is still
    live. When the stored phase is neither idle_clear nor exited, a live
    identity-proven owner is proven and any other live owner is weak. Any
    other gone owner is exited. A chat child does not change this.
    """
    stored = state.get("phase") or ""
    owner = _pane_owner(state)
    if stored == "idle_clear":
        if owner is None:
            return "idle_clear"
        pid, token = owner
        # A reaped pid can still be in the process table. kill decides.
        if not _pid_gone(pid) and not identity_proven(pid, token):
            return "weak"
        if _pid_gone(pid) and not fine_start(token):
            return "weak"
        return "idle_clear"
    if stored == "exited":
        return "exited"
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

    A stored idle_clear phase is idle_clear or weak from owner_phase, not
    exited. process_exit chooses done for a gone fine token whose stored
    phase is proven or idle_clear. A stored weak phase stays weak, and
    that exit writes none.
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
    proven_exit = fine_start(token) and not alive(pid) and (stored == "proven" or _held_proves(token, pid))
    if proven_now or proven_exit:
        state["proven_pid"] = pid
        state["proven_ident"] = token


def _held_pid_unconfirmed(pid: int, token: str) -> bool:
    """True when a held fd names this token but not this owner pid.

    Zero and any other pid are unconfirmed. The tty stays empty and the
    saved owner stays as it was.
    """
    if _pane_fd < 0 or _held_ident != token:
        return False
    return _held_pid <= 1 or _held_pid != pid


def _paint_one_owner(state: dict, kind: str) -> bool:
    """Write working or done on the pane's one outer stdout tty.

    A TUI pane, including one inside the App, gets no OSC. A chat pty and a
    second recorded pid are not targets. The current token is checked before
    any held fd is written. An empty token or sec: plus whole-second lstart
    writes nothing, including when a held fd is open. An unconfirmed held
    pid writes nothing for every kind, including working, and does not open
    the live stdout. The held fd is eligible only when its pid is this
    owner. With no held fd, a live proven owner still paints through
    paint_status.
    """
    if in_tui():
        return True
    owner = _pane_owner(state)
    if owner is None:
        return False
    pid, token = owner
    if not fine_start(token):
        return False
    # A live pid whose token does not match is not painted, including when
    # a held fd is open. kill is the exit check, so a reaped owner can still
    # write that fd when _held_pid is this owner.
    if not _pid_gone(pid) and not identity_proven(pid, token):
        return False
    if _held_pid_unconfirmed(pid, token):
        # The fd was opened for _held_pid. Zero, or any other pid, is a
        # different terminal. Do not paint working or idle, and do not open
        # the live stdout.
        return False
    if _COMMIT_DRY:
        return True
    if kind == "idle" and _pane_fd >= 0 and _held_ident == token:
        return _write_held_fd()
    return paint_status(kind, pid, token)


def _pane_result(write: str, ok: bool) -> tuple[str, bool]:
    """Remember the table result so update can leave a none file untouched."""
    _PANE_OUTCOME["write"] = write
    _PANE_OUTCOME["ok"] = ok
    return write, ok


def _owner_held_unconfirmed(state: dict) -> bool:
    owner = _pane_owner(state)
    if owner is None:
        return False
    return _held_pid_unconfirmed(owner[0], owner[1])


def _leaves_directory_untouched(write: str, ok: bool, trial: dict, state: dict) -> bool:
    """True when this result must not open the pane, the floor, or the log.

    none with an unchanged trial does not. A done, working, or blocked
    result that refused an unconfirmed held pid does not either, even when
    an earlier trial assignment changed the copy. That refusal does not
    arm a watcher or rewrite the pane.
    """
    if write == "none" and trial == state:
        return True
    refused = write in ("done", "working", "paint_blocked") and not ok
    return bool(refused and _owner_held_unconfirmed(state))


def _file_generation_matches(path: str, generation: int) -> bool:
    loaded, existed, readable = _load_for_commit(path)
    if not existed or not readable:
        return True
    try:
        found = int(loaded.get("generation") or 0)
    except (TypeError, ValueError):
        found = 0
    return found == int(generation)


def _preview_pane(state: dict, event: str, now: float):
    """Classify one event. The trial is not the file, and paint is not written."""
    trial = copy.deepcopy(state)
    global _COMMIT_DRY
    previous = _COMMIT_DRY
    _COMMIT_DRY = True
    try:
        write, ok = commit_pane(trial, event, now=now)
    finally:
        _COMMIT_DRY = previous
    return write, bool(ok), trial


def _arm_watch(path: str, pane: str, claim, armed: dict) -> None:
    """Claim the slot on the first real write. none and refresh never arm."""
    if armed["on"]:
        return
    with _applying():
        update(path, claim)
        write_pidfile(path, os.getpid())
        log(f"watcher loop pane={pane}")
    armed["on"] = True


def _persist_watch(path: str, state: dict, event: str, now: float, arm, generation: int | None = None):
    """Write one watcher event, or return the same state when the result is quiet."""
    if generation is not None and not _file_generation_matches(path, generation):
        return "none", False, state, True
    write, ok, trial = _preview_pane(state, event, now)
    if write == "refresh":
        # pane_text may have moved the file generation after this state was loaded.
        # Refresh rewrites that file, so the new timestamp has to land on it.
        loaded, existed, readable = _load_for_commit(path)
        if not existed or not readable:
            return write, ok, state, True
        file_write, file_ok, file_trial = _preview_pane(loaded, event, now)
        if file_write == "refresh" and file_trial != loaded:
            with _applying():
                _write_refresh(path, file_trial)
            return file_write, file_ok, file_trial, False
        return file_write, file_ok, state, True
    if _leaves_directory_untouched(write, ok, trial, state):
        return write, ok, state, True
    arm()

    def apply(current: dict, event: str = event, generation: int | None = generation, now: float = now) -> None:
        if generation is None:
            commit_pane(current, event, now=now)
            return
        commit_if_current(current, event, generation)

    with _applying():
        saved = update(path, apply)
    recorded = str(_PANE_OUTCOME.get("write") or write)
    return recorded, bool(_PANE_OUTCOME.get("ok")), saved, False


def _fold_hook(
    state: dict,
    event: str,
    session: str,
    now: float,
    owner: int,
    owner_started: str,
    chat_pid: int,
    chosen: dict,
) -> None:
    """The hook fold. A later none keeps an earlier mutation for update()."""
    revised = apply_event(state, event, session, now, owner, owner_started, chat_pid)
    chosen["write"] = str(_PANE_OUTCOME["write"])
    chosen["ok"] = bool(_PANE_OUTCOME["ok"])
    chosen["release"] = bool(_PANE_OUTCOME["release"])
    slot_ev = str(_PANE_OUTCOME["event"])
    # A busy hook can stamp the slot and then paint. A Stop at aggregate
    # zero is refresh for a proven owner and none for every other phase.
    # The follow-up commit is skipped because Stop is an end event.
    # SessionStart is record_aggregate, not an exit. Do not follow it
    # with start when that owner already owes process_exit.
    if event not in END_EVENTS and slot_ev != "aggregate_zero":
        target = state if revised is state else revised
        pane_ev = hook_event(target, event)
        owed = event == "SessionStart" and _proven_exit_owed(target)
        if pane_ev != slot_ev and not owed:
            owner_now = _pane_owner(target)
            dead = owner_now is not None and not alive(owner_now[0])
            write2, ok2 = commit_pane(target, pane_ev)
            if write2 != "none":
                chosen["write"] = write2
                chosen["ok"] = ok2
                revised = target
                if write2 == "done" and ok2 and dead:
                    chosen["release"] = True
    if revised is not state:
        saved = dict(revised)
        state.clear()
        state.update(saved)


def _hook_effects(pane: str, path: str, event: str, chosen: dict, state: dict) -> None:
    """Reports and the watcher follow a persisted result. none and refresh do not."""
    if chosen["write"] in ("none", "refresh"):
        return
    ensure_watcher(pane, path)
    screen = pane_text(pane)
    # A failed App read is not an empty screen. Do not explain it, and do
    # not commit_blocked for a screen the App did not return.
    if not isinstance(screen, str):
        return
    view = screen_view(screen)
    snap = ScreenSnapshot(screen, view if view.get("ok") else None)
    action = next_action(
        len(state["busy"]),
        float(state["grace_until"]),
        workflow_banner_from(screen),
        time.time(),
    )
    kind = report_for(snap, action)
    if chosen["write"] == "done" and chosen["ok"] and (event in END_EVENTS or chosen["release"]):
        send_report(pane, path, "idle")
    elif kind == "working" and chosen["write"] == "working":
        send_report(pane, path, "working")
    elif kind == "blocked":
        rule_id = str(view.get("id") or "")
        write, ok = commit_blocked(path, rule_id)
        if write == "paint_blocked" and ok:
            send_report(pane, path, kind)
    if chosen["release"]:
        release_authority(pane, path)


def _commit_persisted(
    event: str,
    session: str,
    now: float,
    owner: int,
    owner_started: str,
    chat_pid: int,
    path: str,
    pane: str,
) -> tuple[str, bool]:
    """Persist one hook. none returns before update, the floor, and the log."""
    loaded, existed, readable = _load_for_commit(path)
    trial = copy.deepcopy(loaded) if readable else fresh_state()
    before = copy.deepcopy(trial)
    chosen = {"write": "none", "ok": True, "release": False}
    global _COMMIT_DRY
    previous = _COMMIT_DRY
    _COMMIT_DRY = True
    try:
        _fold_hook(trial, event, session, now, owner, owner_started, chat_pid, chosen)
    finally:
        _COMMIT_DRY = previous
    write = str(chosen["write"])
    ok = bool(chosen["ok"])
    if write == "none" and trial == before:
        return _pane_result("none", True)
    if write == "refresh":
        if existed and readable and trial != before:
            with _applying():
                _write_refresh(path, trial)
        return _pane_result("refresh", True)
    if _leaves_directory_untouched(write, ok, trial, before):
        return _pane_result(write, ok)

    def apply(current: dict, chosen: dict = chosen) -> None:
        _fold_hook(current, event, session, now, owner, owner_started, chat_pid, chosen)

    with _applying():
        log(f"event={event} session={session} pane={pane} chain={process_chain()}")
        saved = update(path, apply)
        log(f"state event={event} busy={len(saved['busy'])} grace_until={float(saved['grace_until']):.3f}")
        _hook_effects(pane, path, event, chosen, saved)
    return _pane_result(str(chosen["write"]), bool(chosen["ok"]))


def _status_view(screen: str, state: dict, held, now: float):
    """Classify a watcher screen from text already read.

    screen_is_settled does not call herdr and does not delete a pid file.
    screen_view runs only after the preview is a write. A failed App read
    is not passed here, so it is not explained and it is not painted.
    """
    blocked = bool(approval_text(screen)) or held == "blocked"
    banner = bool(workflow_banner_from(screen)) or (held == "working" and not blocked)
    view = _unknown_screen()
    if blocked:
        candidate = "blocked"
    elif banner:
        candidate = screen_event(state, "banner")
    elif prompt_visible(screen) and screen_is_settled(screen):
        candidate = screen_event(state, "settled")
    else:
        action = next_action(len(state.get("busy") or {}), float(state.get("grace_until") or 0), banner, now)
        candidate = screen_event(state, "continue") if action == "working" else ""
    if not candidate:
        return blocked, banner, view
    write, ok, trial = _preview_pane(state, candidate, now)
    if write == "refresh" or _leaves_directory_untouched(write, ok, trial, state):
        return blocked, banner, view
    with _in_apply():
        view = screen_view(screen)
    if not view.get("ok"):
        return blocked, banner, view
    blocked = view.get("state") == "blocked" or bool(approval_text(screen))
    banner = view.get("id") == "workflow_banner_working"
    return blocked, banner, view


def commit_pane(
    state: dict,
    event: str,
    rule_id: str = "",
    *,
    now: float = 0.0,
    owner: int = 0,
    owner_started: str = "",
    session: str = "",
    hook_name: str = "",
    path: str = "",
    pane: str = "",
    chat_pid: int = 0,
    watch: bool = False,
    fault: Exception | None = None,
) -> tuple[str, bool]:
    """Apply one table result.

    A path makes this the only door that writes the pane, the seq floor,
    the log, a watcher pid file, the tty, or OSC. A table result of none
    returns before that I/O and leaves the state directory unchanged,
    including pid files. refresh writes the busy timestamp and nothing
    else. reap unlinks a pid file only when the pid is confirmed dead or
    its recorded token is confirmed mismatched. A matching token is kept
    when only the comm name differs. blocked does not paint an unconfirmed
    held pid. fault logs the error and clears the watcher fields, and that
    none does not unlink a pid file. refuse_watch logs the refusal, then
    returns none.
    """
    if watch:
        _run_watch(pane, path)
        return _pane_result("none", True)
    if fault is not None:
        with _applying():
            log(f"watcher error {type(fault).__name__}: {fault}")
            clear_watcher(path)
        return _pane_result("none", False)
    if event == "refuse_watch":
        with _applying():
            log("refusing watch")
        return _pane_result("none", False)
    if path:
        return _commit_persisted(event, session, now, owner, owner_started, chat_pid, path, pane)
    count = int(state.get("aggregate") or 0)
    prompt_at_zero = count == 0 and event in ("prompt", "start")
    stop_at_zero = session == AGGREGATE and count == 0 and event in ("stop", "piped_chat")
    if prompt_at_zero or stop_at_zero:
        event = "aggregate_zero"
    elif event not in _DIRECT_EVENTS:
        event = _chooser_event(state, event)
    phase = _phase_for_event(state, event)
    write = pane_transition(phase, event)
    if write == "reap":
        if not _COMMIT_DRY:
            with _applying():
                sweep_stale_pidfiles()
        return _pane_result(write, True)
    if write == "none":
        return _pane_result(write, True)
    if write == "refresh":
        if AGGREGATE in state["busy"]:
            state["busy"][AGGREGATE] = now if now else time.time()
        return _pane_result(write, True)
    if write == "paint_blocked":
        owner_now = _pane_owner(state)
        if owner_now is not None and _held_pid_unconfirmed(owner_now[0], owner_now[1]):
            return _pane_result(write, False)
        with _in_apply():
            painted = emit_osc(state, "blocked", rule_id)
        return _pane_result(write, painted)
    if write == "record_aggregate":
        state["aggregate"] = int(state.get("aggregate") or 0) + 1
        state["busy"][AGGREGATE] = now
        remember_owner(state, AGGREGATE, owner, owner_started)
        state["grace_until"] = 0.0
        return _pane_result(write, True)
    if write == "touch_aggregate":
        if _owner_held_unconfirmed(state):
            return _pane_result(write, False)
        state["busy"][AGGREGATE] = now
        remember_owner(state, AGGREGATE, owner, owner_started)
        return _pane_result(write, True)
    if write == "touch_partial":
        count = int(state.get("aggregate") or 0)
        if count > 1:
            state["aggregate"] = count - 1
        state["busy"][AGGREGATE] = now
        state["grace_until"] = 0.0
        return _pane_result(write, True)
    if write == "record_named":
        _record_named(state, hook_name or event, session, now, owner, owner_started)
        state["grace_until"] = 0.0
        return _pane_result(write, True)
    if write == "done":
        drops = bool(session) and hook_name in END_EVENTS and _chooser_drops_row(state, hook_name)
        owner_now = _pane_owner(state)
        if owner_now is not None and alive(owner_now[0]):
            drops = False
        if owner_now is None:
            if drops:
                _finish_slot(state, session, now)
            return _pane_result(write, True)
        # An unconfirmed held pid writes nothing. Do not store proven_pid
        # and do not open the live stdout.
        if _held_pid_unconfirmed(owner_now[0], owner_now[1]):
            return _pane_result(write, False)
        _save_proven_owner(state)
        with _in_apply():
            ok = _paint_one_owner(state, "idle")
        if not ok:
            return _pane_result(write, False)
        state["phase"] = pane_next(phase, event)
        owner_after = _pane_owner(state)
        if owner_after is not None and not alive(owner_after[0]):
            _drop_done_owner(state, owner_after[0])
        if drops:
            _finish_slot(state, session, now)
        return _pane_result(write, True)
    if write == "working":
        owner_now = _pane_owner(state)
        if owner_now is not None and _held_pid_unconfirmed(owner_now[0], owner_now[1]):
            return _pane_result(write, False)
        with _in_apply():
            ok = _paint_one_owner(state, "working")
        state["phase"] = pane_next(phase, event)
        return _pane_result(write, ok)
    raise ValueError(write)


def commit_blocked(path: str, rule_id: str = "") -> tuple[str, bool]:
    """Run the blocked event through the table and paint only that result.

    An unconfirmed held pid returns before update, so the pane and the seq
    floor stay as they were and the live stdout is not opened.
    """
    loaded, _existed, readable = _load_for_commit(path)
    trial = copy.deepcopy(loaded) if readable else fresh_state()
    before = copy.deepcopy(trial)
    global _COMMIT_DRY
    previous = _COMMIT_DRY
    _COMMIT_DRY = True
    try:
        preview, preview_ok = commit_pane(trial, "blocked", rule_id)
    finally:
        _COMMIT_DRY = previous
    if _leaves_directory_untouched(preview, preview_ok, trial, before):
        return _pane_result(preview, preview_ok)
    chosen = {"write": "none", "ok": False}

    def apply(current: dict, chosen: dict = chosen, rule_id: str = rule_id) -> None:
        write, ok = commit_pane(current, "blocked", rule_id)
        chosen["write"] = write
        chosen["ok"] = ok

    update(path, apply)
    return str(chosen["write"]), bool(chosen["ok"])


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

    When start ticks and boot time are readable, the token is fractional
    seconds: boot plus ticks/clk, six digits. That is the Linux /proc token.
    Where those ticks or the boot time are absent, the token is sec: plus
    the whole-second ps lstart. That form matches a pid reused in the same
    second.
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
    """True when the process table still names this pid.

    A missing snapshot is death. A ps error is not death unless kill reports
    ESRCH. EPERM is not death. The start token is not this check.
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
    # The pid file stores whatever hires_start returns. When /proc start
    # ticks and boot time are readable, that token is fractional seconds
    # and uninstall signals it. Where those ticks or the boot time are
    # absent, the token is sec: plus whole-second lstart. A same-second
    # reuse matches that form, so it is not a signal to send.
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
    """Clear this process's watcher fields. Do not unlink the pid file.

    A none result unlinks nothing. Watcher exit removes a pid file only
    when commit_pane applies reap, and reap keeps a live matching token.
    """

    def clear(state: dict) -> None:
        if int(state.get("watcher") or 0) == os.getpid():
            state["watcher"] = 0
            state["watcher_started"] = ""

    update(path, clear)


def commit_if_current(current: dict, event: str, generation: int) -> tuple[str, bool]:
    """Paint only when this generation is still the current one.

    A prompt that landed in the tick must not be overwritten with done.
    """
    if int(current.get("generation") or 0) != int(generation):
        return _pane_result("none", False)
    return commit_pane(current, event)


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

    This does not write working or done, and it does not pop busy or owners.
    The caller paints before it asks.
    """
    me = os.getpid()
    generation = int(state.get("generation") or 0)
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
        decision["ok"] = True

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
        self._spins = 0
        self.early = False

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
            self.early = False
            self._spins = 0
            time.sleep(timeout)
            return False
        self.early = False
        if hasattr(select, "kqueue") and hasattr(select, "KQ_FILTER_PROC") and hasattr(select, "KQ_NOTE_EXIT"):
            return self._wait_kqueue(wanted, timeout)
        if hasattr(os, "pidfd_open"):
            try:
                return self._wait_pidfd(wanted, timeout)
            except OSError:
                time.sleep(max(0.0, timeout))
                return False
        return self._wait_kill(wanted, timeout)

    def _forget_fd(self, pid: int) -> None:
        fd = self._fds.pop(pid, None)
        if fd is None:
            return
        with contextlib.suppress(OSError):
            os.close(fd)

    def _fd_ready(self, fd: int) -> bool:
        poller = select.poll()
        try:
            poller.register(fd, select.POLLIN)
        except (OSError, ValueError):
            return True
        return bool(poller.poll(0))

    def _drop_ready_pidfds(self) -> None:
        """Close pidfds that already show an exit so a reused pid is opened again."""
        ready = [pid for pid, fd in self._fds.items() if self._fd_ready(fd)]
        for pid in ready:
            self._forget_fd(pid)

    def _drop_watches(self) -> None:
        for pid in list(self._fds):
            self._forget_fd(pid)
        self._reset_kqueue()

    def _reset_kqueue(self) -> None:
        if self._kq is None:
            return
        with contextlib.suppress(OSError):
            self._kq.close()
        self._kq = None

    def _pause_for_ready_exit(self) -> None:
        """Sleep after an exit that was already visible. The delay is capped."""
        self.early = True
        self._spins += 1
        if self._spins >= POLL_SPIN_CAP:
            self._spins = POLL_SPIN_CAP
            self._drop_watches()
        delay = min(TICK_SECS * self._spins, POLL_BACKOFF_CAP)
        time.sleep(delay)

    def _already_visible(self, started: float) -> bool:
        return time.monotonic() - started < 0.05

    def _wait_pidfd(self, pids, timeout: float) -> bool:
        for pid in list(self._fds):
            if pid not in pids:
                self._forget_fd(pid)
        for pid in pids:
            existing = self._fds.get(pid)
            # A pidfd that is already readable belongs to the exited pid.
            # Reopening watches the pid number that is listed now.
            if existing is not None and not self._fd_ready(existing):
                continue
            if existing is not None:
                self._forget_fd(pid)
            try:
                self._fds[pid] = os.pidfd_open(pid)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    self._pause_for_ready_exit()
                    return True
                # EPERM is not a wake. Sleep the timeout instead of polling.
                time.sleep(max(0.0, timeout))
                return False
        poller = select.poll()
        for fd in self._fds.values():
            poller.register(fd, select.POLLIN)
        started = time.monotonic()
        ready = bool(poller.poll(max(0, int(timeout * 1000))))
        if ready and self._already_visible(started):
            self._drop_ready_pidfds()
            self._pause_for_ready_exit()
            return True
        if not ready:
            self._spins = 0
        return ready

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
                    self._pause_for_ready_exit()
                    return True
                time.sleep(max(0.0, timeout))
                return False
        started = time.monotonic()
        try:
            events = self._kq.control(changes, 1, timeout)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                self._pause_for_ready_exit()
                return True
            time.sleep(max(0.0, timeout))
            return False
        ready = bool(events)
        if ready and self._already_visible(started):
            # Drop the queued note so a reused pid is not an instant wake.
            self._reset_kqueue()
            self._pause_for_ready_exit()
            return True
        if not ready:
            self._spins = 0
        return ready

    def _wait_kill(self, pids, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            for pid in pids:
                try:
                    os.kill(pid, 0)
                except OSError as exc:
                    if exc.errno == errno.ESRCH:
                        # Already gone. Same capped backoff as a pidfd or
                        # kqueue exit that was visible on entry.
                        self._pause_for_ready_exit()
                        return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


def watch_loop(pane: str, path: str) -> None:
    try:
        _watch_loop(pane, path)
    except Exception as exc:
        commit_pane(fresh_state(), "watcher_fault", path=path, pane=pane, fault=exc)


def _watch_loop(pane: str, path: str) -> None:
    commit_pane(fresh_state(), "watch", path=path, pane=pane, watch=True)


def _run_watch(pane: str, path: str) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _on_term)
    watcher_token = hires_start(os.getpid())
    armed = {"on": False}

    def claim(state: dict) -> None:
        state["watcher"] = os.getpid()
        state["watcher_started"] = watcher_token

    def arm() -> None:
        _arm_watch(path, pane, claim, armed)

    reported = None
    misses = 0
    quiet_since = None
    held = None
    owner_watch = OwnerExitWatch()
    # A SessionEnd that pane_transition accepts keeps a live proven owner.
    # The row stays while that process is alive. A weak owner and a dead
    # sec: owner stay. known_outers keeps the last pid so that exit still
    # wakes the watcher. known_owners is only the live wait set, and an
    # empty owner list clears it.
    known_owners = []
    known_outers = []
    gone_since = None
    gone_hits = 0
    exit_misses = 0
    ready_spins = 0
    try:
        while True:
            tick_started = time.monotonic()
            owner_watch.wait(known_owners, TICK_SECS)
            # wait() sleeps when a pidfd or kqueue exit is already visible.
            # A tick that still returned early sleeps here, up to the cap,
            # so this loop cannot busy-poll the process list or herdr.
            if owner_watch.early:
                ready_spins += 1
                if time.monotonic() - tick_started < TICK_SECS * 0.5:
                    time.sleep(min(TICK_SECS * ready_spins, POLL_BACKOFF_CAP))
                if ready_spins >= POLL_SPIN_CAP:
                    owner_watch._drop_watches()
                    ready_spins = POLL_SPIN_CAP
            else:
                ready_spins = 0
            begin_tick()
            loaded, _existed, readable = _load_for_commit(path)
            state = loaded if readable else fresh_state()
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
            # An empty owner list drops the previous pid. A list whose pids
            # are already gone does too, so kill(0) is not spun on a miss.
            known_owners = live_wait
            pane_owner = _pane_owner(state)
            owner_alive = False
            # pane_transition returns working, done, paint_blocked, refresh,
            # none, reap, record_aggregate, touch_aggregate, touch_partial,
            # or record_named. commit_pane stores pane_next only for working
            # and done. This loop does not assign phase. Holding the tty does
            # not write the phase. A screen read does not reap a pid file.
            if pane_owner is not None and not _pid_gone(pane_owner[0]) and alive(pane_owner[0]):
                owner_alive = True
                if identity_proven(pane_owner[0], pane_owner[1]):
                    _hold_live_owner(pane_owner[0], pane_owner[1])
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
                exit_event = screen_event(state, "exit")
                preview, preview_ok, trial = _preview_pane(state, exit_event, now)
                if _leaves_directory_untouched(preview, preview_ok, trial, state):
                    return
                arm()
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
            # A live weak owner, including one stored as idle_clear, reaches
            # the chooser as pid_reuse. That event writes none.
            if _owner_is_live_weak(state):
                _write, _ok, state, _skipped = _persist_watch(path, state, screen_event(state, "reuse"), now, arm)
            screen = pane_text(pane)
            # None is a failed App read. It is none: not "", a prompt, a
            # banner, a start, or aggregate_zero, and not refresh. It is
            # not explained and not painted. A held banner or blocker is
            # not painted from a screen that was not returned.
            if not isinstance(screen, str):
                continue
            blocked, banner, view = _status_view(screen, state, held, now)
            if blocked:
                held = "blocked"
            elif banner:
                held = "working"
            if blocked:
                quiet_since = None
                if reported != "blocked":
                    preview, preview_ok, trial = _preview_pane(state, "blocked", now)
                    quiet = _leaves_directory_untouched(preview, preview_ok, trial, state)
                    if not quiet:
                        arm()
                        rule_id = str(view.get("id") or "")
                        write, ok = commit_blocked(path, rule_id)
                        if write == "paint_blocked" and ok:
                            reported, misses = note_report(pane, path, "blocked", reported, misses)
                            if reported is None and misses >= 30:
                                return
                continue
            if banner:
                quiet_since = None
            elif prompt_visible(screen) and screen_is_settled(screen):
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= IDLE_CLEAR_SECS and state["busy"]:
                    generation = int(state.get("generation") or 0)
                    write, ok, state, skipped = _persist_watch(
                        path,
                        state,
                        screen_event(state, "timer"),
                        now,
                        arm,
                        generation,
                    )
                    cleared = not skipped and write == "done" and ok and state.get("phase") == "idle_clear"
                    if cleared:
                        log("screen idle clear")
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
            if banner and not owners_gone:
                write, _ok, state, skipped = _persist_watch(path, state, screen_event(state, "banner"), now, arm)
                if not skipped and write == "working":
                    reported, misses = note_report(pane, path, "working", reported, misses)
                    if reported is None and misses >= 30:
                        return
                continue
            # Idle is only the Kiro CLI 3.0 chevron prompt. An empty App
            # screen is not that prompt and does not end the turn. A failed
            # read already continued, so it is not this prompt and it does
            # not paint done. A TUI pane keeps the TUI rule, even when
            # HC_TERMINAL_ID is set.
            if action == "idle" and screen_is_quiet(screen):
                if reported != "idle":
                    generation = int(state.get("generation") or 0)
                    write, ok, state, skipped = _persist_watch(
                        path,
                        state,
                        screen_event(state, "settled"),
                        now,
                        arm,
                        generation,
                    )
                    if skipped:
                        continue
                    if write == "done" and not ok:
                        # A failed done is not a finished idle. Leave the row.
                        exit_misses += 1
                        if exit_misses >= EXIT_RETRY_CAP:
                            clear_watcher(path)
                            log("watcher exit done missed")
                            return
                        time.sleep(min(TICK_SECS * exit_misses, 2.0))
                        continue
                    if write == "done":
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
            if action != "working" or owners_gone:
                continue
            write, _ok, state, skipped = _persist_watch(path, state, screen_event(state, "continue"), now, arm)
            if skipped or write != "working":
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
            commit_pane(fresh_state(), "refuse_watch", pane=pane, path=expected or "")
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
    commit_pane(
        fresh_state(),
        event,
        session=session,
        now=now,
        owner=owner,
        owner_started=owner_started,
        chat_pid=chat_pid,
        path=path,
        pane=pane,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
