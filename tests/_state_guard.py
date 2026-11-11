"""Keep the tests off the real herdr-kiro state dir and hook log.

The real paths are fixed when this module is first imported, before any test
module points XDG_STATE_HOME and HERDR_KIRO_LOG at a temp dir. They are built
by hand: hook.state_dir() would create the real dir.
"""

import os
import shutil
import tempfile

KEYS = ("XDG_STATE_HOME", "HERDR_KIRO_LOG")

_bases = [os.path.join(os.path.expanduser("~"), ".local", "state")]
if os.environ.get("XDG_STATE_HOME"):
    _bases.append(os.environ["XDG_STATE_HOME"])
REAL_DIRS = tuple(sorted({os.path.join(base, "herdr-kiro") for base in _bases}))
_logs = {os.path.join(path, "hook.log") for path in REAL_DIRS}
if os.environ.get("HERDR_KIRO_LOG"):
    _logs.add(os.environ["HERDR_KIRO_LOG"])
REAL_LOGS = tuple(sorted(_logs))


def _stat(path):
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return (False, None, None)
    return (True, info.st_mtime_ns, info.st_size)


def _files(root):
    found = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def snapshot():
    logs = {path: _stat(path) for path in REAL_LOGS}
    dirs = {path: (os.path.isdir(path), _files(path)) for path in REAL_DIRS}
    return logs, dirs


def _tail(path, size):
    try:
        with open(path, "rb") as handle:
            handle.seek(size or 0)
            return handle.read()[-600:]
    except OSError:
        return b""


def changes(before, after):
    lines = []
    for path, was in before[0].items():
        now = after[0][path]
        if now != was:
            lines.append(f"{path}: (exists, mtime_ns, size) {was} -> {now}; new bytes {_tail(path, was[2])!r}")
    for path, (was_dir, was_files) in before[1].items():
        now_dir, now_files = after[1][path]
        if now_dir != was_dir:
            lines.append(f"{path}: dir exists {was_dir} -> {now_dir}")
        if now_files != was_files:
            added = sorted(now_files - was_files)
            removed = sorted(was_files - now_files)
            lines.append(f"{path}: files added {added} removed {removed}")
    return lines


class Isolation:
    """Point the hook at a temp state dir and log for one test module."""

    def __init__(self, module):
        self.module = module
        self.saved = {}
        self.root = ""
        self.before = None

    def start(self):
        self.before = snapshot()
        self.saved = {key: os.environ.get(key) for key in KEYS}
        self.root = tempfile.mkdtemp(prefix="herdr-kiro-tests-")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.root, "state")
        os.environ["HERDR_KIRO_LOG"] = os.path.join(self.root, "hook.log")

    def stop(self):
        try:
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        finally:
            if self.root:
                shutil.rmtree(self.root, ignore_errors=True)
        lines = changes(self.before, snapshot())
        if lines:
            raise AssertionError(f"{self.module} touched the real herdr-kiro state:\n" + "\n".join(lines))
