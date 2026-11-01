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

import atexit
import datetime
import errno
import fcntl
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

__version__ = "0.11.0"
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
# One agent_not_found reading is not enough to end a live run.
GONE_CONFIRM_SECS = 1.5
GONE_CONFIRM_HITS = 3
# Releasing the hook the instant the prompt returns makes herdr report unknown
# for about 3 seconds. Report idle first and release only after this delay, so
# a run can settle idle before that hand-off.
RELEASE_DELAY = 2.0
TAIL_LINES = 30
BUSY_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
# Stop fires after a turn. SessionEnd fires when that loaded session is torn down.
END_EVENTS = {"Stop", "SessionEnd"}
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
        "generation": 0,
        "retiring": False,
        "owners_died": False,
        "chat_pid": 0,
        "chat_started": "",
    }


def normalize(state: dict, now: float) -> dict:
    busy = state.get("busy") if isinstance(state.get("busy"), dict) else {}
    owners = state.get("owners") if isinstance(state.get("owners"), dict) else {}
    owner_started = state.get("owner_started") if isinstance(state.get("owner_started"), dict) else {}
    kept = {}
    kept_owners = {}
    kept_started = {}
    dropped_dead = False
    for session, stamp in busy.items():
        try:
            seen = float(stamp)
        except (TypeError, ValueError):
            continue
        key = str(session)
        if owner_is_dead(owners.get(key), owner_started.get(key)):
            dropped_dead = True
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
    # The second normalize in update() sees an already-empty busy map, so a
    # death noticed on the first pass would otherwise disappear. Keep the
    # flag until a live owner is stored again.
    owners_died = (dropped_dead and not kept_owners) or (
        bool(state.get("owners_died")) and not kept and not kept_owners
    )
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
        "generation": int(state.get("generation") or 0),
        "retiring": bool(state.get("retiring")),
        "owners_died": owners_died,
        "chat_pid": int(state.get("chat_pid") or 0),
        "chat_started": state.get("chat_started") or "",
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
    if started:
        state["owner_started"][key] = started


def finish_if_idle(state: dict, now: float) -> None:
    if state["busy"]:
        return
    state["grace_until"] = now + SETTLE_SECS
    state["grace_started"] = now


def apply_event(
    state: dict,
    event: str,
    session: str,
    now: float,
    owner: int = 0,
    owner_started: str = "",
    chat_pid: int = 0,
    chat_started: str = "",
) -> dict:
    """Fold one hook event into pane state. Pure aside from the clock passed in."""
    nxt = normalize(state, now)
    if chat_pid:
        nxt["chat_pid"] = int(chat_pid)
        nxt["chat_started"] = chat_started or ""
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
            for member in members_of(nxt["busy"], session):
                nxt["busy"][member] = now
        nxt["grace_until"] = 0.0
    elif event in END_EVENTS:
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
    return note_generation(state, nxt)


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
    )


def pane_text(pane: str) -> str:
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
IDLE_PROMPT = re.compile(r"(?i)ask a question or describe a task")
RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiro-detection.toml")
_EXPLAIN_ENV = None
_RULES_DIR = None
_SCREEN_CACHE = {}


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
    if ("uid %s" % os.getuid()) not in lines:
        return None
    return int(matched.group(1))


def sweep_stale_rules() -> None:
    """Remove rules dirs this package created after their process is gone.

    A directory is removed only when its name is herdr-kiro-rules-<pid>-<token>,
    it contains a herdr-kiro-owned marker for this uid, and the directory is
    owned by this uid. EPERM on the pid means the process is alive or foreign.
    """
    for root in temp_roots():
        for path in glob.glob(os.path.join(root, "herdr-kiro-rules-*")):
            pid = _rules_dir_ours(path)
            if pid is None or pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    continue
                shutil.rmtree(path, True)


def sweep_stale_pidfiles() -> None:
    """Drop pid files whose process is gone or whose start time moved."""
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
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        recorded = lines[1].strip() if len(lines) > 1 else ""
        if not recorded:
            continue
        snap = process_snapshot(pid)
        if not snap or snap.get("error"):
            continue
        if snap.get("lstart") and snap.get("lstart") != recorded:
            try:
                os.remove(path)
            except OSError:
                pass


