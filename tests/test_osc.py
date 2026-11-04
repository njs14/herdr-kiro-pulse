"""OSC 7501 is the Herdr App status write. hc has no status-set command.

A Kiro hook may run with stdout captured and with no controlling terminal.
The bytes have to land on the pane pty, which is the owner's stdout, and
the captured pipe must stay empty.
"""

import contextlib
import errno
import json
import os
import select
import shutil
import signal
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
    try:
        return os.read(fd, 4096)
    except OSError as exc:
        # The last slave fd closed. An empty buffer is EIO, not a payload.
        if exc.errno == errno.EIO:
            return b""
        raise


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def _scratch(prefix):
    fd, path = tempfile.mkstemp(prefix=prefix)
    os.close(fd)
    return path


def _live_start(pid):
    deadline = time.time() + 2
    while time.time() < deadline:
        hook.begin_tick()
        snap = hook.process_snapshot(pid)
        if snap and not snap.get("error") and snap.get("lstart"):
            return snap["lstart"]
        time.sleep(0.02)
    raise AssertionError(f"no start token for pid {pid}")


def _read_pidfile(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return 0
    if not text.isdigit():
        return 0
    return int(text)


def _kill_pidfiles(result, suffixes=(".watch", ".hook", ".owner")):
    for suffix in suffixes:
        pid = _read_pidfile(result + suffix)
        if pid > 1:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def _remembered_tty(value):
    vars(hook)["_pane_tty"] = value


class OscMapTests(unittest.TestCase):
    def test_spec_states_are_the_only_words(self):
        self.assertEqual(osc.fields_for("working"), {"state": "working", "app": "kiro-cli"})
        self.assertEqual(osc.fields_for("blocked"), {"state": "blocked", "kind": "permission", "app": "kiro-cli"})
        self.assertEqual(
            osc.fields_for("blocked", "question_panel"),
            {"state": "blocked", "kind": "question", "app": "kiro-cli"},
        )
        self.assertEqual(
            osc.fields_for("blocked", "workflow_question_esc_cancel"),
            {"state": "blocked", "kind": "question", "app": "kiro-cli"},
        )
        self.assertEqual(
            osc.fields_for("blocked", "workspace_trust"),
            {"state": "blocked", "kind": "permission", "app": "kiro-cli"},
        )
        # A finished TUI report is idle. The App word for that finish is done.
        self.assertEqual(osc.fields_for("idle"), {"state": "done", "app": "kiro-cli"})
        self.assertIsNone(osc.fields_for("unknown"))

    def test_kiro_cli_and_kiro_are_both_the_app(self):
        # CLI 3.0 reports app=kiro-cli. An older session still sends app=kiro.
        self.assertTrue(osc.is_kiro_app("kiro-cli"))
        self.assertTrue(osc.is_kiro_app("kiro"))
        self.assertFalse(osc.is_kiro_app("claude"))
        self.assertFalse(osc.is_kiro_app(""))
        fields = osc.fields_for("working")
        assert fields is not None
        self.assertTrue(osc.is_kiro_app(fields["app"]))
        self.assertEqual(fields["app"], "kiro-cli")
        self.assertIsNone(osc.fields_for("need-you"))
        self.assertIsNone(osc.fields_for("needs-you"))

    def test_wire_bytes_match_the_grammar(self):
        self.assertEqual(osc.encode(osc.fields_for("working")), b"\x1b]7501;state=working:app=kiro-cli\x1b\\")
        self.assertEqual(
            osc.encode(osc.fields_for("blocked")),
            b"\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x1b\\",
        )
        self.assertEqual(
            osc.encode(osc.fields_for("blocked", "question_panel")),
            b"\x1b]7501;state=blocked:kind=question:app=kiro-cli\x1b\\",
        )
        self.assertEqual(osc.encode(osc.fields_for("idle")), b"\x1b]7501;state=done:app=kiro-cli\x1b\\")

    def test_hook_words_never_encode_state_idle(self):
        for kind in ("working", "idle", "blocked", "unknown"):
            fields = osc.fields_for(kind)
            if fields is None:
                continue
            self.assertNotEqual(fields["state"], "idle")
            self.assertNotIn(b"state=idle", osc.encode(fields))
        self.assertEqual(osc.fields_for("idle"), {"state": "done", "app": "kiro-cli"})

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
        self.assertIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got)

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

        token = _live_start(holder.pid)

        def seed(state):
            state["chat_pid"] = holder.pid
            state["chat_started"] = token
            state["chat_ident"] = "100.000000"
            state["reported"] = "working"
            state["seq"] = 4

        hook.update(path, seed)
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            return True

        env = {"HC_TERMINAL_ID": "t7"}
        with (
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            self.assertTrue(hook.send_report("t7", path, "idle"))
        self.assertEqual([args[args.index("--state") + 1] for args in calls], ["idle"])
        self.assertNotIn("release-agent", [args[0] for args in calls])
        got = _read_ready(master, 1.0)
        self.assertIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
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

    def test_tui_inside_the_app_skips_hc_read_and_osc(self):
        # A TUI server inside an App terminal inherits HC_TERMINAL_ID.
        # That pane reads itself and must not write OSC. HC_TERMINAL_ID
        # alone still uses hc read, and a failed read is not an empty screen.
        # Hook word idle stays state=done. An idle finish does not release.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-both-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda state: state.update({"reported": "working", "seq": 4, "generation": 1}))
        tui_text = "ask a question or describe a task\n"
        outer_text = "outer terminal\n"
        both = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_BIN_PATH": "",
            "HERDR_KIRO_LOG": os.path.join(directory, "hook.log"),
            "XDG_STATE_HOME": directory,
        }
        app = dict(both)
        app["HERDR_ENV"] = ""
        app["HERDR_PANE_ID"] = ""
        hc = {"n": 0}
        reads = []
        osc_writes = []
        calls = []
        views = []
        herdr_ok = {"value": False}

        def hc_screen():
            hc["n"] += 1
            return outer_text

        def pane_read(argv, **_kwargs):
            reads.append(list(argv))
            return tui_text

        def fake_herdr(*args):
            calls.append(tuple(args))
            return herdr_ok["value"]

        def capture(payload, _pids, **_kwargs):
            osc_writes.append(payload)
            return "/dev/pts/0"

        def view(text):
            views.append(text)
            return {"state": "blocked", "id": "question_panel", "ok": True}

        with (
            mock.patch.dict(os.environ, both, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_screen),
            mock.patch.object(hook.subprocess, "check_output", pane_read),
        ):
            self.assertEqual(hook.pane_text("w1:p1"), tui_text)
        self.assertEqual(hc["n"], 0)
        self.assertEqual(reads, [["herdr", "pane", "read", "w1:p1", "--source", "visible", "--lines", "80"]])

        hc["n"] = 0
        reads.clear()
        with (
            mock.patch.dict(os.environ, both, clear=False),
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "_hc_screen", hc_screen),
            mock.patch.object(hook.subprocess, "check_output", pane_read),
            mock.patch.object(hook, "screen_view", view),
            mock.patch.object(hook.herdr_kiro_osc, "write_to_pane", capture),
        ):
            self.assertFalse(hook.send_report("w1:p1", path, "idle"))
            self.assertFalse(hook.send_report("w1:p1", path, "blocked"))
            herdr_ok["value"] = True
            self.assertTrue(hook.send_report("w1:p1", path, "idle"))
        self.assertEqual(osc_writes, [])
        self.assertEqual(hc["n"], 0)
        self.assertEqual(reads, [])
        self.assertEqual(views, [])
        self.assertEqual([row[0] for row in calls], ["report-agent", "report-agent", "report-agent"])
        self.assertEqual([row[1] for row in calls], ["w1:p1", "w1:p1", "w1:p1"])
        self.assertEqual([row[row.index("--state") + 1] for row in calls], ["idle", "blocked", "idle"])
        self.assertNotIn("release-agent", [row[0] for row in calls])

        def hc_fail():
            hc["n"] += 1
            return

        def hc_empty():
            hc["n"] += 1
            return ""

        def refuse_pane(*_args, **_kwargs):
            raise AssertionError("pane read")

        hc["n"] = 0
        with (
            mock.patch.dict(os.environ, app, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_fail),
            mock.patch.object(hook.subprocess, "check_output", refuse_pane),
        ):
            self.assertIsNone(hook.pane_text("t7"))
        self.assertEqual(hc["n"], 1)

        hc["n"] = 0
        with (
            mock.patch.dict(os.environ, app, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_empty),
            mock.patch.object(hook.subprocess, "check_output", refuse_pane),
        ):
            self.assertEqual(hook.pane_text("t7"), "")
        self.assertEqual(hc["n"], 1)

        calls.clear()
        osc_writes.clear()
        herdr_ok["value"] = True
        with (
            mock.patch.dict(os.environ, app, clear=False),
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "_hc_screen", hc_fail),
            mock.patch.object(hook.subprocess, "check_output", refuse_pane),
            mock.patch.object(hook.herdr_kiro_osc, "write_to_pane", capture),
        ):
            self.assertTrue(hook.send_report("t7", path, "idle"))
            self.assertTrue(hook.finish_if_unchanged("t7", path, {"generation": 1, "busy": {}}, "idle"))
        self.assertEqual(osc_writes, [b"\x1b]7501;state=done:app=kiro-cli\x1b\\"])
        self.assertEqual([row[row.index("--state") + 1] for row in calls if row[0] == "report-agent"], ["idle"])
        self.assertFalse(any(row[0] == "release-agent" for row in calls))

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

        token = _live_start(holder.pid)

        def seed(state):
            state["chat_pid"] = holder.pid
            state["chat_started"] = token
            state["chat_ident"] = "100.000000"
            state["reported"] = "working"
            state["seq"] = 4

        hook.update(path, seed)

        def fake_herdr(*_args):
            return True

        env = {"HC_TERMINAL_ID": "t7"}
        with (
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            self.assertTrue(hook.send_report("t7", path, "idle"))
            self.assertTrue(hook.send_report("t7", path, "idle"))
        got = _read_ready(master, 1.0)
        payload = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
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
            state["owner_ident"] = {key: hook.hires_start(pid) for key, pid in state["owners"].items()}
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

        real_held = hook._write_held_fd

        def spy_held():
            order.append(("held",))
            return real_held()

        def capture(payload, pids, **_kwargs):
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
            mock.patch.object(hook, "_write_held_fd", spy_held),
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
        # The exit write is the fd opened while the owner was alive.
        # write_to_pane is not that write, and its success does not clear.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-exit-")
        self.addCleanup(shutil.rmtree, directory, True)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        hook._pane_fd = os.dup(slave)

        def drop_held():
            fd = hook._pane_fd
            hook._pane_fd = -1
            if isinstance(fd, int) and fd >= 0:
                os.close(fd)

        self.addCleanup(drop_held)
        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": ""}
        done = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
        busy = {"sess": time.time()}
        owners = {"sess": 4242}
        path = os.path.join(directory, "owner-exit.json")
        self._seed(path, busy=busy, owners=owners)
        ok, order, payloads, _pid_lists = self._finish(path, "t7", "owner exit", env, busy=busy)
        self.assertTrue(ok)
        self.assertEqual(payloads, [])
        self.assertNotIn(("emit", path, "idle", ""), order)
        self.assertLess(order.index(("held",)), order.index(("clear", path)))
        self.assertIn(done, _read_ready(master, 0.5))
        released = [(item[0], item[1][:2]) for item in order if item[0] == "herdr"]
        self.assertIn(("herdr", ("release-agent", "t7")), released)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 0)


