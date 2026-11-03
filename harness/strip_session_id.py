#!/usr/bin/env python3
"""Test wrapper. Strips session_id, then execs the real hook as a child.

Kiro spawns this process. The hook's parent is this wrapper, a new pid every
event. That is the live failure mode for a parent-pid session key.
"""

import json
import os
import sys

HOOK = os.environ["HERDR_KIRO_REAL_HOOK"]
EVENT = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        payload.pop("session_id", None)
        payload.pop("sessionId", None)
        data = json.dumps(payload).encode("utf-8")
    else:
        data = raw
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.dup2(read_fd, 0)
        os.close(write_fd)
        os.execv(sys.executable, [sys.executable, HOOK, EVENT])
    os.close(read_fd)
    os.write(write_fd, data)
    os.close(write_fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
