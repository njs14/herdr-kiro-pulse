"""install.sh writes a hook file the shell can run, including odd paths."""

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prototype"))
import herdr_kiro_state as hook


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
        env.pop("HERDR_SESSION", None)
        completed = subprocess.run(
            ["sh", os.path.join(weird, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(previous, encoding="utf-8") as handle:
            document = json.loads(handle.read())
        commands = [item["action"]["command"] for item in document["hooks"]]
        self.assertEqual(len(commands), 6)
        for event, command in zip(
            ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"],
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
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        after = self._files(home)
        marker = after.pop(".local/state/herdr-kiro/lifecycle", None)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.splitlines()[0], b"uninstalled")
        # herdr writes this lock when uninstall asks it to list plugins. It is herdr's file.
        after.pop(".config/herdr/.plugins.lock", None)
        before.pop(".config/herdr/.plugins.lock", None)
        self.assertEqual(after, before)

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
        tmp = os.path.join(base, "tmp")
        os.makedirs(tmp)
        legacy = os.path.join(tmp, "herdr-kiro-hook.log")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("legacy\n")
        legacy_state = os.path.join(tmp, "herdr-kiro-w1_p1.json")
        with open(legacy_state, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        marked = os.path.join(tmp, "herdr-kiro-marked.json")
        with open(marked, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(marked + ".herdr-kiro-owned", "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        decoy_root = os.path.join(base, "not-a-sweep-root")
        os.makedirs(decoy_root)
        decoy_marked = os.path.join(decoy_root, "herdr-kiro-decoy.json")
        with open(decoy_marked, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(decoy_marked + ".herdr-kiro-owned", "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        seq_file = os.path.join(state, "w1_p1.json.seq")
        with open(seq_file, "w", encoding="utf-8") as handle:
            handle.write("3000000\n")
        script = os.path.join(base, "herdr_kiro_state.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import time\ntime.sleep(60)\n")
        watcher = subprocess.Popen([sys.executable, script, "--watch", "w1:p1", state_file])
        decoy = subprocess.Popen([sys.executable, script, "--watch", "other", state_file])
        wrong_start = subprocess.Popen([sys.executable, script, "--watch", "start", state_file])
        wrong_uid = subprocess.Popen([sys.executable, script, "--watch", "uid", state_file])
        reuse = subprocess.Popen([sys.executable, script, "--watch", "reuse", state_file])
        self.addCleanup(lambda: watcher.poll() is not None or watcher.kill())
        self.addCleanup(lambda: decoy.poll() is not None or decoy.kill())
        self.addCleanup(lambda: wrong_start.poll() is not None or wrong_start.kill())
        self.addCleanup(lambda: wrong_uid.poll() is not None or wrong_uid.kill())
        self.addCleanup(lambda: reuse.poll() is not None or reuse.kill())
        hook.begin_tick()
        ident = hook.watcher_identity(watcher.pid)
        self.assertIsNotNone(ident)
        # /proc start ticks are fractional seconds. Where those ticks are absent, the token is sec: plus whole-second lstart.
        if hook._linux_start_ticks(watcher.pid):
            self.assertRegex(ident["start"], r"^\d+\.\d+$")
        else:
            self.assertRegex(ident["start"], r"^sec:\d+$")
        decoy_ident = hook.watcher_identity(decoy.pid)
        start_ident = hook.watcher_identity(wrong_start.pid)
        uid_ident = hook.watcher_identity(wrong_uid.pid)
        self.assertIsNotNone(decoy_ident)
        self.assertIsNotNone(start_ident)
        self.assertIsNotNone(uid_ident)
        other_uid = 0 if os.getuid() != 0 else 1
        pid_dir = os.path.join(state, "pids")
        os.makedirs(pid_dir)
        with open(os.path.join(pid_dir, str(watcher.pid)), "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n{}\n".format(os.getuid(), ident["start"], ident["comm"]))
        with open(os.path.join(pid_dir, str(decoy.pid)), "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n{}\n".format(os.getuid(), decoy_ident["start"], "not-the-comm"))
        with open(os.path.join(pid_dir, str(wrong_start.pid)), "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n{}\n".format(os.getuid(), "1", start_ident["comm"]))
        with open(os.path.join(pid_dir, str(wrong_uid.pid)), "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n{}\n".format(other_uid, uid_ident["start"], uid_ident["comm"]))
        reuse_ident = hook.watcher_identity(reuse.pid)
        self.assertIsNotNone(reuse_ident)
        hook.begin_tick()
        reuse_snap = hook.process_snapshot(reuse.pid) or {}
        reuse_lstart = reuse_snap.get("lstart") or "1"
        with open(os.path.join(pid_dir, str(reuse.pid)), "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n{}\n".format(os.getuid(), "sec:" + str(reuse_lstart), reuse_ident["comm"]))
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
        env["TMPDIR"] = tmp
        env["HERDR_KIRO_LEGACY_DIR"] = decoy_root
        env.pop("KIRO_HOOKS_DIR", None)
        env.pop("HERDR_SESSION", None)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(os.path.join(ROOT, "uninstall.sh"), encoding="utf-8") as handle:
            self.assertNotIn("/proc", handle.read())
        with open(hook_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{}\n")
        self.assertFalse(os.path.exists(state_file))
        self.assertTrue(os.path.exists(seq_file))
        self.assertTrue(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy_state))
        self.assertFalse(os.path.exists(marked))
        self.assertTrue(os.path.exists(decoy_marked))
        watcher.wait(timeout=5)
        self.assertIsNotNone(watcher.returncode)
        self.assertIsNone(decoy.poll())
        self.assertIsNone(wrong_start.poll())
        self.assertIsNone(wrong_uid.poll())
        self.assertIsNone(reuse.poll())
        decoy.kill()
        wrong_start.kill()
        wrong_uid.kill()
        reuse.kill()
        decoy.wait(timeout=5)
        wrong_start.wait(timeout=5)
        wrong_uid.wait(timeout=5)
        reuse.wait(timeout=5)

    def test_uninstall_keeps_a_user_edited_file(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        hook_file = os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")
        with open(hook_file, "ab") as handle:
            handle.write(b"# user edit\n")
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        matches = [
            name
            for name in os.listdir(os.path.dirname(hook_file))
            if name.startswith(os.path.basename(hook_file) + ".user-modified.")
        ]
        self.assertEqual(len(matches), 1)
        with open(os.path.join(os.path.dirname(hook_file), matches[0]), "rb") as handle:
            self.assertIn(b"# user edit\n", handle.read())
        self.assertFalse(os.path.exists(hook_file))
        self.assertFalse(os.path.exists(hook_file + ".herdr-kiro-owned"))

    def test_install_backs_up_a_user_replacement(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        hook_file = os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")
        with open(hook_file + ".original", "wb") as handle:
            handle.write(b'{"user":"original"}\n')
        os.remove(hook_file + ".herdr-kiro-owned")
        with open(hook_file, "wb") as handle:
            handle.write(b'{"user":"replacement"}\n')
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(hook_file + ".original", "rb") as handle:
            self.assertEqual(handle.read(), b'{"user":"original"}\n')
        matches = [
            name for name in os.listdir(os.path.dirname(hook_file)) if name.startswith("herdr-kiro.json.user-modified.")
        ]
        self.assertEqual(len(matches), 1)
        with open(os.path.join(os.path.dirname(hook_file), matches[0]), "rb") as handle:
            self.assertEqual(handle.read(), b'{"user":"replacement"}\n')
        with open(hook_file, encoding="utf-8") as handle:
            self.assertIn("herdr-kiro-SessionStart", handle.read())

    def test_v4_marker_without_a_hash_upgrades_in_place(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        hook_dir = os.path.join(home, ".kiro", "hooks")
        os.makedirs(hook_dir)
        hook_file = os.path.join(hook_dir, "herdr-kiro.json")
        with open(hook_file, "wb") as handle:
            handle.write(b'{"version":"v4"}\n')
        with open(hook_file + ".herdr-kiro-owned", "w", encoding="utf-8") as handle:
            handle.write("herdr-kiro-owned\n")
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("no content hash", completed.stderr)
        with open(hook_file, "rb") as handle:
            self.assertEqual(handle.read(), b'{"version":"v4"}\n')
        backups = [name for name in os.listdir(hook_dir) if name.startswith("herdr-kiro.json.user-modified.")]
        self.assertEqual(len(backups), 1)

    def test_lost_marker_keeps_a_hand_edit(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        hook_file = os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")
        os.remove(hook_file + ".herdr-kiro-owned")
        with open(hook_file, "ab") as handle:
            handle.write(b"\n# edited\n")
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(hook_file + ".original", "rb") as handle:
            self.assertIn(b"# edited\n", handle.read())
        with open(hook_file, encoding="utf-8") as handle:
            self.assertNotIn("# edited", handle.read())

    def test_lost_marker_does_not_backup_our_own_hook(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        hook_file = os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")
        os.remove(hook_file + ".herdr-kiro-owned")
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertFalse(os.path.exists(hook_file + ".user-modified"))
        with open(hook_file, encoding="utf-8") as handle:
            self.assertIn("herdr-kiro-SessionStart", handle.read())

    def test_no_server_is_a_successful_install(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "herdr")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\necho \'{"error":{"code":"server_not_running"}}\' >&2\nexit 1\n')
        os.chmod(fake, 0o755)
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("next herdr start", completed.stderr)

    def test_unknown_session_is_not_a_missing_server(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "herdr")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\necho \'{"error":{"code":"server_not_running"}}\' >&2\nexit 1\n')
        os.chmod(fake, 0o755)
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HERDR_SESSION"] = "not-a-session"
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not-a-session", completed.stderr)
        self.assertIn("not a running herdr server", completed.stderr)

    def test_failed_reload_is_printed_and_uninstall_uses_the_session(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        log = os.path.join(base, "herdr-args")
        fake = os.path.join(bindir, "herdr")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$HERDR_FAKE_LOG"\nexit 1\n')
        os.chmod(fake, 0o755)
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HERDR_FAKE_LOG"] = log
        env["HERDR_SESSION"] = "kiro-repro"
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr or completed.stdout)
        self.assertIn("reload-agent-manifests failed", completed.stderr)
        with open(log, encoding="utf-8") as handle:
            recorded = handle.read()
        self.assertIn("--session kiro-repro server reload-agent-manifests", recorded)
        open(log, "w").close()
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr or completed.stdout)
        self.assertIn("reload-agent-manifests failed", completed.stderr)
        with open(log, encoding="utf-8") as handle:
            self.assertIn("--session kiro-repro server reload-agent-manifests", handle.read())

    def test_hashless_marker_does_not_backup_again_or_say_installed(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        hook_dir = os.path.join(home, ".kiro", "hooks")
        os.makedirs(hook_dir)
        hook_file = os.path.join(hook_dir, "herdr-kiro.json")
        with open(hook_file, "wb") as handle:
            handle.write(b'{"version":"v4"}\n')
        with open(hook_file + ".herdr-kiro-owned", "w", encoding="utf-8") as handle:
            handle.write("herdr-kiro-owned\n")
        env = self._home_env(home)
        script = os.path.join(ROOT, "install.sh")
        first = subprocess.run(["sh", script], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        second = subprocess.run(["sh", script], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        self.assertNotIn(f"Installed {hook_file}", second.stdout)
        backups = [name for name in os.listdir(hook_dir) if name.startswith("herdr-kiro.json.user-modified.")]
        self.assertEqual(len(backups), 1)
        with open(hook_file, "rb") as handle:
            self.assertEqual(handle.read(), b'{"version":"v4"}\n')

    def test_startup_leaves_an_uninstall_alone(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        state = os.path.join(home, ".local", "state", "herdr-kiro")
        os.makedirs(state)
        with open(os.path.join(state, "lifecycle"), "w", encoding="utf-8") as handle:
            handle.write("uninstalled\n")
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "startup.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("not installed", completed.stderr)
        self.assertFalse(os.path.exists(os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")))

    def test_install_uses_herdr_bin_path(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        path_herdr = os.path.join(bindir, "herdr")
        with open(path_herdr, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho path-herdr >&2\nexit 1\n")
        os.chmod(path_herdr, 0o755)
        chosen = os.path.join(base, "chosen-herdr")
        log = os.path.join(base, "log")
        with open(chosen, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$HERDR_FAKE_LOG"\nexit 0\n')
        os.chmod(chosen, 0o755)
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HERDR_BIN_PATH"] = chosen
        env["HERDR_FAKE_LOG"] = log
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertNotIn("path-herdr", completed.stderr)
        with open(log, encoding="utf-8") as handle:
            self.assertIn("reload-agent-manifests", handle.read())

    def _fake_herdr(self, base, body):
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        path = os.path.join(bindir, "herdr")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        return bindir

    def test_pending_unlink_uses_exit_code_when_the_text_differs(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = self._fake_herdr(
            base,
            "#!/bin/sh\n"
            'if [ "$1" = plugin ] && [ "$2" = list ]; then\n'
            "  echo connection refused >&2\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
        )
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env.pop("HERDR_SOCKET_PATH", None)
        env.pop("HERDR_SESSION", None)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        life = os.path.join(home, ".local", "state", "herdr-kiro", "lifecycle")
        with open(life, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("pending_unlink=herdr-kiro", text)
        self.assertNotIn("server_not_running", completed.stderr + completed.stdout)

    def test_pending_unlink_when_the_socket_is_missing(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = self._fake_herdr(base, "#!/bin/sh\nexit 0\n")
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env.pop("HERDR_SOCKET_PATH", None)
        env.pop("HERDR_SESSION", None)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        life = os.path.join(home, ".local", "state", "herdr-kiro", "lifecycle")
        with open(life, encoding="utf-8") as handle:
            self.assertIn("pending_unlink=herdr-kiro", handle.read())

    def test_no_pending_unlink_when_list_succeeds_and_the_socket_exists(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        bindir = self._fake_herdr(base, "#!/bin/sh\nexit 0\n")
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env.pop("HERDR_SESSION", None)
        sock = os.path.join(base, "herdr.sock")
        with open(sock, "w", encoding="utf-8") as handle:
            handle.write("")
        env["HERDR_SOCKET_PATH"] = sock
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        life = os.path.join(home, ".local", "state", "herdr-kiro", "lifecycle")
        with open(life, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("pending_unlink", text)

    def test_startup_keeps_pending_when_list_fails(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        state = os.path.join(home, ".local", "state", "herdr-kiro")
        os.makedirs(state)
        life = os.path.join(state, "lifecycle")
        with open(life, "w", encoding="utf-8") as handle:
            handle.write("uninstalled\npending_unlink=herdr-kiro\n")
        bindir = self._fake_herdr(base, "#!/bin/sh\necho connection refused >&2\nexit 1\n")
        env = self._home_env(home)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env.pop("HERDR_SOCKET_PATH", None)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "startup.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(life, encoding="utf-8") as handle:
            self.assertIn("pending_unlink=herdr-kiro", handle.read())
        self.assertFalse(os.path.exists(os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")))

    def test_uninstall_cleans_tmpdir_and_keeps_a_user_notes_dir(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        tmp = os.path.join(base, "tmp")
        os.makedirs(tmp)
        notes = os.path.join(tmp, "herdr-kiro-notes")
        os.makedirs(notes)
        with open(os.path.join(notes, "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("user")
        unmarked = os.path.join(tmp, "herdr-kiro-rules-999999-left")
        os.makedirs(unmarked)
        dead = subprocess.Popen(["sleep", "30"])
        dead.kill()
        dead.wait(timeout=5)
        owned = os.path.join(tmp, f"herdr-kiro-rules-{dead.pid}-ours")
        os.makedirs(owned)
        with open(os.path.join(owned, "herdr-kiro-owned"), "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        legacy = os.path.join(tmp, "herdr-kiro-hook.log")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("legacy\n")
        foreign = os.path.join(tmp, "herdr-kiro-w1_p1.json")
        with open(foreign, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(foreign + ".herdr-kiro-owned", "w", encoding="utf-8") as handle:
            handle.write("herdr-kiro-owned\nuid 0\n")
        marked = os.path.join(tmp, "herdr-kiro-ours.json")
        with open(marked, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(marked + ".herdr-kiro-owned", "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        env = self._home_env(home)
        env["TMPDIR"] = tmp
        env.pop("HERDR_KIRO_LEGACY_DIR", None)
        env.pop("HERDR_SOCKET_PATH", None)
        env.pop("HERDR_SESSION", None)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(os.path.isfile(os.path.join(notes, "note.txt")))
        self.assertTrue(os.path.isdir(unmarked))
        self.assertFalse(os.path.isdir(owned))
        self.assertTrue(os.path.exists(legacy))
        self.assertTrue(os.path.isfile(foreign))
        self.assertFalse(os.path.exists(marked))

    def test_uninstall_leaves_substring_and_hashless_files(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        hook_dir = os.path.join(home, ".kiro", "hooks")
        rules = os.path.join(home, ".config", "herdr", "agent-detection")
        os.makedirs(hook_dir)
        os.makedirs(rules)
        hook_file = os.path.join(hook_dir, "herdr-kiro.json")
        rules_file = os.path.join(rules, "kiro.toml")
        with open(hook_file, "w", encoding="utf-8") as handle:
            handle.write('{"name":"herdr-kiro-SessionStart"}\n')
        with open(rules_file, "w", encoding="utf-8") as handle:
            handle.write('id = "workflow_banner_working"\n')
        hashless = hook_file + ".herdr-kiro-owned"
        with open(hashless, "w", encoding="utf-8") as handle:
            handle.write("herdr-kiro-owned\n")
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(hook_file, encoding="utf-8") as handle:
            self.assertIn("herdr-kiro-SessionStart", handle.read())
        with open(rules_file, encoding="utf-8") as handle:
            self.assertIn("workflow_banner_working", handle.read())
        self.assertTrue(os.path.isfile(hashless))
        with open(os.path.join(ROOT, "uninstall.sh"), encoding="utf-8") as handle:
            body = handle.read().split("def remove_ours", 1)[1].split("def stop_tracked", 1)[0]
        self.assertNotIn("workflow_banner_working", body)

    def test_uninstall_renames_a_mismatch_and_restores_original(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        hook_file = os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")
        with open(hook_file + ".original", "wb") as handle:
            handle.write(b'{"user":"original"}\n')
        with open(hook_file, "ab") as handle:
            handle.write(b"# user edit\n")
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "uninstall.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        with open(hook_file, "rb") as handle:
            self.assertEqual(handle.read(), b'{"user":"original"}\n')
        matches = [
            name for name in os.listdir(os.path.dirname(hook_file)) if name.startswith("herdr-kiro.json.user-modified.")
        ]
        self.assertEqual(len(matches), 1)
        with open(os.path.join(os.path.dirname(hook_file), matches[0]), "rb") as handle:
            self.assertIn(b"# user edit\n", handle.read())
        self.assertFalse(os.path.exists(hook_file + ".herdr-kiro-owned"))

    def test_install_refuses_a_symlinked_hook(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        hooks = os.path.join(home, ".kiro", "hooks")
        os.makedirs(hooks)
        target = os.path.join(base, "target.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("keep\n")
        link = os.path.join(hooks, "herdr-kiro.json")
        os.symlink(target, link)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "install.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(f"left {link} alone", completed.stderr)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), target)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep\n")
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "herdr", "agent-detection", "kiro.toml")))

    def test_install_sets_the_owner_execute_bit_on_the_hook_path(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        tree = os.path.join(base, "tree")
        os.makedirs(os.path.join(tree, "prototype"))
        shutil.copy(os.path.join(ROOT, "install.sh"), tree)
        hook_path = os.path.join(tree, "prototype", "herdr_kiro_state.py")
        with open(hook_path, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env python3\n")
        os.chmod(hook_path, 0o644)
        completed = subprocess.run(
            ["sh", os.path.join(tree, "install.sh")],
            env=self._home_env(home),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        mode = os.stat(hook_path).st_mode
        self.assertTrue(stat.S_ISREG(mode))
        self.assertTrue(mode & stat.S_IXUSR)
        self.assertFalse(mode & stat.S_IXGRP)
        self.assertFalse(mode & stat.S_IXOTH)

    def test_install_refuses_to_chmod_a_symlinked_hook_script(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        tree = os.path.join(base, "tree")
        os.makedirs(os.path.join(tree, "prototype"))
        shutil.copy(os.path.join(ROOT, "install.sh"), tree)
        target = os.path.join(base, "real.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env python3\n")
        os.chmod(target, 0o644)
        hook_path = os.path.join(tree, "prototype", "herdr_kiro_state.py")
        os.symlink(target, hook_path)
        completed = subprocess.run(
            ["sh", os.path.join(tree, "install.sh")],
            env=self._home_env(home),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("symlink", completed.stderr)
        self.assertIn("refusing", completed.stderr)
        self.assertTrue(os.path.islink(hook_path))
        self.assertEqual(os.readlink(hook_path), target)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)
        self.assertFalse(os.path.exists(os.path.join(home, ".kiro", "hooks", "herdr-kiro.json")))

    def test_install_twice_is_idempotent(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        os.makedirs(home)
        env = self._home_env(home)
        script = os.path.join(ROOT, "install.sh")
        first = subprocess.run(["sh", script], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        snap = self._files(home)
        second = subprocess.run(["sh", script], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        self.assertEqual(self._files(home), snap)

    def test_startup_state_dir_is_private(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        state = os.path.join(home, ".local", "state", "herdr-kiro")
        os.makedirs(state, mode=0o755)
        os.chmod(state, 0o755)
        with open(os.path.join(state, "lifecycle"), "w", encoding="utf-8") as handle:
            handle.write("uninstalled\n")
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "startup.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)

    def _marked_pair(self, base):
        path = os.path.join(base, "herdr-kiro-x.json")
        payload = b"{}\n"
        with open(path, "wb") as handle:
            handle.write(payload)
        marker = path + ".herdr-kiro-owned"
        digest = hashlib.sha256(payload).hexdigest()
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(f"herdr-kiro-owned\nuid {os.getuid()}\nsha256 {digest}\n")
        os.chmod(path, 0o644)
        os.chmod(marker, 0o600)
        return path, marker, payload, digest

    def _stat_as_uid(self, info, uid):
        values = list(info)
        values[4] = uid
        return os.stat_result(tuple(values))

    def test_sidecar_owned_by_another_uid_keeps_the_file(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        path, marker, payload, digest = self._marked_pair(base)
        other = os.getuid() + 1
        real_lstat = hook.os.lstat
        real_stat = hook.os.stat

        def fake_lstat(target, *args, **kwargs):
            info = real_lstat(target, *args, **kwargs)
            if isinstance(target, str) and os.path.abspath(target) == marker:
                return self._stat_as_uid(info, other)
            return info

        def fake_stat(target, *args, **kwargs):
            info = real_stat(target, *args, **kwargs)
            if isinstance(target, str) and os.path.abspath(target) == marker:
                return self._stat_as_uid(info, other)
            return info

        with (
            mock.patch.object(hook.os, "lstat", side_effect=fake_lstat),
            mock.patch.object(hook.os, "stat", side_effect=fake_stat),
            mock.patch.object(hook, "temp_roots", return_value=[base]),
        ):
            self.assertFalse(hook._legacy_file_ours(path))
            self.assertIsNone(hook.read_owned_hash(marker))
            self.assertEqual(hook.ownership(path), "absent")
            hook.sweep_owned_temp()
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)

        os.chmod(marker, 0o666)
        self.assertFalse(hook._legacy_file_ours(path))
        self.assertIsNone(hook.read_owned_hash(marker))
        with mock.patch.object(hook, "temp_roots", return_value=[base]):
            hook.sweep_owned_temp()
        self.assertTrue(os.path.isfile(path))

        os.chmod(marker, 0o600)

        def fake_protected_lstat(target, *args, **kwargs):
            info = real_lstat(target, *args, **kwargs)
            if isinstance(target, str) and os.path.abspath(target) == path:
                return self._stat_as_uid(info, other)
            return info

        def fake_protected_stat(target, *args, **kwargs):
            info = real_stat(target, *args, **kwargs)
            if isinstance(target, str) and os.path.abspath(target) == path:
                return self._stat_as_uid(info, other)
            return info

        with (
            mock.patch.object(hook.os, "lstat", side_effect=fake_protected_lstat),
            mock.patch.object(hook.os, "stat", side_effect=fake_protected_stat),
            mock.patch.object(hook, "temp_roots", return_value=[base]),
        ):
            self.assertFalse(hook._legacy_file_ours(path))
            self.assertIsNone(hook.read_owned_hash(marker))
            hook.sweep_owned_temp()
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(hook._legacy_file_ours(path))
        self.assertEqual(hook.read_owned_hash(marker), digest)

    def test_symlinked_sidecar_keeps_the_file(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        path, marker, payload, _digest = self._marked_pair(base)
        body = marker + ".body"
        os.rename(marker, body)
        os.symlink(body, marker)
        with mock.patch.object(hook, "temp_roots", return_value=[base]):
            self.assertFalse(hook._legacy_file_ours(path))
            self.assertIsNone(hook.read_owned_hash(marker))
            self.assertEqual(hook.ownership(path), "absent")
            hook.sweep_owned_temp()
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertTrue(os.path.islink(marker))

    def test_startup_refuses_a_symlinked_state_dir(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        parent = os.path.join(home, ".local", "state")
        os.makedirs(parent)
        target = os.path.join(base, "elsewhere")
        os.makedirs(target)
        os.chmod(target, 0o755)
        state = os.path.join(parent, "herdr-kiro")
        os.symlink(target, state)
        env = self._home_env(home)
        completed = subprocess.run(
            ["sh", os.path.join(ROOT, "startup.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("symlink", completed.stderr)
        self.assertIn("refusing", completed.stderr)
        self.assertTrue(os.path.islink(state))
        self.assertEqual(os.readlink(state), target)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