_SETSID_DRIVER = """\
import os
import subprocess
import sys

mode = sys.argv[1]
proto = sys.argv[2]
held = int(sys.argv[3])
path = sys.argv[4]
result = sys.argv[5]
os.environ["HC_TERMINAL_ID"] = "t7"
os.environ["HERDR_ENV"] = ""
os.environ["HERDR_PANE_ID"] = ""
os.environ["HERDR_KIRO_LOG"] = path + ".log"
os.environ["HERDR_BIN_PATH"] = os.path.join(os.path.dirname(path), "missing-herdr")
os.environ["XDG_STATE_HOME"] = os.path.dirname(path)
sys.path.insert(0, proto)
import herdr_kiro_state as hook

if mode == "mid":
    os.set_inheritable(held, True)
    with open(result + ".err", "w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            [sys.executable, __file__, "leaf", proto, str(held), path, result],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
            pass_fds=(held,),
        )
        code = proc.wait(timeout=20)
    raise SystemExit(code)

hook._pane_fd = held
ok = hook.finish_if_unchanged(
    "t7",
    path,
    {"generation": 3, "busy": {"sess": 1}},
    "owner exit",
)
with open(result, "w", encoding="utf-8") as handle:
    handle.write("ok\\n" if ok else "miss\\n")
raise SystemExit(0 if ok else 1)
"""


