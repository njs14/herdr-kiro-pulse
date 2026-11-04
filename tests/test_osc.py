"""OSC 7501 is the Herdr App status write. hc has no status-set command.

A Kiro hook may run with stdout captured and with no controlling terminal.
The bytes have to land on the pane pty, which is the owner's stdout, and
the captured pipe must stay empty.
"""

import errno
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


def _remembered_tty(value):
    vars(hook)["_pane_tty"] = value


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
        path = os.path.join(directory, "owner-exit.json")
        self._seed(path, busy=busy, owners=owners)
        ok, order, payloads, pid_lists = self._finish(path, "t7", "owner exit", env, busy=busy)
        self.assertTrue(ok)
        emitted = ("emit", path, "idle", "")
        self.assertIn(emitted, order)
        self.assertLess(order.index(emitted), order.index(("clear", path)))
        self.assertEqual(payloads, [done])
        self.assertIn(4242, pid_lists[0])
        released = [(item[0], item[1][:2]) for item in order if item[0] == "herdr"]
        self.assertIn(("herdr", ("release-agent", "t7")), released)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 0)


class ReapedExitTests(unittest.TestCase):
    def setUp(self):
        self._release_held_fd()
        _remembered_tty("")

    def tearDown(self):
        self._release_held_fd()
        _remembered_tty("")

    def _release_held_fd(self):
        fd = getattr(hook, "_pane_fd", -1)
        if isinstance(fd, int) and fd >= 0:
            os.close(fd)
            hook._pane_fd = -1

    def _seed_owner(self, path):
        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": 4242}
            state["owner_started"] = {"sess": "alive"}
            state["chat_pid"] = 0
            state["reported"] = "working"
            state["seq"] = 4

        snap = {"ppid": 1, "lstart": "alive", "comm": "kiro-cli"}
        with mock.patch.object(hook, "process_snapshot", lambda _pid: snap):
            hook.update(path, seed)
        return snap

    def test_missed_saved_write_keeps_owners_and_the_watcher(self):
        # A failed exit write must not drop the owner, call release-agent, or
        # leave this watcher while its pid is still in the state file.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-miss-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        snap = self._seed_owner(path)
        stolen = 42424242
        waits = {"n": 0}
        during = {}
        calls = []
        real_kill = hook.os.kill

        def fake_kill(pid, sig):
            if int(pid) in (4242, stolen):
                raise OSError(errno.ESRCH, "gone")
            return real_kill(pid, sig)

        def fake_wait(_self, _pids, _timeout):
            waits["n"] += 1
            if waits["n"] == 2:
                with open(path, encoding="utf-8") as handle:
                    during["state"] = json.load(handle)

                def steal(state):
                    state["watcher"] = stolen

                hook.update(path, steal)
            if waits["n"] > 3:
                raise AssertionError("watcher did not leave after the slot moved")
            return False

        def refuse(*_args, **_kwargs):
            raise AssertionError("side-effecting command")

        def write_path(*_args):
            return False

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "kiro_in_pane", lambda _ids: True),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.object(osc, "write_to_pane", lambda _payload, _pids: ""),
            mock.patch.object(osc, "_tty_opens", lambda _pid: iter(())),
            mock.patch.object(osc, "_write_path", write_path),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            _remembered_tty("/dev/pts/7")
            hook._watch_loop("t7", path)
        seen = during.get("state") or {}
        self.assertEqual(
            [waits["n"], seen.get("owners"), seen.get("watcher"), [item[0] for item in calls]],
            [2, {"sess": 4242}, os.getpid(), []],
        )

    def _deliver_reaped(self, path, snap, master, slave, slave_name, *, forget_hook):
        done = b"\x1b]7501;state=done:app=kiro\x1b\\"
        proc_link = "/proc/4242/fd/1"
        phase = {"dead": False, "watch": False, "n": 0}
        device_opens = []
        proc_opens = []
        real_open = os.open
        real_kill = hook.os.kill
        real_send = hook.send_report

        def open_path(file, flags, *args, **kwargs):
            text = file.decode() if isinstance(file, bytes) else file
            if text == proc_link:
                proc_opens.append(text)
                return os.dup(slave)
            if text == slave_name:
                device_opens.append(text)
            return real_open(file, flags, *args, **kwargs)

        def fake_kill(pid, _sig):
            if int(pid) == 4242:
                if phase["dead"]:
                    raise OSError(errno.ESRCH, "gone")
                return None
            return real_kill(pid, _sig)

        def fake_wait(_self, _pids, _timeout):
            phase["n"] += 1
            if phase["n"] > 2:
                raise AssertionError("watcher stayed after the owner exit")
            phase["dead"] = phase["n"] == 2
            return False

        def tty_opens(pid):
            if int(pid) == 4242 and not phase["dead"]:
                yield proc_link, slave_name

        def write_to_pane(_payload, _pids):
            if phase["watch"] or phase["dead"]:
                return ""
            return slave_name

        def send_report(*args, **kwargs):
            if phase["watch"]:
                return True
            return real_send(*args, **kwargs)

        def refuse(*_args, **_kwargs):
            raise AssertionError("side-effecting command")

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "send_report", send_report),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "kiro_in_pane", lambda _ids: True),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.object(osc, "_tty_opens", tty_opens),
            mock.patch.object(osc, "write_to_pane", write_to_pane),
            mock.patch.object(os, "open", open_path),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            if forget_hook:
                self.assertTrue(hook.send_report("t7", path, "working"))
                _remembered_tty("")
                self._release_held_fd()
            else:
                _remembered_tty(slave_name)
            phase["watch"] = True
            hook._watch_loop("t7", path)
        return _read_ready(master, 1.0), device_opens, proc_opens, done

    def test_reaped_owner_exit_writes_done_to_the_tty_from_the_live_report(self):
        # The exit write has to be a descriptor opened while the owner was
        # alive. Opening the saved /dev/pts/N paints a pty allocated later.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-held-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        snap = self._seed_owner(path)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        got, device_opens, proc_opens, done = self._deliver_reaped(
            path, snap, master, slave, os.ttyname(slave), forget_hook=False
        )
        self.assertEqual(device_opens, [])
        self.assertGreaterEqual(len(proc_opens), 1)
        self.assertIn(done, got)

    def test_watcher_writes_a_fd_it_opened_after_the_hook_returns(self):
        # send_report remembers a device in this process, then returns. The
        # watcher is another interpreter and does not inherit that global.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-split-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        snap = self._seed_owner(path)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        got, device_opens, proc_opens, done = self._deliver_reaped(
            path, snap, master, slave, os.ttyname(slave), forget_hook=True
        )
        self.assertEqual(device_opens, [])
        self.assertGreaterEqual(len(proc_opens), 1)
        self.assertIn(done, got)


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
