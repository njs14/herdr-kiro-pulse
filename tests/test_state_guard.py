"""The state guard rejects a script run that could reach the real config, state, or hooks dir."""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prototype"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _state_guard

_ISOLATION = _state_guard.Isolation(__name__)


def setUpModule():
    _ISOLATION.start()


def tearDownModule():
    _ISOLATION.stop()


REAL_HOME = os.path.expanduser("~")


class RunCheckTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)

    def scratch(self, extra=None, drop=()):
        env = dict(os.environ)
        env["HOME"] = os.path.join(self.base, "home")
        env["XDG_CONFIG_HOME"] = os.path.join(self.base, "home", ".config")
        env["XDG_STATE_HOME"] = os.path.join(self.base, "home", ".local", "state")
        env.pop("KIRO_HOOKS_DIR", None)
        for key in drop:
            env.pop(key, None)
        env.update(extra or {})
        return env

    def assert_rejected(self, run):
        with self.assertRaises(AssertionError):
            run()

    def test_no_xdg_config_is_rejected(self):
        self.assert_rejected(
            lambda: subprocess.run(["sh", "-c", "true"], env=self.scratch(drop=["XDG_CONFIG_HOME"]), check=False)
        )

    def test_inherited_env_is_rejected(self):
        self.assert_rejected(lambda: subprocess.run(["sh", "-c", "true"], check=False))

    def test_real_home_is_rejected(self):
        self.assert_rejected(
            lambda: subprocess.run(["sh", "-c", "true"], env=self.scratch({"HOME": REAL_HOME}), check=False)
        )

    def test_hooks_dir_in_real_home_is_rejected(self):
        hooks = os.path.join(REAL_HOME, ".kiro", "hooks")
        self.assert_rejected(
            lambda: subprocess.run(["sh", "-c", "true"], env=self.scratch({"KIRO_HOOKS_DIR": hooks}), check=False)
        )

    def test_popen_is_rejected(self):
        self.assert_rejected(
            lambda: subprocess.Popen(["sh", "-c", "true"], env=self.scratch(drop=["XDG_CONFIG_HOME"])).wait()
        )

    def test_absolute_shell_path_is_rejected(self):
        self.assert_rejected(
            lambda: subprocess.run(["/bin/sh", "-c", "true"], env=self.scratch(drop=["XDG_CONFIG_HOME"]), check=False)
        )

    def test_check_output_is_rejected(self):
        self.assert_rejected(
            lambda: subprocess.check_output(["sh", "-c", "true"], env=self.scratch(drop=["XDG_CONFIG_HOME"]))
        )

    def test_inherited_xdg_config_is_rejected(self):
        inherited = os.environ.get("XDG_CONFIG_HOME") or os.path.join(REAL_HOME, ".config")
        self.assert_rejected(
            lambda: subprocess.run(["sh", "-c", "true"], env=self.scratch({"XDG_CONFIG_HOME": inherited}), check=False)
        )

    def test_shell_behind_a_wrapper_is_rejected(self):
        env = self.scratch(drop=["XDG_CONFIG_HOME"])
        for argv in (["/usr/bin/env", "sh", "-c", "true"], ["nice", "-n", "5", "sh", "-c", "true"]):
            with self.subTest(argv=argv), self.assertRaises(AssertionError):
                subprocess.run(argv, env=env, check=False)

    def test_wrappers_are_stripped_before_the_shell_check(self):
        for argv in (
            ["/usr/bin/env", "sh", "-c", "true"],
            ["env", "FOO=1", "bash", "-c", "true"],
            ["env", "-i", "-u", "PATH", "--", "zsh", "-c", "true"],
            ["env", "-S", "sh -c true"],
            ["nice", "-n", "5", "sh"],
            ["nice", "-5", "dash"],
            ["timeout", "5", "/bin/sh"],
            ["timeout", "-s", "KILL", "--preserve-status", "5s", "ksh"],
            ["exec", "bash"],
            ["FOO=1", "BAR=2", "sh"],
            ["env", "nice", "-n", "1", "timeout", "5", "sh"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(_state_guard._is_script_run(argv, False))

    def test_plain_commands_behind_a_wrapper_stay_unchecked(self):
        for argv in (
            ["env", "git", "status"],
            ["env", "FOO=1", "true"],
            ["nice", "-n", "5", "sleep", "0"],
            ["timeout", "5", "sleep", "0"],
            ["env"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(_state_guard._is_script_run(argv, False))

    def fake_real_home(self):
        """Register a temp dir as the real home's .kiro, .config, and .local/state, so no test links the real ones."""
        real = os.path.join(self.base, "real")
        dirs = {name: os.path.join(real, name) for name in (".kiro", ".config", os.path.join(".local", "state"))}
        for path in dirs.values():
            os.makedirs(path)
        for attr, name in (("HOOK_ROOTS", ".kiro"), ("CONFIG_BASES", ".config"), ("STATE_BASES", ".local/state")):
            patcher = mock.patch.object(_state_guard, attr, (*getattr(_state_guard, attr), dirs[name]))
            patcher.start()
            self.addCleanup(patcher.stop)
        return dirs

    def symlinked_home(self, name, target):
        home = tempfile.mkdtemp(dir=self.base)
        os.makedirs(os.path.dirname(os.path.join(home, name)), exist_ok=True)
        os.symlink(target, os.path.join(home, name))
        return self.scratch(
            {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(self.base, "xc"),
                "XDG_STATE_HOME": os.path.join(self.base, "xs"),
            }
        )

    def test_home_linked_to_the_real_dirs_is_rejected(self):
        for name, target in self.fake_real_home().items():
            with self.subTest(name=name):
                env = self.symlinked_home(name, target)
                self.assertIsNotNone(_state_guard.scratch_env_problem(env))
                with self.assertRaises(AssertionError):
                    subprocess.run(["sh", "-c", "true"], env=env, check=False)

    def test_home_with_its_own_dirs_runs(self):
        self.fake_real_home()
        for name in (".kiro", ".config", os.path.join(".local", "state")):
            with self.subTest(name=name):
                own = tempfile.mkdtemp(dir=self.base)
                self.assertIsNone(_state_guard.scratch_env_problem(self.symlinked_home(name, own)))

    def test_relative_paths_are_rejected(self):
        # The guard resolves paths against this process's cwd, not the child's cwd=, so it cannot vouch for them.
        for key, value in (
            ("HOME", "."),
            ("HOME", "home"),
            ("XDG_CONFIG_HOME", ".config"),
            ("XDG_STATE_HOME", os.path.join(".local", "state")),
            ("KIRO_HOOKS_DIR", "hooks"),
            ("KIRO_HOOKS_DIR", "~/.kiro/hooks"),
        ):
            with self.subTest(key=key, value=value):
                env = self.scratch({key: value})
                self.assertIsNotNone(_state_guard.scratch_env_problem(env))
                with self.assertRaises(AssertionError):
                    subprocess.run(["sh", "-c", "true"], env=env, cwd=self.base, check=False)

    def test_scratch_shell_and_plain_commands_run(self):
        subprocess.run(["sh", "-c", "true"], env=self.scratch(), check=True)
        subprocess.run(["sleep", "0"], check=True)
        subprocess.run([sys.executable, "-c", "pass"], check=True)


class SnapshotTests(unittest.TestCase):
    def test_hooks_dirs_are_watched(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        home = os.path.join(base, "home")
        hooks = os.path.join(home, ".kiro", "hooks")
        exported = os.path.join(base, "khd")
        for path in (hooks, exported):
            os.makedirs(path)
            with open(os.path.join(path, "herdr-kiro.json"), "w", encoding="utf-8") as handle:
                handle.write('{"u":1}\n')
        code = textwrap.dedent(
            """
            import os, sys
            sys.path.insert(0, sys.argv[1])
            import _state_guard
            for path in sys.argv[2:]:
                iso = _state_guard.Isolation("plant")
                iso.start()
                with open(os.path.join(path, "herdr-kiro.json"), "w") as handle:
                    handle.write('{"x":2}\\n')
                try:
                    iso.stop()
                    print("missed", path)
                except AssertionError:
                    print("caught", path)
            """
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(base, "xc"),
            "XDG_STATE_HOME": os.path.join(base, "xs"),
            "KIRO_HOOKS_DIR": exported,
        }
        completed = subprocess.run(
            [sys.executable, "-c", code, os.path.join(ROOT, "tests"), hooks, exported],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.split("\n")[:2], [f"caught {hooks}", f"caught {exported}"])


class DoctorEnvTests(unittest.TestCase):
    def test_doctor_env_drops_an_exported_hooks_dir(self):
        import test_doctor

        exported = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, exported, True)
        with mock.patch.dict(os.environ, {"KIRO_HOOKS_DIR": exported}):
            case = test_doctor.DoctorTests("test_default_3_0_without_agent_engine_exits_zero")
            case.setUp()
            self.addCleanup(case.doCleanups)
        self.assertIsNone(case.env.get("KIRO_HOOKS_DIR"))


if __name__ == "__main__":
    unittest.main()
