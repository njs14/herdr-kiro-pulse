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
        with open(previous + ".original", "rb") as handle:
            self.assertEqual(handle.read(), b'{"version":"v1","hooks":[]}\n')

    def _home_env(self, home):
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
        env["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
        env.pop("KIRO_HOOKS_DIR", None)
        env.pop("HERDR_SESSION", None)
        return env

    def _files(self, home):
        found = {}
        for dirpath, _dirs, names in os.walk(home):
            for name in names:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    found[os.path.relpath(path, home)] = handle.read()
        return found

    def _round_trip(self, seed):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        for relative, payload in seed.items():
            path = os.path.join(home, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(payload)
        before = self._files(home)
        env = self._home_env(home)
        for _ in range(2):
            completed = subprocess.run(
                ["sh", os.path.join(ROOT, "install.sh")],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(self._files(home), before)

    def test_install_twice_then_uninstall_on_a_fresh_home(self):
        self._round_trip({})

    def test_install_twice_then_uninstall_restores_user_originals(self):
        self._round_trip(
            {
                os.path.join(".kiro", "hooks", "herdr-kiro.json"): b'{"user":"hook"}\n',
                os.path.join(".config", "herdr", "agent-detection", "kiro.toml"): b'id = "kiro"\n# user rules\n',
            }
        )

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
        script = os.path.join(base, "herdr_kiro_state.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import time\ntime.sleep(60)\n")
        watcher = subprocess.Popen([sys.executable, script, "--watch", "w1:p1", state_file])
        decoy = subprocess.Popen([sys.executable, script, "--watch", "other", state_file])
        self.addCleanup(lambda: watcher.poll() is not None or watcher.kill())
        self.addCleanup(lambda: decoy.poll() is not None or decoy.kill())
        started = subprocess.check_output(
            ["ps", "-p", str(watcher.pid), "-o", "lstart="], text=True
        )
        collapsed = " ".join(started.split())
        pid_dir = os.path.join(state, "pids")
        os.makedirs(pid_dir)
        with open(os.path.join(pid_dir, str(watcher.pid)), "w", encoding="utf-8") as handle:
            handle.write("%s\n%s\n" % (os.getuid(), collapsed))
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
            self.assertEqual(handle.read(), "{}\n")
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