# The owner holds the pty. Its child calls send_report. write_to_pane walks
# that child up to the owner, which is the tty the bytes land on.
_SEC_REPORT_DRIVER = """\
import os
import subprocess
import sys
import time
import traceback

mode = sys.argv[1]
proto = sys.argv[2]
path = sys.argv[3]
result = sys.argv[4]

if mode == "owner":
    with open(result + ".owner", "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    go = result + ".go"
    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(go):
        time.sleep(0.02)
    proc = subprocess.Popen(
        [sys.executable, __file__, "report", proto, path, result],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raise SystemExit(proc.wait(timeout=20))

os.environ["HC_TERMINAL_ID"] = "t7"
os.environ["HERDR_ENV"] = ""
os.environ["HERDR_PANE_ID"] = ""
os.environ["HERDR_KIRO_LOG"] = path + ".log"
os.environ["HERDR_BIN_PATH"] = os.path.join(os.path.dirname(path), "missing-herdr")
os.environ["XDG_STATE_HOME"] = os.path.dirname(path)
sys.path.insert(0, proto)
try:
    import herdr_kiro_state as hook

    hook.send_report("t7", path, "working")
    hook.send_report("t7", path, "idle")
except Exception:
    with open(path + ".crash", "w", encoding="utf-8") as handle:
        handle.write(traceback.format_exc())
    raise
"""


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
            state["owner_ident"] = {"sess": hook.hires_start(4242)}
            state["chat_pid"] = 0
            state["reported"] = "working"
            state["seq"] = 4

        snap = {"ppid": 1, "lstart": "alive", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        ):
            hook.update(path, seed)
        return snap

    def _run_missed_exit(self, busy_at):
        # A failed exit write must not drop the owner, call release-agent, or
        # leave this watcher while its pid is still in the state file.
        # A reaped pid is absent from the process table and kill returns ESRCH.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-miss-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        self._seed_owner(path)
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["busy"]["sess"] = busy_at
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        stolen = 42424242
        waits = {"n": 0}
        during = {}
        calls = []
        real_kill = hook.os.kill

        def reaped(pid):
            if int(pid) == 4242:
                return None
            return {"ppid": 1, "lstart": "alive", "comm": "kiro-cli"}

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
            mock.patch.object(hook, "process_snapshot", reaped),
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "kiro_in_pane", lambda _ids: True),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.object(osc, "write_to_pane", lambda *_args, **_kwargs: ""),
            mock.patch.object(osc, "_tty_opens", lambda _pid: iter(())),
            mock.patch.object(osc, "_write_path", write_path),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            _remembered_tty("/dev/pts/7")
            hook._watch_loop("t7", path)
        return waits["n"], during.get("state") or {}, calls

    def test_missed_saved_write_keeps_owners_and_the_watcher(self):
        waits, seen, calls = self._run_missed_exit(time.time())
        self.assertEqual(
            [waits, seen.get("owners"), seen.get("watcher"), [item[0] for item in calls]],
            [2, {"sess": 4242}, os.getpid(), []],
        )

    def test_missed_write_keeps_an_owner_older_than_the_stale_window(self):
        # retain_dead_owner used to keep the row only while now - seen < 1800.
        # A missed write with a stamp 1801s old dropped the owner and kept the watcher.
        waits, seen, calls = self._run_missed_exit(time.time() - 1801)
        self.assertEqual(
            [waits, seen.get("owners"), seen.get("watcher"), [item[0] for item in calls]],
            [2, {"sess": 4242}, os.getpid(), []],
        )

    def _deliver_reaped(self, path, snap, master, slave, slave_name):
        done = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
        proc_link = "/proc/4242/fd/1"
        phase = {"dead": False, "watch": False, "n": 0}
        device_opens = []
        proc_opens = []
        real_open = os.open
        real_kill = hook.os.kill

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

        def write_to_pane(_payload, _pids, **_kwargs):
            if phase["watch"] or phase["dead"]:
                return ""
            return slave_name

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
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "kiro_in_pane", lambda _ids: True),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.object(osc, "_tty_opens", tty_opens),
            mock.patch.object(osc, "write_to_pane", write_to_pane),
            mock.patch.object(os, "open", open_path),
            mock.patch.object(hook, "hires_start", lambda _pid: "" if phase["dead"] else "100.000000"),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
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
        got, device_opens, proc_opens, done = self._deliver_reaped(path, snap, master, slave, os.ttyname(slave))
        self.assertEqual(device_opens, [])
        self.assertGreaterEqual(len(proc_opens), 1)
        self.assertIn(done, got)

    def _write_state(self, path, state):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def test_failed_held_write_does_not_count_another_tty(self):
        # A miss on the held fd stays a miss. write_to_pane succeeding
        # must not release the owner or clear the watcher.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-held-miss-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        self._seed_owner(path)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        hook._pane_fd = os.dup(slave)
        calls = []
        painted = []

        def boom(_fd, _payload):
            raise OSError(errno.EIO, "closed")

        def paint(_payload, _pids, **_kwargs):
            painted.append(True)
            return "/dev/pts/7"

        def refuse(*_args, **_kwargs):
            raise AssertionError("side-effecting command")

        def reaped(_pid):
            return None

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.object(hook, "process_snapshot", reaped),
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.object(osc, "_write_all", boom),
            mock.patch.object(osc, "write_to_pane", paint),
            mock.patch.object(hook.subprocess, "run", refuse),
            mock.patch.object(hook.subprocess, "check_output", refuse),
            mock.patch.object(hook.subprocess, "Popen", refuse),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            ok = hook.finish_if_unchanged("t7", path, {"generation": 3, "busy": {"sess": 1}}, "owner exit")
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(
            [ok, saved.get("owners"), saved.get("watcher"), painted, [item[0] for item in calls]],
            [False, {"sess": 4242}, os.getpid(), [], []],
        )

    def test_reused_pid_does_not_receive_the_exit_write(self):
        # A live process has the owner's pid and a different start token.
        # state=done goes to the fd held from the original tty, or the write
        # misses. The new process's tty stays empty.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-reuse-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        held_master, held_slave = os.openpty()
        new_master, new_slave = os.openpty()
        self.addCleanup(os.close, held_master)
        self.addCleanup(os.close, new_master)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=new_slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        hook._pane_fd = os.dup(held_slave)
        os.close(held_slave)
        os.close(new_slave)
        state = hook.fresh_state()
        state["watcher"] = os.getpid()
        state["generation"] = 3
        state["busy"] = {"sess": time.time()}
        state["owners"] = {"sess": proc.pid}
        state["owner_started"] = {"sess": "not-the-real-lstart"}
        state["reported"] = "working"
        state["seq"] = 4
        self._write_state(path, state)
        calls = []
        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        done = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
        with (
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            ok = hook.finish_if_unchanged(
                "t7",
                path,
                {"generation": 3, "busy": {"sess": state["busy"]["sess"]}},
                "owner exit",
            )
        new_got = _read_ready(new_master, 0.3)
        held_got = _read_ready(held_master, 0.3)
        self.assertNotIn(done, new_got)
        self.assertIn(done, held_got)
        self.assertTrue(ok)
        self.assertIn(("release-agent",), [item[:1] for item in calls])

    def test_tui_inside_app_does_not_write_the_exit_osc(self):
        # HERDR_ENV and HC_TERMINAL_ID both survive into the watcher.
        # Those exit bytes would land on the inner tty. A TUI pane must not
        # get state=done from the owner-exit write.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-tui-exit-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        hook._pane_fd = os.dup(slave)
        stamp = time.time()
        state = hook.fresh_state()
        state["watcher"] = os.getpid()
        state["generation"] = 3
        state["busy"] = {"sess": stamp}
        state["owners"] = {"sess": 4242}
        state["owner_started"] = {"sess": "alive"}
        state["seq"] = 4
        self._write_state(path, state)
        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            ok = hook.finish_if_unchanged(
                "t7",
                path,
                {"generation": 3, "busy": {"sess": stamp}},
                "owner exit",
            )
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"")
        self.assertTrue(ok)

    def test_replaced_fd_is_not_the_held_tty(self):
        # fstat of a character device is not enough. The held fd has to be
        # the tty _tty_opens named. A replaced fd 1 must not get state=done.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-rdev-")
        self.addCleanup(shutil.rmtree, directory, True)
        named_master, named_slave = os.openpty()
        wrong_master, wrong_slave = os.openpty()
        self.addCleanup(os.close, named_master)
        self.addCleanup(os.close, named_slave)
        self.addCleanup(os.close, wrong_master)
        self.addCleanup(os.close, wrong_slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=named_slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        hook.begin_tick()
        snap = hook.process_snapshot(proc.pid)
        lstart = (snap or {}).get("lstart") or ""
        self.assertTrue(lstart)
        named = os.ttyname(named_slave)

        def opens(_pid):
            yield f"/proc/{proc.pid}/fd/1", named

        def open_held(_path):
            return os.dup(wrong_slave)

        with (
            mock.patch.object(osc, "_tty_opens", opens),
            mock.patch.object(hook, "_open_held", open_held),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        ):
            hook._hold_live_owner(proc.pid, lstart, "100.000000")
            wrote = hook._write_held_fd()
        wrong = _read_ready(wrong_master, 0.2)
        named_got = _read_ready(named_master, 0.2)
        self.assertFalse(wrote)
        self.assertEqual(wrong, b"")
        self.assertEqual(named_got, b"")
        self.assertLess(hook._pane_fd, 0)

    def test_same_second_reuse_does_not_receive_the_exit_write(self):
        # owner_started is whole-second lstart, so a pid reused in that same
        # second still matches. The pid file uses hires_start. The exit write
        # has to use that identity, and this tty must stay empty.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-same-second-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        hook.begin_tick()
        snap = hook.process_snapshot(proc.pid)
        lstart = (snap or {}).get("lstart") or ""
        self.assertTrue(lstart)
        self.assertNotEqual(hook.hires_start(proc.pid), "sec:not-this-process")
        stamp = time.time()

        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {"sess": stamp}
            state["owners"] = {"sess": proc.pid}
            state["owner_started"] = {"sess": lstart}
            state["owner_ident"] = {"sess": "sec:not-this-process"}
            state["chat_pid"] = 0
            state["reported"] = "working"
            state["seq"] = 4

        hook.update(path, seed)
        stolen = 42424242
        waits = {"n": 0}
        real_kill = hook.os.kill

        def fake_kill(pid, sig):
            if int(pid) == stolen:
                raise OSError(errno.ESRCH, "gone")
            return real_kill(pid, sig)

        def fake_wait(_self, _pids, _timeout):
            waits["n"] += 1
            if waits["n"] == 2:
                hook.finish_if_unchanged(
                    "t7",
                    path,
                    {"generation": 3, "busy": {"sess": stamp}},
                    "owner exit",
                )

                def steal(state):
                    state["watcher"] = stolen

                hook.update(path, steal)
            if waits["n"] > 3:
                raise AssertionError("watcher did not leave after the slot moved")
            return False

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "kiro_in_pane", lambda _ids: True),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            hook._watch_loop("t7", path)
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"")

    def _run_status_loop(self, path, seed):
        hook.begin_tick()
        hook.update(path, seed)
        stolen = 42424242
        waits = {"n": 0}
        real_kill = hook.os.kill

        def fake_kill(pid, sig):
            if int(pid) == stolen:
                raise OSError(errno.ESRCH, "gone")
            return real_kill(pid, sig)

        def fake_wait(_self, _pids, _timeout):
            waits["n"] += 1
            if waits["n"] == 2:

                def steal(state):
                    state["watcher"] = stolen

                hook.update(path, steal)
            if waits["n"] > 3:
                raise AssertionError("watcher did not leave after the slot moved")
            return False

        directory = os.path.dirname(path)
        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
            "XDG_STATE_HOME": directory,
            "HERDR_BIN_PATH": os.path.join(directory, "missing-herdr"),
        }
        with (
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            hook._watch_loop("t7", path)

    def test_same_second_lstart_does_not_receive_status(self):
        # owner_started matches this pid's whole-second lstart and owner_ident
        # is empty. That pid stays the owner today, and the watch loop writes
        # its tty. send_report is the real function. Neither state=done nor
        # state=working may land there.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-lstart-status-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        lstart = _live_start(proc.pid)
        self.assertNotEqual(hook.hires_start(proc.pid), lstart)
        stamp = time.time()

        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {"sess": stamp}
            state["owners"] = {"sess": proc.pid}
            state["owner_started"] = {"sess": lstart}
            state["owner_ident"] = {}
            state["chat_pid"] = 0
            state["chat_started"] = ""
            state["reported"] = None
            state["seq"] = 4

        self._run_status_loop(path, seed)
        hook.emit_osc(path, "idle")
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertNotIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"")

    def test_same_second_chat_lstart_does_not_receive_status(self):
        # chat_started is that same whole-second lstart. The watch loop treats
        # the pid as the owner and emit_osc writes its tty.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-chat-lstart-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        lstart = _live_start(proc.pid)
        stamp = time.time()

        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {"sess": stamp}
            state["owners"] = {}
            state["chat_pid"] = proc.pid
            state["chat_started"] = lstart
            state["reported"] = None
            state["seq"] = 4

        self._run_status_loop(path, seed)
        hook.emit_osc(path, "idle")
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertNotIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"")

    def _sec_ancestor_report(self, role):
        """send_report from a child of a live pid known only by sec: plus lstart.

        The state file is written directly. send_report's own update is what
        drops that owner on 7fb233c, and write_to_pane then walks the child
        up to this pty. The bytes under test are the ones on that pty.
        """
        directory = tempfile.mkdtemp(prefix="herdr-kiro-sec-ancestor-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        result = os.path.join(directory, "result")
        driver = os.path.join(directory, "driver.py")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(_SEC_REPORT_DRIVER)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        err_path = os.path.join(directory, "owner.err")
        proto = os.path.dirname(os.path.abspath(hook.__file__))
        with open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [sys.executable, driver, "owner", proto, path, result],
                stdin=subprocess.DEVNULL,
                stdout=slave,
                stderr=err,
            )
        self.addCleanup(_stop, proc)
        self.addCleanup(_kill_pidfiles, result, (".owner",))
        owner_pid = 0
        deadline = time.time() + 5
        while time.time() < deadline:
            owner_pid = _read_pidfile(result + ".owner")
            if owner_pid > 1:
                break
            time.sleep(0.02)
        self.assertGreater(owner_pid, 1)
        lstart = _live_start(owner_pid)
        ident = "sec:" + lstart
        self.assertFalse(hook.fine_start(ident))
        state = hook.fresh_state()
        state["generation"] = 3
        state["busy"] = {"sess": time.time()}
        state["seq"] = 4
        if role == "chat":
            state["chat_pid"] = owner_pid
            state["chat_started"] = lstart
            state["chat_ident"] = ident
        elif role == "owner":
            state["owners"] = {"sess": owner_pid}
            state["owner_started"] = {"sess": lstart}
            state["owner_ident"] = {"sess": ident}
        else:
            raise AssertionError(role)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        with open(result + ".go", "w", encoding="utf-8") as handle:
            handle.write("go\n")
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _kill_pidfiles(result, (".owner",))
            proc.kill()
            proc.wait(timeout=5)
            self.fail("reporter did not finish")
        if os.path.exists(path + ".crash"):
            with open(path + ".crash", encoding="utf-8") as handle:
                self.fail(handle.read())
        device = os.ttyname(slave)
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"", device)
        log_path = path + ".log"
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as handle:
                logged = handle.read()
            self.assertNotIn("osc working via " + device, logged)
            self.assertNotIn("osc done via " + device, logged)

    def test_empty_token_does_not_paint_done(self):
        # owner_ident is sec: plus this live ancestor's lstart. send_report
        # must not paint the tty write_to_pane walks to.
        self._sec_ancestor_report("owner")

    def test_sec_lstart_collision_does_not_paint_done(self):
        # chat_ident is that same sec: string on the same ancestor.
        self._sec_ancestor_report("chat")

    def test_setsid_watcher_does_not_write_the_parent_pty(self):
        # A setsid watcher with stdio on /dev/null still has a parent pty.
        # The exit must not paint that parent. The held fd is the write.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-setsid-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        result = os.path.join(directory, "result")
        driver = os.path.join(directory, "driver.py")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(_SETSID_DRIVER)
        parent_master, parent_slave = os.openpty()
        held_master, held_slave = os.openpty()
        self.addCleanup(os.close, parent_master)
        self.addCleanup(os.close, held_master)
        # macOS drops unread pty output when the last slave fd closes.
        # Keep these open through the read. Do not reopen the device path.
        self.addCleanup(os.close, parent_slave)
        self.addCleanup(os.close, held_slave)
        state = hook.fresh_state()
        state["watcher"] = 0
        state["generation"] = 3
        state["busy"] = {"sess": time.time()}
        state["owners"] = {"sess": 1}
        state["owner_started"] = {"sess": "not-the-real-lstart"}
        state["reported"] = "working"
        state["seq"] = 4
        self._write_state(path, state)
        proto = os.path.dirname(os.path.abspath(hook.__file__))
        proc = subprocess.Popen(
            [sys.executable, driver, "mid", proto, str(held_slave), path, result],
            stdin=subprocess.DEVNULL,
            stdout=parent_slave,
            stderr=subprocess.PIPE,
            pass_fds=(held_slave,),
        )
        try:
            _out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise
        detail = err.decode("utf-8", "replace")
        err_path = result + ".err"
        if os.path.exists(err_path):
            with open(err_path, encoding="utf-8") as handle:
                detail = detail + handle.read()
        self.assertEqual(proc.returncode, 0, detail)
        done = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
        parent_got = _read_ready(parent_master, 0.3)
        held_got = _read_ready(held_master, 0.3)
        self.assertNotIn(done, parent_got, detail)
        self.assertIn(done, held_got, detail)

    def test_empty_owner_started_does_not_hold_the_live_pid(self):
        # An empty start token must not attach the hold to whatever process
        # now has that pid, and the exit must not write that process's tty.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-empty-start-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        # macOS drops unread pty output when the last slave fd closes.
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        stamp = time.time()

        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {"sess": stamp}
            state["owners"] = {"sess": proc.pid}
            state["owner_started"] = {"sess": ""}
            state["chat_pid"] = 0
            state["reported"] = "working"
            state["seq"] = 4

        # A warm ps cache would drop this live pid before the hold runs.
        hook.begin_tick()
        hook.update(path, seed)
        stolen = 42424242
        waits = {"n": 0}
        real_kill = hook.os.kill

        def fake_kill(pid, sig):
            if int(pid) == stolen:
                raise OSError(errno.ESRCH, "gone")
            return real_kill(pid, sig)

        def fake_wait(_self, _pids, _timeout):
            waits["n"] += 1
            if waits["n"] == 2:
                # The first iteration already tried to hold. This is the exit write.
                hook.finish_if_unchanged(
                    "t7",
                    path,
                    {"generation": 3, "busy": {"sess": stamp}},
                    "owner exit",
                )

                def steal(state):
                    state["watcher"] = stolen

                hook.update(path, steal)
            if waits["n"] > 3:
                raise AssertionError("watcher did not leave after the slot moved")
            return False

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "kiro_in_pane", lambda _ids: True),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            hook._watch_loop("t7", path)
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertNotIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"")

    def test_empty_owner_started_does_not_receive_a_status_write(self):
        # send_report stays real. An empty start token is not that live pid,
        # so the watch loop must not write state=working, or any other status,
        # onto its pty.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-empty-report-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_stop, proc)
        device = os.ttyname(slave)

        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 3
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": proc.pid}
            state["owner_started"] = {"sess": ""}
            state["chat_pid"] = 0
            state["reported"] = None
            state["seq"] = 4

        # A warm ps cache treats this new sleep as dead and the write never runs.
        hook.begin_tick()
        hook.update(path, seed)
        stolen = 42424242
        waits = {"n": 0}
        real_kill = hook.os.kill

        def fake_kill(pid, sig):
            if int(pid) == stolen:
                raise OSError(errno.ESRCH, "gone")
            return real_kill(pid, sig)

        def fake_wait(_self, _pids, _timeout):
            waits["n"] += 1
            if waits["n"] == 2:

                def steal(state):
                    state["watcher"] = stolen

                hook.update(path, steal)
            if waits["n"] > 3:
                raise AssertionError("watcher did not leave after the slot moved")
            return False

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
            "XDG_STATE_HOME": directory,
            "HERDR_BIN_PATH": os.path.join(directory, "missing-herdr"),
        }
        with (
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            mock.patch.object(hook.os, "kill", fake_kill),
            mock.patch.dict(os.environ, env, clear=False),
        ):
            hook._watch_loop("t7", path)
        got = _read_ready(master, 0.3)
        self.assertNotIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got)
        self.assertNotIn(b"\x1b]7501;state=done:app=kiro-cli\x1b\\", got)
        self.assertEqual(got, b"")
        log_path = path + ".log"
        logged = ""
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as handle:
                logged = handle.read()
        self.assertNotIn(device, logged)