def cleanup_rules() -> None:
    if _RULES_DIR and os.path.isdir(_RULES_DIR):
        shutil.rmtree(_RULES_DIR, True)
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
    base = tempfile.mkdtemp(prefix="herdr-kiro-rules-%s-" % os.getpid())
    _RULES_DIR = base
    marker = os.path.join(base, "herdr-kiro-owned")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("uid %s\n" % os.getuid())
    try:
        os.chmod(marker, FILE_MODE)
    except OSError:
        pass
    atexit.register(shutil.rmtree, base, True)
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
        fd, path = tempfile.mkstemp(prefix="herdr-kiro-screen-", suffix=".txt")
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
            try:
                os.remove(path)
            except OSError:
                pass
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


def highest_rule(text: str):
    view = screen_view(text)
    if not view.get("id"):
        return None
    return {"id": view["id"], "state": view["state"]}


def blocker_visible(text: str) -> bool:
    """True when herdr's winning rule for this screen is blocked."""
    if approval_text(text):
        return True
    return screen_view(text).get("state") == "blocked"


def banner_visible(text: str) -> bool:
    return screen_view(text).get("id") == "workflow_banner_working"


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


def prompt_visible(text: str) -> bool:
    """Kiro's idle prompt, or a shell prompt. Model text is neither."""
    tail = tail_text(text)
    if IDLE_PROMPT.search(tail or ""):
        return True
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        if re.search(r"[$#%]\s*$", stripped) and ("/" in stripped or ":" in stripped or stripped.endswith("$")):
            return True
    return False


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


def release_if_current(state: dict, generation: int, keys) -> bool:
    """Clear only the snapshotted slots when no newer hook event has landed."""
    if int(state.get("generation") or 0) != int(generation):
        state["retiring"] = False
        return False
    for key in keys:
        state["busy"].pop(key, None)
        state["owners"].pop(key, None)
        state["owner_started"].pop(key, None)
    if AGGREGATE not in state["busy"]:
        state["aggregate"] = 0
    if not state["busy"]:
        state["grace_until"] = 0.0
        state["grace_started"] = 0.0
    state["retiring"] = True
    return True


def _pids_exited(pids) -> bool:
    """True when every pid is gone. EPERM means alive or foreign, so not exited."""
    saw = False
    for pid in pids:
        try:
            number = int(pid)
        except (TypeError, ValueError):
            continue
        if number <= 1:
            continue
        saw = True
        try:
            os.kill(number, 0)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                return False
        else:
            return False
    return saw


def tracked_exit(state: dict, chat_hits: int):
    """How a tracked Kiro process ended.

    'outer' when the recorded kiro-cli pid is gone. 'chat' when the recorded
    kiro-cli-chat pid is gone and no replacement is under a living outer, for
    several ticks. A live pid is not an exit, including when its parent is init.
    """
    owners = state.get("owners") or {}
    started = state.get("owner_started") or {}
    outer_alive = False
    saw_outer = False
    for key, owner in owners.items():
        try:
            pid = int(owner)
        except (TypeError, ValueError):
            continue
        if pid <= 1:
            continue
        saw_outer = True
        if alive(pid, started.get(key) or ""):
            outer_alive = True
    if saw_outer and not outer_alive:
        return "outer", 0
    chat = int(state.get("chat_pid") or 0)
    if not chat:
        return None, 0
    if alive(chat, state.get("chat_started") or ""):
        return None, 0
    if outer_alive:
        for key, owner in owners.items():
            try:
                pid = int(owner)
            except (TypeError, ValueError):
                continue
            if alive(pid, started.get(key) or "") and chat_under(pid):
                return None, 0
    hits = chat_hits + 1
    if hits >= GONE_CONFIRM_HITS:
        return "chat", hits
    return None, hits


def should_stop_for_dead_owner(state: dict, pane_kiro: bool) -> bool:
    """True after normalize recorded that every owned kiro process is gone."""
    return bool(state.get("owners_died")) and not pane_kiro


def gone_confirmed(since, now: float, hits: int, owners_died: bool) -> bool:
    if owners_died:
        return True
    if since is None:
        return False
    return hits >= GONE_CONFIRM_HITS and now - since >= GONE_CONFIRM_SECS


def workflow_banner(pane: str) -> bool:
    return workflow_banner_from(pane_text(pane))


_TABLE = None
_PS_LINE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\S+\s+\S+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.*?)\s*$"
)


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


def process_snapshot(pid: int) -> dict:
    """Return ppid, lstart, and comm basename. None if the pid is gone.

    One ps fills the table for the tick. Later lookups do not call ps again.
    """
    if pid <= 0:
        return None
    table = process_table()
    if table == "error":
        return {"error": True}
    return table.get(int(pid))


