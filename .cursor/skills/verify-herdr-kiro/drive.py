#!/usr/bin/env python3
"""Drive an isolated herdr session the way a Kiro pane uses this hook.

The screen stand-in is a process named kiro-cli. It is not Kiro. A live Mac
run starts real kiro-cli instead and does not call paint or hook.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
HOOK = os.path.join(ROOT, "prototype", "herdr_kiro_state.py")
TOML = os.path.join(ROOT, "prototype", "kiro-detection.toml")
PAINTER = os.path.join(os.path.dirname(__file__), "painter.c")
SESSION = "verify"


def run_dir() -> str:
    return os.environ.get("VERIFY_RUN") or "/tmp/herdr-kiro-verify"


def evidence_dir(meta: dict | None = None) -> str:
    if meta and meta.get("evidence"):
        return meta["evidence"]
    override = os.environ.get("VERIFY_EVIDENCE")
    if override:
        return override
    return "/tmp/herdr-kiro-verify-evidence"


def meta_path() -> str:
    return os.path.join(run_dir(), "run.json")


def load_meta() -> dict:
    with open(meta_path(), encoding="utf-8") as handle:
        meta = json.load(handle)
    if not isinstance(meta, dict):
        raise SystemExit("run.json is not an object")
    return meta


def save_meta(meta: dict) -> None:
    path = meta_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
        handle.write("\n")


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN") or shutil.which("herdr") or "herdr"


def base_env(home: str) -> dict:
    env = os.environ.copy()
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    env["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
    env["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
    env["XDG_CACHE_HOME"] = os.path.join(home, ".cache")
    env["PATH"] = "/usr/bin:/bin:" + env.get("PATH", "")
    env.pop("HERDR_SOCKET_PATH", None)
    env.pop("HERDR_SESSION", None)
    return env


def herdr(meta: dict, args: list, timeout: float = 20) -> subprocess.CompletedProcess:
    env = base_env(meta["home"])
    env["HERDR_SOCKET_PATH"] = meta["socket"]
    return subprocess.run(
        [herdr_bin(), "--session", SESSION, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=env,
    )


def require_ok(completed: subprocess.CompletedProcess, what: str) -> dict:
    if completed.returncode != 0:
        raise SystemExit(what + " failed: " + (completed.stdout or completed.stderr))
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(what + " was not json") from exc


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_throwaway_agent(home: str, workspace: str) -> None:
    agent_dir = os.path.join(home, ".kiro", "agents")
    os.makedirs(agent_dir, mode=0o700, exist_ok=True)
    agent = os.path.join(agent_dir, "verify-herdr.md")
    with open(agent, "w", encoding="utf-8") as handle:
        handle.write(
            "---\n"
            "name: verify-herdr\n"
            "description: Throwaway agent for herdr-kiro verification.\n"
            "tools:\n"
            '  - "*"\n'
            "---\n"
            "Ask before every tool. Do not trust tools ahead of time.\n"
        )
    os.chmod(agent, 0o600)
    config = os.path.join(workspace, ".kiro")
    os.makedirs(config, mode=0o700, exist_ok=True)
    permissions = os.path.join(config, "permissions.yaml")
    with open(permissions, "w", encoding="utf-8") as handle:
        handle.write(
            "# Throwaway verification permissions. Every tool asks.\n"
            "rules:\n"
            "  - tool: '*'\n"
            "    permission: ask\n"
        )
    os.chmod(permissions, 0o600)


def launch() -> None:
    if os.path.exists(meta_path()):
        try:
            old = load_meta()
        except (OSError, json.JSONDecodeError):
            old = {}
        if alive(int(old.get("server_pid") or 0)):
            raise SystemExit("a verify session is already running; cleanup before launch")
    home = os.path.join(run_dir(), "home")
    workspace = os.path.join(run_dir(), "workspace")
    if os.path.isdir(run_dir()):
        shutil.rmtree(run_dir())
    os.makedirs(home, mode=0o700)
    os.makedirs(workspace, mode=0o700)
    evidence = os.path.join(evidence_dir(), str(int(time.time())))
    os.makedirs(evidence, mode=0o700, exist_ok=True)
    detection = os.path.join(home, ".config", "herdr", "agent-detection")
    os.makedirs(detection, mode=0o700)
    shutil.copyfile(TOML, os.path.join(detection, "kiro.toml"))
    write_throwaway_agent(home, workspace)
    binary = os.path.join(run_dir(), "bin", "kiro-cli")
    os.makedirs(os.path.dirname(binary), mode=0o700)
    compiled = subprocess.run(
        ["cc", "-O2", "-o", binary, PAINTER],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if compiled.returncode != 0:
        raise SystemExit(compiled.stderr or "painter compile failed")
    os.chmod(binary, 0o755)
    fifo = os.path.join(run_dir(), "paint.fifo")
    os.mkfifo(fifo, 0o600)
    env = base_env(home)
    env["PATH"] = os.path.dirname(binary) + ":" + env["PATH"]
    log = open(os.path.join(evidence, "herdr-server.log"), "w", encoding="utf-8")
    server = subprocess.Popen(
        [herdr_bin(), "--session", SESSION, "server"],
        cwd=workspace,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    socket = os.path.join(home, ".config", "herdr", "sessions", SESSION, "herdr.sock")
    meta = {
        "home": home,
        "workspace": workspace,
        "evidence": evidence,
        "server_pid": server.pid,
        "socket": socket,
        "fifo": fifo,
        "painter": binary,
        "pane": "",
        "server_log": log.name,
    }
    save_meta(meta)
    deadline = time.time() + 15
    listed = None
    while time.time() < deadline:
        if server.poll() is not None:
            raise SystemExit("herdr server exited before it was ready")
        if os.path.exists(socket):
            listed = herdr(meta, ["workspace", "list"])
            if listed.returncode == 0 and "workspace_list" in listed.stdout:
                break
        time.sleep(0.2)
    else:
        raise SystemExit("herdr server did not accept workspace list")
    payload = require_ok(listed, "workspace list")
    workspaces = payload.get("result", {}).get("workspaces") or []
    if not workspaces:
        created = require_ok(
            herdr(meta, ["workspace", "create", "--cwd", workspace]),
            "workspace create",
        )
        if not created.get("result", {}).get("workspace"):
            raise SystemExit("herdr started with no workspace")
    panes = require_ok(herdr(meta, ["pane", "list"]), "pane list")
    pane_rows = panes.get("result", {}).get("panes") or []
    if not pane_rows:
        raise SystemExit("herdr started with no pane")
    meta["pane"] = pane_rows[0]["pane_id"]
    save_meta(meta)
    started = herdr(meta, ["pane", "run", meta["pane"], binary, fifo])
    if started.returncode != 0:
        raise SystemExit(started.stderr or started.stdout or "pane run failed")
    print(json.dumps({"ready": True, "pane": meta["pane"], "evidence": evidence}))


def doctor() -> None:
    meta = load_meta()
    if not alive(int(meta["server_pid"])):
        raise SystemExit("herdr server pid is not running")
    version = subprocess.run(
        [herdr_bin(), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    text = (version.stdout or version.stderr).strip()
    if "0.9.5" not in text:
        raise SystemExit("herdr version is not 0.9.5: " + text)
    listed = herdr(meta, ["workspace", "list"])
    if listed.returncode != 0:
        raise SystemExit(listed.stderr or listed.stdout)
    deadline = time.time() + 8
    agent = {}
    while time.time() < deadline:
        got = herdr(meta, ["agent", "get", meta["pane"]])
        if got.returncode == 0:
            try:
                agent = json.loads(got.stdout).get("result", {}).get("agent") or {}
            except json.JSONDecodeError:
                agent = {}
            if agent.get("agent") == "kiro":
                break
        time.sleep(0.3)
    else:
        raise SystemExit("pane agent is not kiro yet")
    if agent.get("cwd") and not str(agent.get("cwd")).startswith(run_dir()):
        raise SystemExit("pane cwd is outside this run")
    print(json.dumps({"doctor": "ok", "version": text, "pane": meta["pane"], "agent": "kiro"}))


def paint(mode: str) -> None:
    if mode not in ("idle", "working", "approval", "trust"):
        raise SystemExit("unknown paint mode")
    meta = load_meta()
    fd = os.open(meta["fifo"], os.O_WRONLY)
    try:
        os.write(fd, (mode + "\n").encode())
    finally:
        os.close(fd)
    time.sleep(0.3)
    print(json.dumps({"painted": mode}))


def hook_event(event: str) -> None:
    allowed = {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
    if event not in allowed:
        raise SystemExit("unknown hook event")
    meta = load_meta()
    env = base_env(meta["home"])
    env["HERDR_SOCKET_PATH"] = meta["socket"]
    env["HERDR_ENV"] = "1"
    env["HERDR_PANE_ID"] = meta["pane"]
    env["HERDR_BIN_PATH"] = herdr_bin()
    env["HERDR_KIRO_LOG"] = os.path.join(meta["evidence"], "hook.log")
    payload = json.dumps({"session_id": "verify-session", "hook_event_name": event})
    completed = subprocess.run(
        [sys.executable, HOOK, event],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or "hook exited nonzero")
    print(json.dumps({"hook": event, "exit": completed.returncode}))


def snapshot(meta: dict) -> dict:
    got = require_ok(herdr(meta, ["agent", "get", meta["pane"]]), "agent get")
    explained = herdr(meta, ["agent", "explain", meta["pane"], "--json"])
    agent = got.get("result", {}).get("agent") or {}
    try:
        explain = json.loads(explained.stdout)
    except json.JSONDecodeError:
        explain = {"raw": explained.stdout}
    rule = (explain.get("matched_rule") or {}).get("id")
    return {
        "agent_status": agent.get("agent_status"),
        "state_change_seq": agent.get("state_change_seq"),
        "completion_seq": agent.get("completion_seq"),
        "explain_state": explain.get("state"),
        "explain_rule": rule,
        "screen_detection_skipped": explain.get("screen_detection_skipped"),
    }


def status(save: str | None) -> None:
    meta = load_meta()
    row = snapshot(meta)
    if save:
        path = os.path.join(meta["evidence"], save)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(row, handle)
            handle.write("\n")
        row["saved"] = path
    print(json.dumps(row))


def wait_for(status_name: str, rule: str, seconds: float) -> None:
    meta = load_meta()
    deadline = time.time() + seconds
    last = {}
    while time.time() < deadline:
        last = snapshot(meta)
        if (
            last.get("agent_status") == status_name
            and last.get("explain_state") == status_name
            and last.get("explain_rule") == rule
        ):
            path = os.path.join(
                meta["evidence"], "wait-%s-%s.json" % (status_name, rule)
            )
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(last, handle)
                handle.write("\n")
            last["saved"] = path
            print(json.dumps(last))
            return
        time.sleep(0.3)
    raise SystemExit("timed out waiting for %s/%s last=%s" % (status_name, rule, json.dumps(last)))


def background() -> None:
    meta = load_meta()
    created = require_ok(
        herdr(meta, ["workspace", "create", "--cwd", meta["workspace"]]),
        "workspace create",
    )
    workspace_id = created.get("result", {}).get("workspace", {}).get("workspace_id")
    if not workspace_id or workspace_id == "w1":
        raise SystemExit("background workspace was not created")
    focused = herdr(meta, ["workspace", "focus", workspace_id])
    if focused.returncode != 0:
        raise SystemExit(focused.stderr or focused.stdout)
    print(json.dumps({"background": workspace_id, "pane": meta["pane"]}))


def focus_primary() -> None:
    meta = load_meta()
    focused = herdr(meta, ["workspace", "focus", "w1"])
    if focused.returncode != 0:
        raise SystemExit(focused.stderr or focused.stdout)
    print(json.dumps({"focused": "w1"}))


def cleanup() -> None:
    if not os.path.exists(meta_path()):
        print(json.dumps({"cleanup": "absent"}))
        return
    meta = load_meta()
    evidence = meta.get("evidence")
    env = base_env(meta["home"])
    env["HERDR_SOCKET_PATH"] = meta.get("socket") or ""
    env["HERDR_KIRO_LOG"] = os.path.join(evidence or "/tmp", "hook.log")
    state_root = os.path.join(meta["home"], ".local", "state", "herdr-kiro")
    if os.path.isdir(state_root):
        for name in os.listdir(state_root):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(state_root, name), encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            pid = int(state.get("watcher") or 0)
            if alive(pid):
                os.kill(pid, signal.SIGTERM)
    pid = int(meta.get("server_pid") or 0)
    if alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 3
        while time.time() < deadline and alive(pid):
            time.sleep(0.1)
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                os.kill(pid, signal.SIGKILL)
    scratch = run_dir()
    if os.path.isdir(scratch) and os.path.abspath(scratch) != os.path.abspath(evidence or ""):
        shutil.rmtree(scratch)
    print(json.dumps({"cleanup": "ok", "evidence": evidence}))


def main() -> int:
    args = sys.argv[1:]
    if not args:
        raise SystemExit("usage: drive.py launch|doctor|paint|hook|status|wait|background|focus-primary|cleanup")
    command = args[0]
    if command == "launch":
        launch()
    elif command == "doctor":
        doctor()
    elif command == "paint":
        paint(args[1] if len(args) > 1 else "")
    elif command == "hook":
        hook_event(args[1] if len(args) > 1 else "")
    elif command == "status":
        save = None
        if len(args) > 2 and args[1] == "--save":
            save = args[2]
        status(save)
    elif command == "wait":
        if len(args) < 3:
            raise SystemExit("usage: drive.py wait STATUS RULE [seconds]")
        seconds = float(args[3]) if len(args) > 3 else 8
        wait_for(args[1], args[2], seconds)
    elif command == "background":
        background()
    elif command == "focus-primary":
        focus_primary()
    elif command == "cleanup":
        cleanup()
    else:
        raise SystemExit("unknown command")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
