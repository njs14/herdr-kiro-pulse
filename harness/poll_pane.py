#!/usr/bin/env python3
"""Poll herdr agent status and a few screen lines until a deadline."""

import json
import subprocess
import sys
import time


def main() -> int:
    session = sys.argv[1]
    pane = sys.argv[2]
    out_path = sys.argv[3]
    seconds = float(sys.argv[4])
    herdr = ["herdr", "--session", session]
    deadline = time.time() + seconds
    with open(out_path, "w") as out:
        while time.time() < deadline:
            t = time.time()
            try:
                raw = subprocess.check_output([*herdr, "agent", "get", pane], text=True, timeout=5)
                agent = json.loads(raw)["result"]["agent"]
                screen = subprocess.check_output(
                    [*herdr, "pane", "read", pane, "--source", "visible", "--lines", "18"],
                    text=True,
                    timeout=5,
                )
            except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as exc:
                out.write(json.dumps({"t": round(t, 3), "error": str(exc)}) + "\n")
                out.flush()
                time.sleep(0.2)
                continue
            interesting = []
            for line in screen.splitlines():
                text = line.strip()
                if not text:
                    continue
                lowered = text.lower()
                if (
                    "running" in lowered
                    or "working" in lowered
                    or text.startswith("▶")
                    or text.startswith("›")
                    or "ask a question" in lowered
                    or "approval" in lowered
                    or "trust" in lowered
                    or "workflow" in lowered
                    or "allow" in lowered
                    or "deny" in lowered
                ):
                    interesting.append(text[:180])
            row = {
                "t": round(t, 3),
                "agent_status": agent.get("agent_status"),
                "seq": agent.get("state_change_seq"),
                "interesting": interesting[-6:],
            }
            out.write(json.dumps(row) + "\n")
            out.flush()
            time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
