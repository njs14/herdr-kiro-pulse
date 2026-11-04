"""OSC 7501 reports for the Herdr App.

herdr-core 0.1.17 keeps a terminal's status only from OSC 7501 bytes on that
terminal's output. `hc` can show the status and can paste input. It has no
command that sets it. A Kiro hook often runs with a captured stdout and, for
the watcher, in its own session, so the bytes go to the pane process's tty.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess

APP = "kiro"
_STATES = {"idle", "working", "done", "blocked", "error", "clear"}
_KINDS = {"permission", "question", "auth"}
_QUESTION_RULES = {"question_panel", "workflow_question_esc_cancel"}
_TTY = re.compile(r"^/dev/(pts/[0-9]+|ttys[0-9]+)$")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_FLAG_SETS = (
    os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | _CLOEXEC,
    os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | _CLOEXEC,
)


def fields_for(kind: str, rule_id: str = "") -> dict | None:
    """Map a hook report onto one OSC 7501 root record.

    `idle` is the TUI word for a finished turn. The App word for that finish
    is `done`, and it stays `done` after the pane is seen. The Agents
    sidebar keeps a focused row under Done until that row is left. Sending
    `state=idle` leaves that hold in place, so this function never returns it.
    `unknown` is not a protocol state, so it is not sent.
    """
    if kind == "working":
        return {"state": "working", "app": APP}
    if kind == "blocked":
        block = "question" if rule_id in _QUESTION_RULES else "permission"
        return {"state": "blocked", "kind": block, "app": APP}
    if kind == "idle":
        return {"state": "done", "app": APP}
    return None


def encode(fields: dict | None) -> bytes:
    """One OSC 7501 report: ESC ] 7501 ; key=value : ... ESC \\."""
    if not fields:
        raise ValueError("fields")
    state = str(fields.get("state") or "")
    if state not in _STATES:
        raise ValueError("state")
    pairs = ["state=" + state]
    kind = str(fields.get("kind") or "")
    if kind:
        if kind not in _KINDS:
            raise ValueError("kind")
        pairs.append("kind=" + kind)
    app = str(fields.get("app") or "")
    if app:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", app):
            raise ValueError("app")
        pairs.append("app=" + app)
    return b"\x1b]7501;" + ":".join(pairs).encode("ascii") + b"\x1b\\"


def write_pid(payload: bytes, pid: int) -> str:
    """Write payload to pid's tty, stdout first. Return the device, or ''."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return ""
    if number <= 1:
        return ""
    for open_path, device in _tty_opens(number):
        if _write_path(open_path, device, payload):
            return device
    return ""


def write_to_pane(payload: bytes, pids: list[int], *, skip: list[int] | None = None) -> str:
    """Write payload to the first pane tty among pids, this process, and its parents.

    `skip` holds recorded pids that a start token does not identify. An empty
    token matches any live pid, and the spawning hook is still this process's
    parent on the first report, so the parent walk would otherwise write that
    ancestor's tty. Those pids are left out of both lists.
    """
    blocked: set[int] = set()
    for pid in skip or []:
        try:
            number = int(pid)
        except (TypeError, ValueError):
            continue
        if number > 1:
            blocked.add(number)
    seen: set[int] = set()
    ordered: list[int] = []
    for pid in list(pids) + _self_chain():
        try:
            number = int(pid)
        except (TypeError, ValueError):
            continue
        if number <= 1 or number in seen or number in blocked:
            continue
        seen.add(number)
        ordered.append(number)
    for number in ordered:
        found = write_pid(payload, number)
        if found:
            return found
    return _write_controlling(payload)


def _self_chain() -> list[int]:
    chain = [os.getpid()]
    pid = os.getpid()
    for _ in range(16):
        parent = _ppid(pid)
        if parent <= 1 or parent in chain:
            break
        chain.append(parent)
        pid = parent
    return chain


