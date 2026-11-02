#!/usr/bin/env python3
"""Live herdr board for an asciinema capture.

Every status line and pane line is read from herdr at that moment.
Nothing is retyped from memory.
"""
import json
import os
import subprocess
import sys
import time

SESSION = os.environ.get("HERDR_SESSION", "kiro-repro")
PANE = os.environ.get("HERDR_PANE", "w1:p1")
SECRET = os.environ.get("KIRO_API_KEY", "")


def scrub(text: str) -> str:
    """Replace KIRO_API_KEY with a placeholder. Reads the environment each call."""
    secret = os.environ.get("KIRO_API_KEY", "") or SECRET
    if secret and text and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def herdr(*args, timeout=20):
    return subprocess.run(
        ["herdr", "--session", SESSION, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def agent_status():
    completed = herdr("agent", "get", PANE, timeout=10)
    raw = scrub(completed.stdout or "")
    try:
        agent = json.loads(raw)["result"]["agent"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return "unknown", raw[-180:]
    return agent.get("agent_status") or "unknown", ""


def pane_text():
    completed = herdr("pane", "read", PANE, "--source", "visible", "--lines", "18", timeout=10)
    return scrub(completed.stdout or "")


def banner_of(text):
    for line in text.splitlines():
        lowered = line.lower()
        if "running" in lowered and ("▶" in line or "ctrl+g" in lowered):
            return line.strip()
    return ""


def tail_lines(text, count=8):
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-count:]


def paint(phase, clock, elapsed, status, banner, tail, note):
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(f"  {phase}\n")
    sys.stdout.write(f"  clock {clock}    elapsed {elapsed:6.1f}s\n")
    sys.stdout.write("\n")
    label = status.upper()
    if status == "working":
        color = "\033[30;42m"
    elif status == "blocked":
        color = "\033[30;43m"
    elif status == "idle":
        color = "\033[37;41m"
    else:
        color = "\033[37;44m"
    sys.stdout.write(f"  herdr  {color}  {label:10}  \033[0m\n")
    sys.stdout.write("\n")
    sys.stdout.write(f"  banner  {banner or '(no workflow banner)'}\n")
    sys.stdout.write(f"  {note}\n")
    sys.stdout.write("  " + "-" * 88 + "\n")
    sys.stdout.write("  pane read, last lines, unedited\n")
    for line in tail:
        sys.stdout.write("  | " + line[:96] + "\n")
    sys.stdout.write("\n")
    sys.stdout.flush()


def title_card(text, seconds):
    end = time.time() + seconds
    while time.time() < end:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write("\n\n\n")
        sys.stdout.write(f"          {text}\n")
        sys.stdout.write(f"\n          clock {time.strftime('%H:%M:%S')}\n")
        sys.stdout.flush()
        time.sleep(0.4)


def run(phase, prompt, out_path, timeout):
    title_card(phase, 2.5)
    herdr("agent", "prompt", PANE, prompt, timeout=20)
    started = time.time()
    rows = []
    saw_banner = False
    idle_hits = 0
    deadline = started + timeout
    while time.time() < deadline:
        moment = time.time()
        status, err = agent_status()
        text = pane_text()
        banner = banner_of(text)
        tail = tail_lines(text)
        if banner:
            saw_banner = True
        note = err or ("workflow banner is on screen" if banner else "no banner")
        paint(
            phase,
            time.strftime("%H:%M:%S"),
            moment - started,
            status,
            banner,
            tail,
            note,
        )
        rows.append(
            {
                "t": round(moment, 3),
                "elapsed": round(moment - started, 3),
                "agent_status": status,
                "banner": banner,
            }
        )
        if saw_banner and not banner and status == "idle":
            idle_hits += 1
            if idle_hits >= 8:
                break
        else:
            idle_hits = 0
        time.sleep(0.4)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    title_card(phase + "  ended", 1.5)


def main():
    phase = sys.argv[1]
    out_path = sys.argv[2]
    kind = sys.argv[3] if len(sys.argv) > 3 else "sequential"
    prompts = {
        "sequential": "/workflow run two-step",
        "parallel": "/workflow run parallel-steps",
    }
    run(phase, prompts[kind], out_path, 240)


if __name__ == "__main__":
    main()
