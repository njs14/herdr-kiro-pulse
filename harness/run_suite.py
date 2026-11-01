#!/usr/bin/env python3
"""Run Kiro V3 workflows in a herdr pane and assert the pane stays working."""
import json
import os
import subprocess
import sys
import time

SESSION = os.environ.get("HERDR_SESSION", "kiro-repro")
PANE = os.environ.get("HERDR_PANE", "w1:p1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
HOOK_LOG = os.environ.get("HERDR_KIRO_LOG", "/tmp/herdr-kiro-hook.log")
POLL_SECS = 0.4


def herdr(*args, timeout=30):
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
    payload = json.loads(completed.stdout)
    agent = payload["result"]["agent"]
    return agent.get("agent_status"), agent.get("state_change_seq")


def visible_text():
    completed = herdr("pane", "read", PANE, "--source", "visible", "--lines", "30", timeout=10)
    return completed.stdout or ""


def banner_of(text):
    for line in text.splitlines():
        lowered = line.lower()
        if "running" in lowered and ("▶" in line or "ctrl+g" in lowered):
            return line.strip()[:160]
    return ""


def interesting_lines(text):
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            "running" in lowered
            or "working" in lowered
            or stripped.startswith("›")
            or stripped.startswith(">")
            or "approval" in lowered
            or "trust" in lowered
            or "allow" in lowered
            or "deny" in lowered
            or "yes, i accept" in lowered
        ):
            found.append(stripped[:180])
    return found[-8:]


def accept_trust_dialog():
    text = visible_text()
    if "Yes, I accept" not in text:
        return False
    os.makedirs(LOG_DIR, exist_ok=True)
    status, seq = agent_status()
    with open(os.path.join(LOG_DIR, "trust-dialog.txt"), "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(LOG_DIR, "trust-dialog-status.txt"), "w", encoding="utf-8") as handle:
        handle.write(f"agent_status={status} seq={seq}\n")
        handle.write(
            "The trust-all confirmation is on the parent pane. "
            "herdr does not match it to a Kiro approval rule, so the status above is not blocked.\n"
        )
    herdr("agent", "send-keys", PANE, "down", "enter", timeout=10)
    time.sleep(1.2)
    return True


def max_used_seq():
    best = 0
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    path = os.path.join(base, f"herdr-kiro-{PANE.replace(':', '_')}.json")
    try:
        best = int(json.load(open(path, encoding="utf-8")).get("seq") or 0)
    except (OSError, ValueError, TypeError):
        pass
    try:
        for line in open(HOOK_LOG, encoding="utf-8"):
            marker = "--seq "
            if marker not in line:
                continue
            try:
                best = max(best, int(line.rsplit(marker, 1)[1].split()[0]))
            except ValueError:
                continue
    except OSError:
        pass
    return best


def release_stale_authority():
    seq = max_used_seq() + 1
    herdr(
        "pane",
        "release-agent",
        PANE,
        "--source",
        "hook:kiro",
        "--agent",
        "kiro",
        "--seq",
        str(seq),
        timeout=10,
    )
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    path = os.path.join(base, f"herdr-kiro-{PANE.replace(':', '_')}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "busy": {},
                "seq": seq,
                "grace_until": 0,
                "grace_started": 0,
                "reported": None,
                "watcher": 0,
                "watcher_at": 0,
            },
            handle,
        )


def tail_text(text, count=14):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def agent_present():
    completed = herdr("agent", "get", PANE, timeout=10)
    if completed.returncode != 0 or "agent_not_found" in (completed.stdout or ""):
        return False
    try:
        json.loads(completed.stdout)["result"]["agent"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return False
    return True


def ensure_kiro():
    if not agent_present():
        started = herdr(
            "agent",
            "start",
            "repro",
            "--kind",
            "kiro",
            "--pane",
            PANE,
            "--timeout",
            "120000",
            "--",
            "chat",
            "--v3",
            "--trust-all-tools",
            "--trust-workspace",
            timeout=150,
        )
        if started.returncode != 0:
            raise RuntimeError(started.stdout or started.stderr)
    deadline = time.time() + 20
    while time.time() < deadline:
        if accept_trust_dialog():
            time.sleep(0.8)
        text = tail_text(visible_text())
        if "ask a question" in text and "Yes, I accept" not in text:
            break
        time.sleep(0.4)
    else:
        raise RuntimeError("Kiro did not reach its prompt")
    if not agent_present():
        raise RuntimeError("Kiro did not stay up in the pane")


def prompt(text):
    completed = herdr("agent", "prompt", PANE, text, timeout=20)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)


