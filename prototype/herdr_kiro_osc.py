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

# The hook writes app=kiro-cli. An incoming status whose app is kiro is still this program.
APP = "kiro-cli"
KIRO_APPS = frozenset({"kiro", "kiro-cli"})
_STATES = {"idle", "working", "done", "blocked", "error", "clear"}
_KINDS = {"permission", "question", "auth"}
_QUESTION_RULES = {"question_panel", "workflow_question_esc_cancel"}
_TTY = re.compile(r"^/dev/(pts/[0-9]+|ttys[0-9]+)$")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_FLAG_SETS = (
    os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | _CLOEXEC,
    os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | _CLOEXEC,
)


def is_kiro_app(name: object) -> bool:
    """True when an incoming status app is kiro-cli or kiro."""
    return isinstance(name, str) and name in KIRO_APPS


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
    """Write payload to pid's stdout tty. Return that device, or ''.

    One candidate. A missing fd lookup, an ambiguous fd 1, or a stdout that
    is not a tty writes nothing. The controlling terminal and /dev/tty are
    not candidates, and a failed open does not try another device.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return ""
    if number <= 1:
        return ""
    found = list(_tty_opens(number))
    if len(found) != 1:
        return ""
    open_path, device = found[0]
    if _write_path(open_path, device, payload):
        return device
    return ""


def _tty_opens(pid: int):
    """Yield the stdout tty once, or nothing.

    `/proc/<pid>/fd/1` when that directory exists. Elsewhere one lsof path
    for fd 1. stderr, stdin, `ps -o tty=`, and /dev/tty are not yields.
    """
    proc = f"/proc/{pid}/fd"
    if os.path.isdir(proc):
        link = os.path.join(proc, "1")
        try:
            target = os.readlink(link)
        except OSError:
            return
        if _TTY.fullmatch(target):
            yield link, target
        return
    if _ambiguous_stdout(_lsof_text(pid)):
        return
    stdout = _fd_devices(pid).get("1") or ""
    if _TTY.fullmatch(stdout):
        yield stdout, stdout


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


def _ambiguous_stdout(text: str) -> bool:
    """True when fd 1 is named more than once as different paths."""
    paths: list[str] = []
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
        elif line.startswith("n") and number == "1":
            paths.append(line[1:].strip())
            number = ""
    unique: list[str] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return len(unique) > 1


def _lsof_bin() -> str:
    found = shutil.which("lsof") or ""
    if found:
        return found
    if os.access("/usr/sbin/lsof", os.X_OK):
        return "/usr/sbin/lsof"
    return ""


def _lsof_text(pid: int) -> str:
    binary = _lsof_bin()
    if not binary:
        return ""
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
        return ""
    return completed.stdout or ""


def _fd_devices(pid: int) -> dict[str, str]:
    return _devices_from_lsof(_lsof_text(pid))


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