# The child-sleep check above stays green on 3113252: that sleep is not an
# ancestor, so the parent walk never sees it. This driver is the tree
# ensure_watcher starts. The hook stays alive. The owner is the hook's parent
# and holds the pty. send_report is the real function inside the watcher.
_ANCESTOR_DRIVER = """\
import os
import subprocess
import sys
import time
import traceback

mode = sys.argv[1]
proto = sys.argv[2]
path = sys.argv[3]
result = sys.argv[4]
os.environ["HC_TERMINAL_ID"] = "t7"
os.environ["HERDR_ENV"] = ""
os.environ["HERDR_PANE_ID"] = ""
os.environ["HERDR_KIRO_LOG"] = path + ".log"
os.environ["HERDR_BIN_PATH"] = os.path.join(os.path.dirname(path), "missing-herdr")
os.environ["XDG_STATE_HOME"] = os.path.dirname(path)


def _pidfile(name):
    with open(result + "." + name, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


if mode == "owner":
    _pidfile("owner")
    go = result + ".go"
    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(go):
        time.sleep(0.02)
    proc = subprocess.Popen(
        [sys.executable, __file__, "hook", proto, path, result],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raise SystemExit(proc.wait(timeout=20))

if mode == "hook":
    _pidfile("hook")
    proc = subprocess.Popen(
        [sys.executable, __file__, "watch", proto, path, result],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with open(result + ".watch", "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    raise SystemExit(proc.wait(timeout=20))

sys.path.insert(0, proto)
try:
    import herdr_kiro_state as hook

    hook._watch_loop("t7", path)
except Exception:
    with open(path + ".crash", "w", encoding="utf-8") as handle:
        handle.write(traceback.format_exc())
    raise
"""


