"""OSC 7501 is the Herdr App status write. hc has no status-set command.

A Kiro hook may run with stdout captured and with no controlling terminal.
The bytes have to land on the pane pty, which is the owner's stdout, and
the captured pipe must stay empty.
"""

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prototype"))

import herdr_kiro_osc as osc
import herdr_kiro_state as hook


def _read_ready(fd, timeout):
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return b""
    return os.read(fd, 4096)


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def _scratch(prefix):
    fd, path = tempfile.mkstemp(prefix=prefix)
    os.close(fd)
    return path


class OscMapTests(unittest.TestCase):
    def test_spec_states_are_the_only_words(self):
        self.assertEqual(osc.fields_for("working"), {"state": "working", "app": "kiro"})
        self.assertEqual(osc.fields_for("blocked"), {"state": "blocked", "kind": "permission", "app": "kiro"})
        self.assertEqual(
            osc.fields_for("blocked", "question_panel"),
            {"state": "blocked", "kind": "question", "app": "kiro"},
        )
        self.assertEqual(
            osc.fields_for("blocked", "workflow_question_esc_cancel"),
            {"state": "blocked", "kind": "question", "app": "kiro"},
        )
        self.assertEqual(
            osc.fields_for("blocked", "workspace_trust"),
            {"state": "blocked", "kind": "permission", "app": "kiro"},
        )
        # A finished TUI report is idle. The App word for that finish is done.
        self.assertEqual(osc.fields_for("idle"), {"state": "done", "app": "kiro"})
        self.assertIsNone(osc.fields_for("unknown"))
        self.assertIsNone(osc.fields_for("need-you"))
        self.assertIsNone(osc.fields_for("needs-you"))

    def test_wire_bytes_match_the_grammar(self):
        self.assertEqual(osc.encode(osc.fields_for("working")), b"\x1b]7501;state=working:app=kiro\x1b\\")
        self.assertEqual(
            osc.encode(osc.fields_for("blocked")),
            b"\x1b]7501;state=blocked:kind=permission:app=kiro\x1b\\",
        )
        self.assertEqual(
            osc.encode(osc.fields_for("blocked", "question_panel")),
            b"\x1b]7501;state=blocked:kind=question:app=kiro\x1b\\",
        )
        self.assertEqual(osc.encode(osc.fields_for("idle")), b"\x1b]7501;state=done:app=kiro\x1b\\")

    def test_hook_words_never_encode_state_idle(self):
        for kind in ("working", "idle", "blocked", "unknown"):
            fields = osc.fields_for(kind)
            if fields is None:
                continue
            self.assertNotEqual(fields["state"], "idle")
            self.assertNotIn(b"state=idle", osc.encode(fields))
        self.assertEqual(osc.fields_for("idle"), {"state": "done", "app": "kiro"})

    def test_a_regular_file_is_not_a_pane(self):
        with tempfile.NamedTemporaryFile(prefix="herdr-kiro-notty-") as handle:
            holder = subprocess.Popen(
                ["sleep", "30"],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(_stop, holder)
            payload = osc.encode(osc.fields_for("working"))
            self.assertEqual(osc.write_pid(payload, holder.pid), "")
            _stop(holder)
            handle.flush()
            handle.seek(0)
            self.assertNotIn(b"7501", handle.read())


class OscDeliveryTests(unittest.TestCase):
    def test_owner_pty_receives_the_sequence(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        holder = subprocess.Popen(
            ["sleep", "30"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        self.addCleanup(_stop, holder)
        _read_ready(master, 0.05)
        payload = osc.encode(osc.fields_for("idle"))
        written = osc.write_pid(payload, holder.pid)
        self.assertEqual(written, os.ttyname(slave))
        got = _read_ready(master, 1.0)
        self.assertIn(payload, got)
        _stop(holder)

    def test_setsid_hook_writes_the_ancestor_pty_not_its_pipe(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proto = os.path.join(os.path.dirname(__file__), "..", "prototype")
        hook_code = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import herdr_kiro_osc as osc\n"
            "payload = osc.encode(osc.fields_for('working'))\n"
            "path = osc.write_to_pane(payload, [os.getppid()])\n"
            "sys.stderr.write(path + '\\n')\n"
            "raise SystemExit(0 if path else 3)\n"
        )
        parent_code = (
            "import subprocess, sys\n"
            "proc = subprocess.Popen(\n"
            "    [sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.PIPE,\n"
            "    stderr=subprocess.PIPE,\n"
            "    start_new_session=True,\n"
            ")\n"
            "out, err = proc.communicate(timeout=15)\n"
            "open(sys.argv[3], 'wb').write(out)\n"
            "open(sys.argv[4], 'wb').write(err)\n"
            "open(sys.argv[5], 'w').write(str(proc.returncode))\n"
        )
        stdout_path = _scratch("herdr-kiro-hook-out-")
        stderr_path = _scratch("herdr-kiro-hook-err-")
        code_path = _scratch("herdr-kiro-hook-code-")
        for path in (stdout_path, stderr_path, code_path):
            self.addCleanup(lambda kept=path: os.path.exists(kept) and os.remove(kept))
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code, hook_code, proto, stdout_path, stderr_path, code_path],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        self.addCleanup(_stop, parent)
        self.assertEqual(parent.wait(timeout=20), 0)
        with open(code_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "0")
        with open(stdout_path, "rb") as handle:
            captured = handle.read()
        self.assertNotIn(b"7501", captured)
        with open(stderr_path, "rb") as handle:
            reported = handle.read().decode("utf-8", "replace").strip()
        self.assertEqual(reported, os.ttyname(slave))
        got = _read_ready(master, 1.0)
        self.assertIn(b"\x1b]7501;state=working:app=kiro\x1b\\", got)

    def test_lsof_text_names_stdout_before_the_controlling_tty(self):
        text = "p42\nf1u\nn/dev/ttys000\nf2w\nn/dev/ttys000\nf0r\nn/dev/ttys001\nfcwd\nn/\n"
        self.assertEqual(
            osc._devices_from_lsof(text),
            {"1": "/dev/ttys000", "2": "/dev/ttys000", "0": "/dev/ttys001"},
        )

    def test_missing_proc_writes_the_fd_device(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        holder = subprocess.Popen(
            ["sleep", "30"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        self.addCleanup(_stop, holder)
        device = os.ttyname(slave)
        payload = osc.encode(osc.fields_for("working"))
        real_isdir = os.path.isdir

        def hide_proc(path):
            text = str(path)
            if text.startswith("/proc/") and text.endswith("/fd"):
                return False
            return real_isdir(path)

        def devices(pid):
            if pid == holder.pid:
                return {"1": device}
            return {}

        with (
            mock.patch.object(osc, "_fd_devices", devices),
            mock.patch.object(osc.os.path, "isdir", hide_proc),
        ):
            written = osc.write_pid(payload, holder.pid)
        self.assertEqual(written, device)
        self.assertIn(payload, _read_ready(master, 1.0))
        _stop(holder)


class OscHookTests(unittest.TestCase):
    def test_app_idle_is_done_on_the_tty_and_tui_stays_idle(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        holder = subprocess.Popen(
            ["sleep", "30"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        self.addCleanup(_stop, holder)
        directory = tempfile.mkdtemp(prefix="herdr-kiro-osc-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")

        def seed(state):
            state["chat_pid"] = holder.pid
            state["reported"] = "working"
            state["seq"] = 4

        hook.update(path, seed)
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            return True

        env = {"HC_TERMINAL_ID": "t7"}
        with mock.patch.object(hook, "herdr", fake_herdr), mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(hook.send_report("t7", path, "idle"))
        self.assertEqual([args[args.index("--state") + 1] for args in calls], ["idle"])
        self.assertNotIn("release-agent", [args[0] for args in calls])
        got = _read_ready(master, 1.0)
        self.assertIn(b"\x1b]7501;state=done:app=kiro\x1b\\", got)
        _stop(holder)

    def test_tui_report_does_not_need_a_pane_tty(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-tui-")
        path = os.path.join(directory, "pane.json")
        self.addCleanup(shutil.rmtree, directory, True)
        hook.update(path, lambda state: state.update({"reported": "working", "seq": 4}))
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            return True

        def refuse(*_args, **_kwargs):
            raise AssertionError("tty write")

        env = {"HC_TERMINAL_ID": "", "HERDR_ENV": ""}
        with (
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(osc, "write_to_pane", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            self.assertTrue(hook.send_report("w1:p1", path, "idle"))
        self.assertEqual([args[args.index("--state") + 1] for args in calls], ["idle"])

    def test_a_second_idle_report_stays_done(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        holder = subprocess.Popen(
            ["sleep", "30"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        self.addCleanup(_stop, holder)
        directory = tempfile.mkdtemp(prefix="herdr-kiro-seen-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")

        def seed(state):
            state["chat_pid"] = holder.pid
            state["reported"] = "working"
            state["seq"] = 4

        hook.update(path, seed)

        def fake_herdr(*_args):
            return True

        env = {"HC_TERMINAL_ID": "t7"}
        with mock.patch.object(hook, "herdr", fake_herdr), mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(hook.send_report("t7", path, "idle"))
            self.assertTrue(hook.send_report("t7", path, "idle"))
        got = _read_ready(master, 1.0)
        payload = b"\x1b]7501;state=done:app=kiro\x1b\\"
        self.assertGreaterEqual(got.count(payload), 2)
        self.assertNotIn(b"state=idle", got)
        _stop(holder)


class AppExitOscTests(unittest.TestCase):
    """A non-idle watcher exit in the App writes wire state=done before the slot clears."""

    def _seed(self, path, busy=None, owners=None):
        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {} if busy is None else busy
            state["owners"] = {} if owners is None else owners
            state["owner_started"] = dict.fromkeys(state["owners"], "alive")
            state["chat_pid"] = 0
            state["reported"] = "working"
            state["seq"] = 4

        snap = {"ppid": 1, "lstart": "alive", "comm": "kiro-cli"}
        with mock.patch.object(hook, "process_snapshot", lambda _pid: snap):
            hook.update(path, seed)

    def _finish(self, path, pane, reason, env, busy=None):
        order = []
        payloads = []
        pid_lists = []
        log_file = path + ".log"
        reported = {} if busy is None else busy

        def fake_herdr(*args):
            order.append(("herdr", args))
            return True

        real_clear = hook.clear_watcher

        def spy_clear(state_path):
            order.append(("clear", state_path))
            return real_clear(state_path)

        real_emit = hook.emit_osc

        def spy_emit(state_path, kind, rule_id=""):
            order.append(("emit", state_path, kind, rule_id))
            return real_emit(state_path, kind, rule_id)

        def capture(payload, pids):
            payloads.append(payload)
            pid_lists.append(list(pids))
            return "/dev/pts/7"

        def refuse(*_args, **_kwargs):
            raise AssertionError("side-effecting command")

        scoped = dict(env)
        scoped["HERDR_KIRO_LOG"] = log_file
        snap = {"ppid": 1, "lstart": "alive", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "clear_watcher", spy_clear),
            mock.patch.object(hook, "emit_osc", spy_emit),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(osc, "write_to_pane", capture),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, scoped, clear=False),
        ):
            ok = hook.finish_if_unchanged(pane, path, {"generation": 3, "busy": reported}, reason)
        return ok, order, payloads, pid_lists

    def test_non_idle_app_exit_writes_done_before_the_watcher_clears(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-exit-")
        self.addCleanup(shutil.rmtree, directory, True)
        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": ""}
        done = b"\x1b]7501;state=done:app=kiro\x1b\\"
        busy = {"sess": time.time()}
        owners = {"sess": 4242}
        for reason in ("owner exit", "agent gone"):
            path = os.path.join(directory, reason.replace(" ", "-") + ".json")
            self._seed(path, busy=busy, owners=owners)
            ok, order, payloads, pid_lists = self._finish(path, "t7", reason, env, busy=busy)
            self.assertTrue(ok)
            emitted = ("emit", path, "idle", "")
            self.assertIn(emitted, order)
            self.assertLess(order.index(emitted), order.index(("clear", path)))
            self.assertEqual(payloads, [done])
            self.assertNotIn(b"state=idle", payloads[0])
            self.assertNotIn(b"kind=auth", payloads[0])
            self.assertIn(4242, pid_lists[0])
            released = [(item[0], item[1][:2]) for item in order if item[0] == "herdr"]
            self.assertIn(("herdr", ("release-agent", "t7")), released)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["watcher"], 0)

    def test_app_idle_finish_does_not_release_or_emit(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-idle-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        self._seed(path)
        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": ""}
        ok, order, payloads, pid_lists = self._finish(path, "t7", "idle", env)
        self.assertTrue(ok)
        self.assertEqual(payloads, [])
        self.assertEqual(pid_lists, [])
        self.assertFalse(any(item[0] == "emit" for item in order))
        self.assertFalse(any(item[0] == "herdr" and item[1][0] == "release-agent" for item in order))
        self.assertTrue(any(item[0] == "clear" for item in order))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 0)

    def test_tui_owner_exit_releases_without_osc(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-tui-exit-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")
        self._seed(path)
        env = {"HC_TERMINAL_ID": "", "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p9"}
        ok, order, payloads, pid_lists = self._finish(path, "w1:p9", "owner exit", env)
        self.assertTrue(ok)
        self.assertEqual(payloads, [])
        self.assertEqual(pid_lists, [])
        self.assertFalse(any(item[0] == "emit" for item in order))
        released = [(item[0], item[1][:2]) for item in order if item[0] == "herdr"]
        self.assertIn(("herdr", ("release-agent", "w1:p9")), released)

    def test_unknown_hook_state_writes_no_osc(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-unknown-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")

        def seed(state):
            state["seq"] = 2

        hook.update(path, seed)
        payloads = []

        def fake_herdr(*_args):
            return True

        def capture(payload, _pids):
            payloads.append(payload)
            return "/dev/pts/7"

        def refuse(*_args, **_kwargs):
            raise AssertionError("side-effecting command")

        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": "", "HERDR_KIRO_LOG": path + ".log"}
        with (
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(osc, "write_to_pane", capture),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            self.assertTrue(hook.send_report("t7", path, "unknown"))
            self.assertTrue(hook.emit_osc(path, "unknown"))
        self.assertEqual(payloads, [])

    def test_hook_words_do_not_send_kind_auth(self):
        for kind, rule in (
            ("working", ""),
            ("idle", ""),
            ("blocked", ""),
            ("blocked", "question_panel"),
            ("blocked", "workflow_question_esc_cancel"),
            ("blocked", "workspace_trust"),
            ("unknown", ""),
            ("auth", ""),
        ):
            fields = osc.fields_for(kind, rule)
            if fields is None:
                continue
            self.assertNotEqual(fields.get("kind"), "auth")
            encoded = osc.encode(fields)
            self.assertNotIn(b"kind=auth", encoded)
            self.assertNotIn(b"state=idle", encoded)


# Herdr App 0.1.12 defaults/mods/agents/main.js. hold() lines 126-131 and
# rank() lines 133-136. The painted section is this rank. herdr-app ls is
# Model::status, which is already idle once the turn has been seen.
_RANK = {"blocked": 4, "done": 3, "working": 2, "idle": 1}
_SECTION = {4: "Needs you", 3: "Done", 2: "Working", 1: "Idle"}


class _AgentsHold:
    def __init__(self):
        self.held = None

    def show(self, agents):
        current = next((agent for agent in agents if agent["focused"]), None)
        same = current is not None and self.held is not None and current["address"] == self.held["address"]
        if current is None or not same or current["status"] != "idle":
            if current is None:
                self.held = None
            else:
                self.held = {"address": current["address"], "rank": _RANK.get(current["status"], 0)}
        sections = {}
        for agent in agents:
            rank = self._rank(agent) or _RANK["idle"]
            sections[agent["address"]] = _SECTION[rank]
        return sections

    def _rank(self, agent):
        saw_done = (
            self.held is not None
            and agent["address"] == self.held["address"]
            and self.held["rank"] == _RANK["done"]
            and agent["status"] == "idle"
        )
        if saw_done:
            return _RANK["done"]
        return _RANK.get(agent["status"], 0)


class AgentsSectionTests(unittest.TestCase):
    def test_focused_row_stays_under_done_until_left(self):
        hold = _AgentsHold()
        self.assertEqual(
            hold.show([{"address": "t1", "status": "done", "focused": True}]),
            {"t1": "Done"},
        )
        self.assertEqual(
            hold.show([{"address": "t1", "status": "idle", "focused": True}]),
            {"t1": "Done"},
        )
        # The other tab is selected. A terminal with no status is absent from agents().
        self.assertEqual(
            hold.show([{"address": "t1", "status": "idle", "focused": False}]),
            {"t1": "Idle"},
        )

    def test_a_row_that_arrives_idle_is_under_idle(self):
        hold = _AgentsHold()
        self.assertEqual(
            hold.show([{"address": "t1", "status": "idle", "focused": True}]),
            {"t1": "Idle"},
        )

    def test_leaving_busy_moves_the_section_at_once(self):
        hold = _AgentsHold()
        hold.show([{"address": "t1", "status": "done", "focused": True}])
        self.assertEqual(
            hold.show([{"address": "t1", "status": "working", "focused": True}]),
            {"t1": "Working"},
        )
        self.assertEqual(
            hold.show([{"address": "t1", "status": "blocked", "focused": True}]),
            {"t1": "Needs you"},
        )


if __name__ == "__main__":
    unittest.main()
