#!/usr/bin/env python3
"""Run Kiro V3 workflows in a herdr pane and assert the pane stays working."""

import json
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prototype"))
sys.path.insert(0, os.path.join(ROOT, "harness"))

import contextlib

import herdr_kiro_state as hook
from demo_live import scrub


class SecretLeakError(RuntimeError):
    """KIRO_API_KEY was still present after scrub."""


def guard_text(text: str) -> str:
    """Scrub pane text. Fail when the key survives."""
    cleaned = scrub("" if text is None else text)
    secret = os.environ.get("KIRO_API_KEY", "")
    if secret and secret in cleaned:
        raise SecretLeakError("KIRO_API_KEY still present")
    return cleaned


def guard_obj(value):
    if isinstance(value, str):
        return guard_text(value)
    if isinstance(value, list):
        return [guard_obj(item) for item in value]
    if isinstance(value, dict):
        return {guard_text(str(key)): guard_obj(item) for key, item in value.items()}
    return value


def write_private(path: str, text: str) -> None:
    cleaned = guard_text(text)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(cleaned)


def say(text: str) -> None:
    print(guard_text(text), flush=True)


SESSION = os.environ.get("HERDR_SESSION", "kiro-repro")
PANE = os.environ.get("HERDR_PANE", "w1:p1")
LOG_DIR = os.path.join(ROOT, "logs")
HOOK_LOG = os.environ.get("HERDR_KIRO_LOG") or hook.log_path()
POLL_SECS = 0.4
IDLE_AFTER_STOP = 3.5


def herdr(*args, timeout=30):
    return subprocess.run(
        ["herdr", "--session", SESSION, *args],
        text=True,
        capture_output=True,
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
    write_private(os.path.join(LOG_DIR, "trust-dialog.txt"), text)
    write_private(
        os.path.join(LOG_DIR, "trust-dialog-status.txt"),
        f"agent_status={status} seq={seq}\n"
        + (
            "The trust-all confirmation is on the parent pane. "
            "herdr does not match it to a Kiro approval rule, so the status above is not blocked.\n"
        ),
    )
    herdr("agent", "send-keys", PANE, "down", "enter", timeout=10)
    time.sleep(1.2)
    return True


def max_used_seq():
    best = hook.read_seq_floor(hook.state_path(PANE))
    path = hook.state_path(PANE)
    try:
        with open(path, encoding="utf-8") as handle:
            best = max(best, int(json.load(handle).get("seq") or 0))
    except (OSError, ValueError, TypeError):
        pass
    logs = [HOOK_LOG, "/tmp/herdr-kiro-hook.log"]
    for log_path in logs:
        try:
            with open(log_path, encoding="utf-8") as handle:
                for line in handle:
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

    def reset(state):
        state["busy"] = {}
        state["seq"] = max(seq, int(state.get("seq") or 0))
        state["grace_until"] = 0
        state["grace_started"] = 0
        state["reported"] = None
        state["watcher"] = 0
        state["watcher_at"] = 0

    hook.update(hook.state_path(PANE), reset)


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
            # CLI 3.0 starts interactive chat on V3 without --v3. A headless
            # run that omits --trust-all-tools stays on V2, so this suite
            # keeps that flag. The approval check does not pass it.
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
        with open(HOOK_LOG, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
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


def first_hook_event(start):
    earliest = None
    try:
        with open(HOOK_LOG, encoding="utf-8") as handle:
            for line in handle:
                if " event=" not in line or "state event=" in line:
                    continue
                try:
                    stamped = float(line.split()[0])
                except ValueError:
                    continue
                if stamped < start:
                    continue
                if earliest is None or stamped < earliest:
                    earliest = stamped
    except OSError:
        return None
    return earliest


def assert_run(name, rows, stops, _run_start=None):
    """Working for the whole banner, then idle within 3.5s of the later end."""
    banner_rows = [row for row in rows if row.get("banner")]
    if not banner_rows:
        raise AssertionError(f"{name}: workflow banner never appeared")
    bad = [row for row in banner_rows if row.get("agent_status") != "working"]
    if bad:
        raise AssertionError(f"{name}: {len(bad)} non-working samples while the banner was up")
    anchor = banner_rows[-1]["t"]
    if stops:
        anchor = max(anchor, *stops)
    run = []
    stable = None
    for row in rows:
        if row.get("t", 0) < anchor:
            run = []
            continue
        if row.get("agent_status") == "idle":
            run.append(row)
            if len(run) >= 4 and stable is None:
                stable = run[0]["t"]
        else:
            run = []
    if stable is None:
        raise AssertionError(f"{name}: never settled idle")
    gap = stable - anchor
    if gap > IDLE_AFTER_STOP:
        raise AssertionError(f"{name}: sustained idle {gap:.2f}s after the run ended, wanted within {IDLE_AFTER_STOP}s")
    return {
        "banner_samples": len(banner_rows),
        "mid_run_idle": len(bad),
        "idle_gap_s": round(gap, 3),
        "anchor": round(anchor, 3),
        "last_stop": round(max(stops), 3) if stops else None,
    }


def write_log(name, rows):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"poll-{name}.jsonl")
    body = "".join(json.dumps(guard_obj(row)) + "\n" for row in rows)
    write_private(path, body)
    return path


def watcher_commands():
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as handle:
                command = handle.read().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "herdr_kiro_state.py" in command and "--watch" in command:
            found.append(f"{name} {command.strip()}")
    return found


def stop_watchers():
    for row in watcher_commands():
        pid = int(row.split()[0])
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 2
    while watcher_commands() and time.time() < deadline:
        time.sleep(0.1)


def save_stops(name, start):
    path = os.path.join(LOG_DIR, f"stops-{name}.txt")
    lines = []
    try:
        with open(HOOK_LOG, encoding="utf-8") as handle:
            for line in handle:
                if "event=Stop" not in line and "grace_until" not in line:
                    continue
                try:
                    stamped = float(line.split()[0])
                except ValueError:
                    continue
                if stamped >= start:
                    lines.append(guard_text(line if line.endswith("\n") else line + "\n"))
    except OSError:
        pass
    write_private(path, "".join(lines))
    return path


def save_ps(name):
    path = os.path.join(LOG_DIR, f"ps-{name}.txt")
    rows = [guard_text(row) for row in watcher_commands()]
    if rows:
        write_private(path, "\n".join(rows) + "\n")
    else:
        write_private(path, "no herdr_kiro_state.py watcher\n")
    return path, rows


def wait_until_settled():
    """Don't start a workflow while a release has left the pane unknown."""
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            status, _seq = agent_status()
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError):
            time.sleep(0.2)
            continue
        if status == "idle" and "ask a question" in visible_text():
            return
        time.sleep(0.2)