def _ppid(pid: int) -> int:
    text = ""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = ""
    if text:
        end = text.rfind(")")
        fields = text[end + 2 :].split() if end >= 0 else []
        if len(fields) >= 2:
            try:
                return int(fields[1])
            except ValueError:
                return 0
    return _ps_int(pid, "ppid")


def _ps_int(pid: int, field: str) -> int:
    try:
        return int(_ps_field(pid, field))
    except ValueError:
        return 0


def _ps_field(pid: int, field: str) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-o", field + "=", "-p", str(int(pid))],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _tty_opens(pid: int):
    """Stdout, then stderr, then stdin.

    Linux uses `/proc/<pid>/fd`. Elsewhere `lsof` names those same fds.
    `ps -o tty=` is only the controlling terminal, and a setsid hook has none.
    """
    proc = f"/proc/{pid}/fd"
    if os.path.isdir(proc):
        for name in ("1", "2", "0"):
            link = os.path.join(proc, name)
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if _TTY.fullmatch(target):
                yield link, target
        return
    seen: set[str] = set()
    devices = _fd_devices(pid)
    for name in ("1", "2", "0"):
        device = devices.get(name) or ""
        if _TTY.fullmatch(device) and device not in seen:
            seen.add(device)
            yield device, device
    controlling = _tty_from_ps(pid)
    if controlling and controlling not in seen:
        yield controlling, controlling


def _devices_from_lsof(text: str) -> dict[str, str]:
    """Map fd number to path from `lsof -F fn` text. Stdout is field `f1`."""
    found: dict[str, str] = {}
    number = ""
    for line in text.splitlines():
        if line.startswith("f"):
            number = ""
            digits: list[str] = []
            for ch in line[1:]:
                if not ch.isdigit():
                    break
                digits.append(ch)
            number = "".join(digits)
        elif line.startswith("n") and number and number not in found:
            found[number] = line[1:].strip()
            number = ""
    return found


def _lsof_bin() -> str:
    found = shutil.which("lsof") or ""
    if found:
        return found
    if os.access("/usr/sbin/lsof", os.X_OK):
        return "/usr/sbin/lsof"
    return ""


def _fd_devices(pid: int) -> dict[str, str]:
    binary = _lsof_bin()
    if not binary:
        return {}
    try:
        completed = subprocess.run(
            [binary, "-n", "-P", "-a", "-p", str(int(pid)), "-d", "0,1,2", "-F", "fn"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return _devices_from_lsof(completed.stdout or "")


def _tty_from_ps(pid: int) -> str:
    token = _ps_field(pid, "tty")
    if token in ("", "?", "??", "-"):
        return ""
    path = token if token.startswith("/dev/") else "/dev/" + token
    if _TTY.fullmatch(path):
        return path
    return ""


def _write_path(open_path: str, device: str, payload: bytes) -> bool:
    if not _TTY.fullmatch(device):
        return False
    try:
        expected = os.stat(device)
    except OSError:
        return False
    if not stat.S_ISCHR(expected.st_mode):
        return False
    for flags in _FLAG_SETS:
        try:
            fd = os.open(open_path, flags)
        except OSError:
            continue
        ok = False
        try:
            info = os.fstat(fd)
            ok = stat.S_ISCHR(info.st_mode) and info.st_rdev == expected.st_rdev
            if ok:
                _write_all(fd, payload)
        except OSError:
            continue
        else:
            if ok:
                return True
        finally:
            os.close(fd)
    return False


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_controlling(payload: bytes) -> str:
    for flags in _FLAG_SETS:
        try:
            fd = os.open("/dev/tty", flags)
        except OSError:
            continue
        device = ""
        try:
            info = os.fstat(fd)
            if stat.S_ISCHR(info.st_mode):
                device = os.ttyname(fd)
            if not _TTY.fullmatch(device):
                device = ""
            elif device:
                _write_all(fd, payload)
        except OSError:
            continue
        else:
            if device:
                return device
        finally:
            os.close(fd)
    return ""