class AncestorReportTests(unittest.TestCase):
    def _status(self, kind):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-ancestor-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        result = os.path.join(directory, "result")
        driver = os.path.join(directory, "driver.py")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(_ANCESTOR_DRIVER)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        # macOS drops unread pty output when the last slave fd closes.
        self.addCleanup(os.close, slave)
        err_path = os.path.join(directory, "owner.err")
        with open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [sys.executable, driver, "owner", os.path.dirname(os.path.abspath(hook.__file__)), path, result],
                stdin=subprocess.DEVNULL,
                stdout=slave,
                stderr=err,
            )
        self.addCleanup(_stop, proc)
        self.addCleanup(_kill_pidfiles, result)
        owner_pid = 0
        deadline = time.time() + 5
        while time.time() < deadline:
            owner_pid = _read_pidfile(result + ".owner")
            if owner_pid > 1:
                break
            time.sleep(0.02)
        self.assertGreater(owner_pid, 1)
        token = _live_start(owner_pid) if kind == "match" else ""
        ident = hook.hires_start(owner_pid) if kind == "match" else ""
        fine = kind == "match" and hook.fine_start(ident)
        # update() normalizes with this process's ps cache. A stale cache
        # treats the new owner as dead and drops it before the watcher starts.
        hook.begin_tick()

        def seed(state):
            state["watcher"] = 0
            state["generation"] = 3
            state["busy"] = {"sess": time.time()}
            state["owners"] = {} if kind == "chat" else {"sess": owner_pid}
            state["owner_started"] = {} if kind != "match" else {"sess": token}
            state["owner_ident"] = {} if not fine else {"sess": ident}
            state["chat_pid"] = owner_pid if kind == "chat" else 0
            state["chat_started"] = ""
            state["reported"] = None
            state["seq"] = 4

        hook.update(path, seed)
        with open(result + ".go", "w", encoding="utf-8") as handle:
            handle.write("go\n")
        log_path = path + ".log"
        crash_path = path + ".crash"
        logged = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            if os.path.exists(crash_path):
                with open(crash_path, encoding="utf-8") as handle:
                    self.fail(handle.read())
            if os.path.exists(log_path):
                with open(log_path, encoding="utf-8") as handle:
                    logged = handle.read()
            # "watcher exit osc missed" also contains " osc ". Wait for a
            # status line from emit_osc, which names the state. An empty
            # token never becomes that attempt: the watcher has started and
            # the pty stays empty.
            if "osc missed " in logged or "osc working via" in logged or "osc done" in logged:
                break
            # A sec: token is not an identity. The watcher starts and the pty
            # stays empty, the same as an empty token.
            if not fine and "watcher loop" in logged:
                break
            time.sleep(0.05)
        else:
            detail = logged
            if os.path.exists(err_path):
                with open(err_path, encoding="utf-8") as handle:
                    detail = detail + handle.read()
            self.fail("no osc attempt\n" + detail)
        if not fine:
            time.sleep(0.3)
        got = _read_ready(master, 0.3)

        def steal(state):
            state["watcher"] = 42424242

        hook.update(path, steal)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _kill_pidfiles(result)
            proc.kill()
            proc.wait(timeout=5)
        return got, logged, os.ttyname(slave), ident

    def test_empty_owner_token_on_a_live_ancestor_stays_empty(self):
        # setsid watcher, hook still alive, owner is the hook's parent.
        # owner_started is empty. That pty must get no status bytes.
        got, logged, device, _ident = self._status("owner")
        self.assertEqual(got, b"", logged)
        self.assertNotIn(b"state=working", got)
        self.assertNotIn(b"state=done", got)
        self.assertNotIn("osc working via", logged)
        self.assertNotIn("osc done", logged)
        self.assertNotIn(device, logged)

    def test_empty_chat_token_on_a_live_ancestor_stays_empty(self):
        # Same tree. The pid is chat_pid and chat_started is empty.
        got, logged, device, _ident = self._status("chat")
        self.assertEqual(got, b"", logged)
        self.assertNotIn(b"state=working", got)
        self.assertNotIn(b"state=done", got)
        self.assertNotIn("osc working via", logged)
        self.assertNotIn("osc done", logged)
        self.assertNotIn(device, logged)

    def test_matching_ancestor_token_receives_working(self):
        # A fine start token that matches still goes through write_to_pane,
        # including when that pid is an ancestor of the setsid watcher.
        # sec: plus lstart is not that token, so the pty stays empty.
        got, logged, device, ident = self._status("match")
        if hook.fine_start(ident):
            self.assertIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", got, logged)
            self.assertNotIn(b"state=done", got)
            self.assertIn("osc working via ", logged)
            self.assertIn(device, logged)
            return
        self.assertEqual(got, b"", logged)
        self.assertNotIn(b"state=working", got)
        self.assertNotIn(b"state=done", got)
        self.assertNotIn("osc working via", logged)
        self.assertNotIn("osc done", logged)
        self.assertNotIn(device, logged)


