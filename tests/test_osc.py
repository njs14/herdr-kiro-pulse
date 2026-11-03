"""OSC 7501 is the Herdr App status write. hc has no status-set command.

A Kiro hook may run with stdout captured and with no controlling terminal.
The bytes have to land on the pane pty, which is the owner's stdout, and
the captured pipe must stay empty.
"""

import os
import select
import shutil
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