def poll_run(seconds):
    rows = []
    deadline = time.time() + seconds
    idle_stable = 0
    saw_banner = False
    while time.time() < deadline:
        moment = time.time()
        try:
            status, seq = agent_status()
            text = visible_text()
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as exc:
            rows.append({"t": round(moment, 3), "error": str(exc)})
            time.sleep(POLL_SECS)
            continue
        banner = banner_of(text)
        if banner:
            saw_banner = True
        row = {
            "t": round(moment, 3),
            "agent_status": status,
            "seq": seq,
            "banner": banner,
            "interesting": interesting_lines(text),
        }
        rows.append(row)
        if saw_banner and not banner and status == "idle":
            idle_stable += 1
            if idle_stable >= 6:
                break
        else:
            idle_stable = 0
        time.sleep(POLL_SECS)
    return rows


def stop_times_since(start):
    times = []
    try:
        lines = open(HOOK_LOG, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    for line in lines:
        if "event=Stop" not in line:
            continue
        try:
            stamped = float(line.split()[0])
        except ValueError:
            continue
        if stamped >= start:
            times.append(stamped)
    return times


def assert_run(name, rows, stops):
    banner_rows = [row for row in rows if row.get("banner")]
    if not banner_rows:
        raise AssertionError(f"{name}: workflow banner never appeared")
    working = [row for row in banner_rows if row.get("agent_status") == "working"]
    ratio = len(working) / len(banner_rows)
    if ratio < 0.85:
        raise AssertionError(f"{name}: working for {ratio:.2f} of banner samples")
    streak = 0
    for row in banner_rows:
        if row.get("agent_status") != "working":
            streak += 1
            if streak >= 3:
                raise AssertionError(f"{name}: idle streak while the workflow banner was up")
        else:
            streak = 0
    if not stops:
        raise AssertionError(f"{name}: no Stop hook was logged")
    last_banner = banner_rows[-1]["t"]
    anchor = last_banner
    later_stops = [stamped for stamped in stops if stamped >= last_banner - 1]
    if later_stops:
        anchor = max(later_stops)
    stable = None
    run = 0
    for row in rows:
        if row.get("t", 0) < anchor:
            run = 0
            continue
        if row.get("agent_status") == "idle":
            run += 1
            if run >= 4 and stable is None:
                stable = row["t"]
        else:
            run = 0
    if stable is None:
        raise AssertionError(f"{name}: never settled idle")
    gap = stable - anchor
    if gap < 2.5:
        raise AssertionError(f"{name}: idle {gap:.2f}s after the run, wanted the 3s grace")
    if gap > 10:
        raise AssertionError(f"{name}: idle {gap:.2f}s after the run, grace ran long")
    return {
        "banner_samples": len(banner_rows),
        "working_ratio": round(ratio, 3),
        "idle_gap_s": round(gap, 3),
        "anchor": round(anchor, 3),
    }


def write_log(name, rows):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"poll-{name}.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def run_case(name, text, seconds):
    start = time.time()
    prompt(text)
    rows = poll_run(seconds)
    path = write_log(name, rows)
    stops = stop_times_since(start)
    summary = assert_run(name, rows, stops)
    summary["log"] = path
    summary["stops"] = len(stops)
    return summary


def main() -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    ensure_kiro()
    release_stale_authority()
    cases = [
        ("sequential", "/workflow run two-step", 240),
        ("parallel", "/workflow run parallel-steps", 240),
        ("repeat", "/workflow run repeat-step", 240),
        ("watch", "/workflow run watch-step", 180),
        (
            "auto",
            "Start the saved workflow named solo with the run workflow tool. Do not run the shell command yourself.",
            240,
        ),
    ]
    results = {}
    for name, text, seconds in cases:
        print(f"RUN {name}", flush=True)
        try:
            results[name] = run_case(name, text, seconds)
            print(f"PASS {name} {results[name]}", flush=True)
        except Exception as exc:
            screen = visible_text()[-1500:]
            path = os.path.join(LOG_DIR, f"fail-{name}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(exc) + "\n\n" + screen)
            print(f"FAIL {name}: {exc}", flush=True)
            results[name] = {"error": str(exc), "screen": path}
    summary_path = os.path.join(LOG_DIR, "suite-summary.json")
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    failed = [name for name, item in results.items() if "error" in item]
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
