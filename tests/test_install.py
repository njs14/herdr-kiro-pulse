"""install.sh writes a hook file the shell can run, including odd paths."""
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InstallTests(unittest.TestCase):
    def test_install_quotes_a_path_with_space_quote_and_backslash(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        weird = os.path.join(base, "dir with space", 'qu"ote', "back\\slash")
        os.makedirs(os.path.join(weird, "prototype"))
        shutil.copy(os.path.join(ROOT, "install.sh"), weird)
        hook_path = os.path.join(weird, "prototype", "herdr_kiro_state.py")
        with open(hook_path, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env python3\n")
        os.chmod(hook_path, os.stat(hook_path).st_mode | stat.S_IEXEC)
        home = os.path.join(base, "home")
        os.makedirs(home)
        existing = os.path.join(home, ".kiro", "hooks")
        os.makedirs(existing)
        previous = os.path.join(existing, "herdr-kiro.json")
        with open(previous, "w", encoding="utf-8") as handle:
            handle.write('{"version":"v1","hooks":[]}\n')
        env = dict(os.environ)
        env["HOME"] = home
        env.pop("KIRO_HOOKS_DIR", None)
        completed = subprocess.run(
            ["sh", os.path.join(weird, "install.sh")],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(previous, encoding="utf-8") as handle:
            document = json.loads(handle.read())
        commands = [item["action"]["command"] for item in document["hooks"]]
        self.assertEqual(len(commands), 5)
        for event, command in zip(
            ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"],
            commands,
        ):
            parts = shlex.split(command)
            self.assertEqual(parts, [hook_path, event])
        backups = [name for name in os.listdir(existing) if name.startswith("herdr-kiro.json.bak")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(existing, backups[0]), encoding="utf-8") as handle:
            saved = handle.read()
        self.assertIn('"hooks":[]', saved.replace(" ", ""))

    def test_uninstall_removes_hook_kills_watcher_and_cleans_state(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        hooks = os.path.join(home, ".kiro", "hooks")
        os.makedirs(hooks)
        hook_file = os.path.join(hooks, "herdr-kiro.json")
        with open(hook_file, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        state = os.path.join(home, ".local", "state", "herdr-kiro")
        os.makedirs(state)
        state_file = os.path.join(state, "w1_p1.json")
        with open(state_file, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        legacy_dir = os.path.join(base, "legacy")
        os.makedirs(legacy_dir)
        legacy = os.path.join(legacy_dir, "herdr-kiro-hook.log")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("legacy\n")
        legacy_state = os.path.join(legacy_dir, "herdr-kiro-w1_p1.json")
        with open(legacy_state, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        seq_file = os.path.join(state, "w1_p1.json.seq")
        with open(seq_file, "w", encoding="utf-8") as handle:
            handle.write("3000000\n")
        newest = os.path.join(hooks, "herdr-kiro.json.bak.20260102000000")
        older = os.path.join(hooks, "herdr-kiro.json.bak.20200101000000")
        with open(newest, "w", encoding="utf-8") as handle:
            handle.write('{"restored":true}\n')
        with open(older, "w", encoding="utf-8") as handle:
            handle.write('{"old":true}\n')
        script = os.path.join(base, "herdr_kiro_state.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import time\ntime.sleep(60)\n")
        watcher = subprocess.Popen([sys.executable, script, "--watch", "w1:p1", state_file])
        decoy = subprocess.Popen([sys.executable, script, "--watch", "other", state_file])
        self.addCleanup(lambda: watcher.poll() is not None or watcher.kill())
        self.addCleanup(lambda: decoy.poll() is not None or decoy.kill())
        started = subprocess.check_output(
            ["ps", "-p", str(watcher.pid), "-o", "lstart="], text=True
        ).strip()
        pid_dir = os.path.join(state, "pids")
        os.makedirs(pid_dir)
        with open(os.path.join(pid_dir, str(watcher.pid)), "w", encoding="utf-8") as handle:
            handle.write("%s\n%s\n" % (os.getuid(), started))
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
        env["HERDR_KIRO_LEGACY_DIR"] = legacy_dir
        env.pop("KIRO_HOOKS_DIR", None)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(os.path.join(ROOT, "uninstall.sh"), encoding="utf-8") as handle:
            self.assertNotIn("/proc", handle.read())
        with open(hook_file, encoding="utf-8") as handle:
            self.assertIn("restored", handle.read())
        self.assertFalse(os.path.exists(newest))
        self.assertFalse(os.path.exists(older))
        self.assertFalse(os.path.exists(state_file))
        self.assertTrue(os.path.exists(seq_file))
        self.assertFalse(os.path.exists(legacy))
        self.assertFalse(os.path.exists(legacy_state))
        watcher.wait(timeout=5)
        self.assertIsNotNone(watcher.returncode)
        self.assertIsNone(decoy.poll())
        decoy.kill()
        decoy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