# The recorded pid sits between the setsid watcher and a parent that has its
# own pty. A second pty is on the grandparent. send_report stays real.
_ABOVE_DRIVER = """\
import os
import subprocess
import sys
import time
import traceback

mode = sys.argv[1]
proto = sys.argv[2]
path = sys.argv[3]
result = sys.argv[4]
os.environ["HC_TERMINAL_ID"] = "t7"
os.environ["HERDR_ENV"] = ""
os.environ["HERDR_PANE_ID"] = ""
os.environ["HERDR_KIRO_LOG"] = path + ".log"
os.environ["HERDR_BIN_PATH"] = os.path.join(os.path.dirname(path), "missing-herdr")
os.environ["XDG_STATE_HOME"] = os.path.dirname(path)


def _pidfile(name):
    with open(result + "." + name, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def _wait_go():
    go = result + ".go"
    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(go):
        time.sleep(0.02)


if mode == "grand":
    _pidfile("grand")
    parent_fd = int(sys.argv[5])
    mid_fd = int(sys.argv[6])
    os.set_inheritable(parent_fd, True)
    os.set_inheritable(mid_fd, True)
    proc = subprocess.Popen(
        [sys.executable, __file__, "parent", proto, path, result, str(mid_fd)],
        stdin=subprocess.DEVNULL,
        stdout=parent_fd,
        stderr=subprocess.DEVNULL,
        pass_fds=(mid_fd,),
    )
    raise SystemExit(proc.wait(timeout=20))

if mode == "parent":
    _pidfile("parent")
    mid_fd = int(sys.argv[5])
    os.set_inheritable(mid_fd, True)
    proc = subprocess.Popen(
        [sys.executable, __file__, "mid", proto, path, result],
        stdin=subprocess.DEVNULL,
        stdout=mid_fd,
        stderr=subprocess.DEVNULL,
    )
    raise SystemExit(proc.wait(timeout=20))

if mode == "mid":
    _pidfile("mid")
    _wait_go()
    proc = subprocess.Popen(
        [sys.executable, __file__, "hook", proto, path, result],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raise SystemExit(proc.wait(timeout=20))

if mode == "hook":
    _pidfile("hook")
    proc = subprocess.Popen(
        [sys.executable, __file__, "watch", proto, path, result],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with open(result + ".watch", "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    raise SystemExit(proc.wait(timeout=20))

sys.path.insert(0, proto)
try:
    import herdr_kiro_state as hook

    hook._watch_loop("t7", path)
except Exception:
    with open(path + ".crash", "w", encoding="utf-8") as handle:
        handle.write(traceback.format_exc())
    raise
"""

