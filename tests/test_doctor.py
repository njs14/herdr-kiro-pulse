"""doctor exits non-zero when the install is not healthy."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prototype"))
import herdr_kiro_state as hook


class _Timed:
    """A completed doctor run plus how long it took."""

    def __init__(self, completed: subprocess.CompletedProcess, elapsed: float) -> None:
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.elapsed = elapsed


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = os.path.join(self.base, "home")
        self.bindir = os.path.join(self.base, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bindir)
        settings = os.path.join(self.home, ".kiro", "settings")
        os.makedirs(settings)
        with open(os.path.join(settings, "cli.json"), "w", encoding="utf-8") as handle:
            json.dump({"chat.agentEngine": "v3", "chat.enableWorkflows": True}, handle)
        self.env = os.environ.copy()
        self.env["HOME"] = self.home
        self.env["XDG_CONFIG_HOME"] = os.path.join(self.home, ".config")
        self.env["XDG_STATE_HOME"] = os.path.join(self.home, ".local", "state")
        self.env["PATH"] = self.bindir + os.pathsep + self.env.get("PATH", "")
        self.env.pop("HERDR_BIN_PATH", None)
        self.env.pop("HERDR_SESSION", None)

    def write_herdr(self, remote="2026.09.19.1", server=True, plugin=True):
        server_body = '{"result":{"panes":[]}}' if server else '{"error":{"code":"server_not_running"}}'
        server_rc = "0" if server else "1"
        plugin_body = "- herdr-kiro" if plugin else "No plugins installed."
        script = f"""#!/bin/sh
case "$1" in
  --version) echo herdr 0.9.5; exit 0 ;;
  pane) echo '{server_body}'; exit {server_rc} ;;
  plugin) echo '{plugin_body}'; exit 0 ;;
  agent) echo '{{"cached_remote_version":"{remote}","local_override_shadowing_remote":true}}'; exit 0 ;;
esac
exit 0
"""
        path = os.path.join(self.bindir, "herdr")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(path, stat.S_IRWXU)

    def write_kiro(self, slow=False, engine="v3", workflows="true"):
        if slow:
            body = '#!/bin/sh\nif [ "$1" = --version ]; then echo kiro-cli 2.37.0; exit 0; fi\nsleep 5\nexit 0\n'
        else:
            body = (
                "#!/bin/sh\n"
                'if [ "$1" = --version ]; then echo kiro-cli 2.37.0; exit 0; fi\n'
                f'if [ "$2" = chat.agentEngine ]; then echo {engine}; exit 0; fi\n'
                f'if [ "$2" = chat.enableWorkflows ]; then echo {workflows}; exit 0; fi\n'
                "exit 0\n"
            )
        path = os.path.join(self.bindir, "kiro-cli")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, stat.S_IRWXU)

    def install(self):
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def doctor(self):
        started = time.time()
        completed = subprocess.run(
            ["python3", os.path.join(ROOT, "prototype", "herdr_kiro_doctor.py")],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        return _Timed(completed, time.time() - started)

    def test_healthy_install_exits_zero(self):
        self.write_herdr()
        self.write_kiro()
        self.install()
        completed = self.doctor()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("problems: 0", completed.stdout)

    def test_newer_upstream_is_a_failure(self):
        self.write_herdr(remote="2026.11.01.9")
        self.write_kiro()
        self.install()
        completed = self.doctor()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("2026.11.01.9", completed.stdout + completed.stderr)
        self.assertIn("2026.09.19.1", completed.stdout + completed.stderr)

    def test_hand_edit_is_a_failure(self):
        self.write_herdr()
        self.write_kiro()
        self.install()
        hook = os.path.join(self.home, ".kiro", "hooks", "herdr-kiro.json")
        with open(hook, "ab") as handle:
            handle.write(b"\n")
        completed = self.doctor()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("hand edit", completed.stdout)

    def test_dead_pidfile_is_a_failure_and_is_removed(self):
        self.write_herdr()
        self.write_kiro()
        self.install()
        pid_dir = os.path.join(self.home, ".local", "state", "herdr-kiro", "pids")
        os.makedirs(pid_dir)
        stale = os.path.join(pid_dir, "999999")
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write("1\n\n")
        completed = self.doctor()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("stale watcher", completed.stdout)
        self.assertFalse(os.path.exists(stale))

    def test_stopped_server_is_a_failure(self):
        self.write_herdr(server=False)
        self.write_kiro()
        self.install()
        completed = self.doctor()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("server is not running", completed.stdout)

    def test_settings_timeout_uses_the_file_and_stays_short(self):
        self.write_herdr()
        self.write_kiro(slow=True)
        self.install()
        completed = self.doctor()
        self.assertLess(completed.elapsed, 6)
        self.assertIn("v3", completed.stdout)
        self.assertIn("settings file", completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_uninstalled_marker_with_files_is_a_failure(self):
        self.write_herdr()
        self.write_kiro()
        self.install()
        lifecycle = os.path.join(self.home, ".local", "state", "herdr-kiro", "lifecycle")
        with open(lifecycle, "w", encoding="utf-8") as handle:
            handle.write("uninstalled\n")
        completed = self.doctor()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("files remain", completed.stdout)

    def test_unlinked_plugin_leaves_files_flagged(self):
        self.write_herdr(plugin=False)
        self.write_kiro()
        self.env["HERDR_PLUGIN_ID"] = "herdr-kiro"
        self.install()
        completed = self.doctor()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unlink does not delete files", completed.stdout)

    def test_a_user_rules_file_is_not_a_problem(self):
        self.write_herdr()
        self.write_kiro()
        rules = os.path.join(self.home, ".config", "herdr", "agent-detection")
        os.makedirs(rules)
        with open(os.path.join(rules, "kiro.toml"), "w", encoding="utf-8") as handle:
            handle.write('id = "kiro"\n# user rules\n')
        completed = self.doctor()
        self.assertNotIn("uninstall.sh", completed.stdout)
        self.assertIn("user file", completed.stdout)

    def test_eperm_pidfile_is_left_alone(self):
        import errno
        from unittest import mock

        sys.path.insert(0, os.path.join(ROOT, "prototype"))
        import herdr_kiro_doctor as doctor

        def kill(_pid, _sig):
            raise OSError(errno.EPERM, "not permitted")

        pid_dir = os.path.join(self.home, "pids")
        os.makedirs(pid_dir)
        stale = os.path.join(pid_dir, "42")
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write("1\n\n")
        with mock.patch.object(doctor.os, "kill", kill):
            self.assertEqual(doctor.dead_pidfiles(pid_dir), [])
        self.assertTrue(os.path.exists(stale))

    def test_substring_without_a_marker_is_a_user_file(self):
        self.write_herdr()
        self.write_kiro()
        rules = os.path.join(self.home, ".config", "herdr", "agent-detection")
        os.makedirs(rules)
        path = os.path.join(rules, "kiro.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('id = "workflow_banner_working"\n')
        completed = self.doctor()
        self.assertIn("user file", completed.stdout)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("workflow_banner_working", handle.read())
        self.assertEqual(hook.ownership(path), "absent")


if __name__ == "__main__":
    unittest.main()