def owner_is_dead(owner, started: str) -> bool:
    """True when this pid is gone. A live process is never dead because of its parent."""
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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


def kiro_in_pane(owner_ids) -> bool:
    """True when a kiro process is still under one of this pane's owners."""
    table = process_table()
    if table == "error":
        return True
    owners = set()
    for owner in owner_ids:
        try:
            owners.add(int(owner))
        except (TypeError, ValueError):
            continue
    for pid, row in table.items():
        if "kiro" not in command_name(row.get("comm") or ""):
            continue
        cursor = pid
        seen = set()
        while cursor > 1 and cursor not in seen:
            seen.add(cursor)
            if cursor in owners:
                return True
            parent = table.get(cursor, {}).get("ppid") or 0
            if parent in owners:
                return True
            cursor = parent
    return False


def owners_status(state: dict) -> str:
    owners = state.get("owners") or {}
    if not owners:
        return "none"
    started = state.get("owner_started") or {}
    for key, owner in owners.items():
        if not owner_is_dead(owner, started.get(key) or ""):
            return "alive"
    return "dead"


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
    outer_pid, outer_started, _chat_pid, _chat_started = kiro_lineage()
    if outer_pid:
        return outer_pid, outer_started
    return _chat_pid, _chat_started


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


def chat_under(outer_pid: int):
    """A kiro-cli-chat whose parent chain reaches this outer kiro-cli."""
    if not outer_pid:
        return None
    table = process_table()
    if table == "error":
        return None
    for pid, row in table.items():
        if command_name(row.get("comm") or "") != "kiro-cli-chat":
            continue
        cursor = pid
        seen = set()
        while cursor > 1 and cursor not in seen:
            seen.add(cursor)
            if cursor == int(outer_pid):
                return pid
            parent = table.get(cursor, {}).get("ppid") or 0
            try:
                cursor = int(parent)
            except (TypeError, ValueError):
                break
    return None


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
        retiring = bool(state.get("retiring"))
        if claimed and retiring:
            return
        if not retiring and (claimed or alive(pid, state.get("watcher_started") or "")):
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
    begin_tick()
    snap = process_snapshot(proc.pid)
    if not snap or snap.get("error"):
        time.sleep(0.05)
        begin_tick()
        snap = process_snapshot(proc.pid)
    if snap and not snap.get("error"):
        started = snap.get("lstart") or ""

    def record(state: dict) -> None:
        state["watcher"] = proc.pid
        if started:
            state["watcher_started"] = started
        state["retiring"] = False

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


def finish_if_unchanged(pane: str, path: str, state: dict, reason: str) -> bool:
    """Release only while this process still owns the watcher slot."""
    me = os.getpid()
    generation = int(state.get("generation") or 0)
    keys = list(state.get("busy") or {})
    decision = {"ok": False}

    def apply(current: dict) -> None:
        watcher = int(current.get("watcher") or 0)
        if watcher not in (me, 0, -1):
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
        if watcher not in (me, 0, -1) or int(current.get("generation") or 0) != generation:
            current["retiring"] = False
            return
        if reason == "idle" and current.get("busy"):
            current["retiring"] = False
            return
        still["ok"] = True

    update(path, confirm)
    if not still["ok"]:
        return False
    release_authority(pane, path)
    clear_watcher(path)
    log("watcher exit " + reason)
    return True