_ABOVE_PIDS = (".watch", ".hook", ".mid", ".parent", ".grand")


class AboveSkippedPidTests(unittest.TestCase):
    def _report(self, kind):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-above-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        result = os.path.join(directory, "result")
        driver = os.path.join(directory, "driver.py")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(_ABOVE_DRIVER)
        grand_master, grand_slave = os.openpty()
        parent_master, parent_slave = os.openpty()
        mid_master, mid_slave = os.openpty()
        for fd in (grand_master, grand_slave, parent_master, parent_slave, mid_master, mid_slave):
            self.addCleanup(os.close, fd)
        err_path = os.path.join(directory, "grand.err")
        proto = os.path.dirname(os.path.abspath(hook.__file__))
        with open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [sys.executable, driver, "grand", proto, path, result, str(parent_slave), str(mid_slave)],
                stdin=subprocess.DEVNULL,
                stdout=grand_slave,
                stderr=err,
                pass_fds=(parent_slave, mid_slave),
            )
        self.addCleanup(_stop, proc)
        self.addCleanup(_kill_pidfiles, result, _ABOVE_PIDS)
        mid_pid = 0
        deadline = time.time() + 5
        while time.time() < deadline:
            mid_pid = _read_pidfile(result + ".mid")
            if mid_pid > 1:
                break
            time.sleep(0.02)
        self.assertGreater(mid_pid, 1)
        token = _live_start(mid_pid) if kind == "match" else ""
        ident = hook.hires_start(mid_pid) if kind == "match" else ""
        fine = kind == "match" and hook.fine_start(ident)
        hook.begin_tick()

        def seed(state):
            state["watcher"] = 0
            state["generation"] = 3
            state["busy"] = {"sess": time.time()}
            state["owners"] = {} if kind == "chat" else {"sess": mid_pid}
            state["owner_started"] = {} if kind != "match" else {"sess": token}
            state["owner_ident"] = {} if not fine else {"sess": ident}
            state["chat_pid"] = mid_pid if kind == "chat" else 0
            state["chat_started"] = ""
            state["reported"] = None
            state["seq"] = 4

        hook.update(path, seed)
        with open(result + ".go", "w", encoding="utf-8") as handle:
            handle.write("go\n")
        log_path = path + ".log"
        crash_path = path + ".crash"
        logged = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            if os.path.exists(crash_path):
                with open(crash_path, encoding="utf-8") as handle:
                    self.fail(handle.read())
            if os.path.exists(log_path):
                with open(log_path, encoding="utf-8") as handle:
                    logged = handle.read()
            if "osc missed " in logged or "osc working via" in logged or "osc done" in logged:
                break
            if not fine and "watcher loop" in logged:
                break
            time.sleep(0.05)
        else:
            detail = logged
            if os.path.exists(err_path):
                with open(err_path, encoding="utf-8") as handle:
                    detail = detail + handle.read()
            self.fail("no osc attempt\n" + detail)
        if not fine:
            time.sleep(0.3)
        parent_got = _read_ready(parent_master, 0.3)
        grand_got = _read_ready(grand_master, 0.3)
        mid_got = _read_ready(mid_master, 0.3)

        def steal(state):
            state["watcher"] = 42424242

        hook.update(path, steal)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _kill_pidfiles(result, _ABOVE_PIDS)
            proc.kill()
            proc.wait(timeout=5)
        return parent_got, grand_got, mid_got, logged, os.ttyname(parent_slave), os.ttyname(mid_slave), ident

    def test_empty_owner_token_does_not_write_the_parent_pty(self):
        # The recorded pid is in the middle. Its parent holds a different pty.
        # An empty owner_started must not paint that parent, or the grandparent.
        parent_got, grand_got, mid_got, logged, parent_dev, _mid_dev, _ident = self._report("owner")
        self.assertEqual(parent_got, b"", logged)
        self.assertEqual(grand_got, b"", logged)
        self.assertEqual(mid_got, b"", logged)
        self.assertNotIn("osc working via", logged)
        self.assertNotIn("osc done", logged)
        self.assertNotIn(parent_dev, logged)

    def test_empty_chat_token_does_not_write_the_parent_pty(self):
        # No owner row. chat_pid is that middle process and chat_started is empty.
        parent_got, grand_got, mid_got, logged, parent_dev, _mid_dev, _ident = self._report("chat")
        self.assertEqual(parent_got, b"", logged)
        self.assertEqual(grand_got, b"", logged)
        self.assertEqual(mid_got, b"", logged)
        self.assertNotIn("osc working via", logged)
        self.assertNotIn("osc done", logged)
        self.assertNotIn(parent_dev, logged)

    def test_matching_token_writes_its_pty_not_the_parent(self):
        # A fine matching token still writes that pid's pty, and not the parent's.
        # sec: plus lstart writes neither.
        parent_got, grand_got, mid_got, logged, parent_dev, mid_dev, ident = self._report("match")
        self.assertEqual(parent_got, b"", logged)
        self.assertEqual(grand_got, b"", logged)
        self.assertNotIn(parent_dev, logged)
        if hook.fine_start(ident):
            self.assertIn(b"\x1b]7501;state=working:app=kiro-cli\x1b\\", mid_got, logged)
            self.assertIn(mid_dev, logged)
            return
        self.assertEqual(mid_got, b"", logged)
        self.assertNotIn(b"state=working", mid_got)
        self.assertNotIn(b"state=done", mid_got)
        self.assertNotIn("osc working via", logged)


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