def run_case(name, text, seconds):
    stop_watchers()
    wait_until_settled()
    start = time.time()
    prompt(text)
    rows = poll_run(seconds)
    path = write_log(name, rows)
    stops = stop_times_since(start)
    stop_path = save_stops(name, start)
    deadline = time.time() + 2
    while watcher_commands() and time.time() < deadline:
        time.sleep(0.1)
    ps_path, leftover = save_ps(name)
    summary = assert_run(name, rows, stops, first_hook_event(start))
    if leftover:
        raise AssertionError(f"{name}: watcher still running\n" + "\n".join(leftover))
    summary["log"] = path
    summary["stops"] = len(stops)
    summary["stop_log"] = stop_path
    summary["ps"] = ps_path
    return summary


def main() -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    stop_watchers()
    ensure_kiro()
    release_stale_authority()
    cases = [
        ("sequential", "/workflow run two-step", 240),
        ("parallel", "/workflow run parallel-steps", 240),
        ("repeat", "/workflow run repeat-step", 240),
        ("watch", "/workflow run watch-step", 180),
        (
            "solo",
            "Start the saved workflow named solo with the run workflow tool. Do not run the shell command yourself.",
            240,
        ),
        (
            "watch-tool",
            "Start the saved workflow named watch-step with the run workflow tool. Do not run the shell command yourself.",
            180,
        ),
    ]
    results = {}
    try:
        for name, text, seconds in cases:
            say(f"RUN {name}")
            try:
                results[name] = guard_obj(run_case(name, text, seconds))
                say(f"PASS {name} {results[name]}")
            except SecretLeakError:
                say(f"FAIL {name}: KIRO_API_KEY still present")
                return 1
            except Exception as exc:
                screen = guard_text(visible_text()[-1500:])
                path = os.path.join(LOG_DIR, f"fail-{name}.txt")
                write_private(path, guard_text(str(exc)) + "\n\n" + screen)
                say(f"FAIL {name}: {guard_text(str(exc))}")
                results[name] = {"error": guard_text(str(exc)), "screen": path}
    except SecretLeakError:
        say("FAIL: KIRO_API_KEY still present")
        return 1
    summary_path = os.path.join(LOG_DIR, "suite-summary.json")
    write_private(summary_path, json.dumps(guard_obj(results), indent=2))
    failed = [name for name, item in results.items() if "error" in item]
    say(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
