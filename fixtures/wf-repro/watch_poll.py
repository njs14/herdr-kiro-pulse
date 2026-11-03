#!/usr/bin/env python3
"""Watch handler. First poll is idle. The next poll is terminal."""

import json
import sys


def main() -> int:
    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    if not cursor:
        json.dump({"outcome": "idle", "cursor": {"n": 1}}, sys.stdout)
        return 0
    json.dump(
        {"outcome": "terminal-state", "cursor": {"n": 2}, "payload": '{"done":true}'},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