def note_report(pane: str, path: str, kind: str, reported, misses: int):
    ok = send_report(pane, path, kind, release=False)
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
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        if self._kq is not None:
            try:
                self._kq.close()
            except OSError:
                pass
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
                pass
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
                raise
        poller = select.poll()
        for fd in self._fds.values():
            poller.register(fd, select.POLLIN)
        return bool(poller.poll(max(0, int(timeout * 1000))))

    def _wait_kqueue(self, pids, timeout: float) -> bool:
        if self._kq is None:
            self._kq = select.kqueue()
        changes = []
        for pid in pids:
            try:
                changes.append(
                    select.kevent(
                        pid,
                        select.KQ_FILTER_PROC,
                        select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                        select.KQ_NOTE_EXIT,
                    )
                )
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return True
                raise
        try:
            events = self._kq.control(changes, 1, timeout)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return True
            raise
        return bool(events)

    def _wait_kill(self, pids, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            for pid in pids:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


def watch_loop(pane: str, path: str) -> None:
    try:
        _watch_loop(pane, path)
    except Exception as exc:
        log("watcher error %s: %s" % (type(exc).__name__, exc))
        clear_watcher(path)


def _watch_loop(pane: str, path: str) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _on_term)
    begin_tick()
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
    held = None
    owner_watch = OwnerExitWatch()
    # SessionEnd drops the owner from the state file before the process exits.
    # Keep the last pid so that exit still wakes the watcher.
    known_owners = []
    known_outers = []
    chat_hits = 0
    gone_since = None
    gone_hits = 0
    try:
        while True:
            owner_watch.wait(known_owners, TICK_SECS)
            begin_tick()
            looked = agent_lookup(pane)
            state = update(path, lambda _state: None)
            now = time.time()
            current_owners = []
            for owner in (state.get("owners") or {}).values():
                try:
                    current_owners.append(int(owner))
                except (TypeError, ValueError):
                    continue
            chat_pid = int(state.get("chat_pid") or 0)
            if chat_pid:
                current_owners.append(chat_pid)
            outers_now = []
            for owner in (state.get("owners") or {}).values():
                try:
                    outers_now.append(int(owner))
                except (TypeError, ValueError):
                    continue
            if outers_now:
                known_outers = outers_now
            if current_owners:
                known_owners = current_owners
            kind, chat_hits = tracked_exit(state, chat_hits)
            owner_alive = False
            started_by_pid = {}
            for key, owner in (state.get("owners") or {}).items():
                try:
                    started_by_pid[int(owner)] = (state.get("owner_started") or {}).get(key) or ""
                except (TypeError, ValueError):
                    continue
            for owner in (state.get("owners") or {}).values():
                try:
                    pid = int(owner)
                except (TypeError, ValueError):
                    continue
                if alive(pid, started_by_pid.get(pid) or ""):
                    owner_alive = True
                    break
            if not owner_alive and chat_pid and alive(chat_pid, state.get("chat_started") or ""):
                owner_alive = True
            pane_kiro = kiro_in_pane([pid for pid in known_owners])
            owner_dead = kind is not None or should_stop_for_dead_owner(state, pane_kiro)
            if not known_owners and looked == "gone":
                if gone_since is None:
                    gone_since = now
                gone_hits += 1
                if gone_confirmed(gone_since, now, gone_hits, False):
                    if finish_if_unchanged(pane, path, state, "agent gone"):
                        return
            else:
                gone_since = None
                gone_hits = 0
            if owner_dead or kind is not None or _pids_exited(known_outers):
                if not finish_if_unchanged(pane, path, state, "owner exit"):
                    log("watcher exit slot held")
                return
            screen = pane_text(pane)
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
                    revised = note_banner(current, True, time.time())
                    current["grace_until"] = revised["grace_until"]

                state = update(path, extend)
            elif prompt_visible(screen) and screen_is_settled(screen):
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= IDLE_CLEAR_SECS and state["busy"]:
                    generation = int(state.get("generation") or 0)
                    keys = list(state["busy"])
                    decision = {"ok": False}

                    def apply_clear(current: dict) -> None:
                        decision["ok"] = release_if_current(current, generation, keys)

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
                reported, misses = note_report(pane, path, "working", reported, misses)
                if reported is None and misses >= 30:
                    return
                continue
            if action == "idle" and screen_is_quiet(screen):
                if reported != "idle":
                    reported, misses = note_report(pane, path, "idle", reported, misses)
                    if reported is None and misses >= 30:
                        return
                    log("watcher idle")
                held = quiet_since is not None and now - quiet_since >= RELEASE_DELAY
                if reported == "idle" and not state["busy"] and held:
                    if finish_if_unchanged(pane, path, state, "idle"):
                        return
                continue
            if action == reported:
                continue
            if action != "working" or state.get("owners_died") or owners_gone:
                continue
            reported, misses = note_report(pane, path, "working", reported, misses)
            if reported is None and misses >= 30:
                return
    finally:
        owner_watch.close()


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
    if event not in BUSY_EVENTS and event not in END_EVENTS:
        return 0
    begin_tick()
    owner, owner_started, chat_pid, chat_started = kiro_lineage()
    log(f"event={event} session={session} pane={pane} chain={process_chain()}")

    def fold(state: dict) -> None:
        revised = apply_event(
            state, event, session, now, owner, owner_started, chat_pid, chat_started
        )
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
