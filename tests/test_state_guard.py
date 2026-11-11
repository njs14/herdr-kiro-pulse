"""The state guard rejects a script run that could reach the real config, state, or hooks dir."""

import os
import shlex
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
# For each written dir: the real top dir it sits under, and a link one level below a scratch top dir.
DEEP_LINKS = {
    "hooks": (".kiro", os.path.join(".kiro", "hooks")),
    "config": (".config", os.path.join("xc", "herdr")),
    "state": (os.path.join(".local", "state"), os.path.join("xs", "herdr-kiro")),
}


def fake_real_home_root(test, base):
    """Register a temp dir as the real home, with its .kiro/hooks, config, and state dirs, so no run targets the real one."""
    home = os.path.join(base, "real-home")
    hooks = os.path.join(home, ".kiro", "hooks")
    config = os.path.join(home, ".config")
    state = os.path.join(home, ".local", "state")
    for path in (hooks, os.path.join(config, "herdr", "agent-detection"), os.path.join(state, "herdr-kiro")):
        os.makedirs(path)
    extra = {
        "HOOK_ROOTS": os.path.join(home, ".kiro"),
        "REAL_HOOK_DIRS": hooks,
        "CONFIG_BASES": config,
        "REAL_CONFIG_DIRS": os.path.join(config, "herdr", "agent-detection"),
        "STATE_BASES": state,
        "REAL_DIRS": os.path.join(state, "herdr-kiro"),
    }
    patchers = [mock.patch.object(_state_guard, "REAL_HOME", home)]
    patchers += [
        mock.patch.object(_state_guard, attr, (*getattr(_state_guard, attr), path)) for attr, path in extra.items()
    ]
    for patcher in patchers:
        patcher.start()
        test.addCleanup(patcher.stop)
    return home, hooks


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

    def test_argv_overrides_of_a_scratch_env_are_rejected(self):
        real, hooks = fake_real_home_root(self, self.base)
        for argv in (
            ["env", f"HOME={real}", "XDG_CONFIG_HOME=", "KIRO_HOOKS_DIR=", "sh", "-c", "true"],
            ["env", "-S", f"HOME={real} sh -c true"],
            ["env", f"KIRO_HOOKS_DIR={hooks}", "sh", "-c", "true"],
            [f"KIRO_HOOKS_DIR={hooks}", "sh", "-c", "true"],
            ["nice", "-n", "5", f"HOME={real}", "sh", "-c", "true"],
            ["env", "-u", "XDG_CONFIG_HOME", "sh", "-c", "true"],
            ["env", "--unset=XDG_STATE_HOME", "sh", "-c", "true"],
            ["env", "-i", "PATH=/usr/bin:/bin", "sh", "-c", "true"],
            ["env", "-", "PATH=/usr/bin:/bin", "sh", "-c", "true"],
        ):
            with self.subTest(argv=argv), self.assertRaises(AssertionError):
                subprocess.run(argv, env=self.scratch(), check=False)

    def test_scratch_argv_overrides_run(self):
        env = self.scratch()
        other = os.path.join(self.base, "other")
        words = [f"{key}={env[key]}" for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")]
        for argv in (
            ["env", f"XDG_CONFIG_HOME={other}", "sh", "-c", "true"],
            ["env", "-S", f"HOME={other} sh -c true"],
            ["env", "-i", *words, "sh", "-c", "true"],
            ["env", "-u", "KIRO_HOOKS_DIR", "sh", "-c", "true"],
        ):
            with self.subTest(argv=argv):
                subprocess.run(argv, env=env, check=True)

    def test_combined_env_short_options_are_rejected(self):
        real, _hooks = fake_real_home_root(self, self.base)
        home = os.path.join(self.base, "home")
        for argv in (
            ["env", "-iS", f"PATH=/usr/bin:/bin HOME={real} sh -c true"],
            ["env", "-vS", f"HOME={real} XDG_CONFIG_HOME= KIRO_HOOKS_DIR= sh -c true"],
            ["env", "-ivS", f"PATH=/usr/bin:/bin HOME={home} sh -c true"],
            ["env", "-iu", "XDG_CONFIG_HOME", f"HOME={home}", "PATH=/usr/bin:/bin", "sh", "-c", "true"],
            ["env", "-vuXDG_STATE_HOME", "sh", "-c", "true"],
            ["env", "-vC", self.base, f"HOME={real}", "sh", "-c", "true"],
        ):
            with self.subTest(argv=argv), self.assertRaises(AssertionError):
                subprocess.run(argv, env=self.scratch(), stderr=subprocess.DEVNULL, check=False)

    def test_combined_env_short_options_with_scratch_values_run(self):
        env = self.scratch()
        other = os.path.join(self.base, "other")
        words = [f"{key}={env[key]}" for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")]
        for argv in (
            ["env", "-vS", shlex.join([f"HOME={other}", *words[1:], "sh", "-c", "true"])],
            ["env", "-iS", shlex.join([*words, "PATH=/usr/bin:/bin", "sh", "-c", "true"])],
            ["env", "-iu", "KIRO_HOOKS_DIR", *words, "PATH=/usr/bin:/bin", "sh", "-c", "true"],
        ):
            with self.subTest(argv=argv):
                subprocess.run(argv, env=env, stderr=subprocess.DEVNULL, check=True)

    def test_env_options_are_applied_in_order(self):
        env = {"HOME": "/h", "PATH": "/p", "X": "x"}
        for argv, expected in (
            (["env", "-iS", "A=1 sh"], (["sh"], {"A": "1"})),
            (["env", "-vS", "A=1 sh"], (["sh"], {**env, "A": "1"})),
            (["env", "-SA=1 sh"], (["sh"], {**env, "A": "1"})),
            (["env", "-iu", "X", "A=1", "sh"], (["sh"], {"A": "1"})),
            (["env", "-uiX", "sh"], (["sh"], {"HOME": "/h", "PATH": "/p", "X": "x"})),
            (["env", "-viuX", "sh"], (["sh"], {})),
            (["env", "-0vuX", "sh"], (["sh"], {"HOME": "/h", "PATH": "/p"})),
            (["env", "-vC", "/tmp", "A=1", "sh"], (["sh"], {**env, "A": "1"})),
            (["env", "-vP/bin", "A=1", "sh"], (["sh"], {**env, "A": "1"})),
            (["env", "-uX", "-i", "A=1", "sh"], (["sh"], {"A": "1"})),
            (["env", "-u", "X", "sh"], (["sh"], {"HOME": "/h", "PATH": "/p"})),
            (["env", "--unset=X", "--split-string=A=1 sh"], (["sh"], {"HOME": "/h", "PATH": "/p", "A": "1"})),
            (["env", "--unset", "X", "--split-string", "sh"], (["sh"], {"HOME": "/h", "PATH": "/p"})),
            (["env", "--ignore-environment", "A=1", "sh"], (["sh"], {"A": "1"})),
            (["env", "-", "A=1", "sh"], (["sh"], {"A": "1"})),
            (["env", "-v", "--", "A=1", "sh"], (["sh"], {**env, "A": "1"})),
            (["nice", "-n", "5", "timeout", "-s", "KILL", "5", "sh"], (["sh"], env)),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(_state_guard._parse(argv, env), expected)

    def test_argv_overrides_do_not_excuse_an_inherited_env(self):
        self.assert_rejected(
            lambda: subprocess.run(["env", "-i", f"HOME={self.base}", "sh", "-c", "true"], check=False)
        )

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

    def deep_linked_env(self, kind, target):
        """A scratch HOME, XDG_CONFIG_HOME, and XDG_STATE_HOME whose top dirs are real dirs, with one link below them."""
        home = tempfile.mkdtemp(dir=self.base)
        link = os.path.join(home, DEEP_LINKS[kind][1])
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
        return self.scratch(
            {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, "xc"),
                "XDG_STATE_HOME": os.path.join(home, "xs"),
            }
        )

    def test_a_link_below_the_top_dirs_to_the_real_dirs_is_rejected(self):
        real = self.fake_real_home()
        for kind, (root, below) in DEEP_LINKS.items():
            with self.subTest(kind=kind):
                target = os.path.join(real[root], os.path.basename(below))
                os.makedirs(target, exist_ok=True)
                env = self.deep_linked_env(kind, target)
                self.assertIsNotNone(_state_guard.scratch_env_problem(env))
                with self.assertRaises(AssertionError):
                    subprocess.run(["sh", "-c", "true"], env=env, check=False)

    def test_a_link_below_the_top_dirs_to_other_scratch_dirs_runs(self):
        self.fake_real_home()
        for kind in DEEP_LINKS:
            with self.subTest(kind=kind):
                own = tempfile.mkdtemp(dir=self.base)
                self.assertIsNone(_state_guard.scratch_env_problem(self.deep_linked_env(kind, own)))

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


class OsRunTests(unittest.TestCase):
    """os.system, os.exec*, os.spawn*, and posix_spawn skip Popen, so the guard owns them too."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_system_is_rejected(self):
        with self.assertRaises(AssertionError):
            os.system("true")  # noqa: S605 - the call under test, which the guard must refuse

    def test_spawn_is_rejected(self):
        with self.assertRaises(AssertionError):
            os.spawnv(os.P_WAIT, "/usr/bin/true", ["true"])  # ty: ignore[deprecated]
        with self.assertRaises(AssertionError):
            os.spawnvpe(os.P_WAIT, "true", ["true"], dict(os.environ))  # ty: ignore[deprecated]

    def test_exec_in_a_forked_child_exits_the_child(self):
        # Exec in a child, so an unguarded exec replaces the child and not this test run.
        # A refused exec must end the child: raising would unwind it into a second copy of the test run.
        for name in ("execv", "execve", "execvp", "execl"):
            with self.subTest(name=name):
                pid = os.fork()
                if pid == 0:
                    try:
                        devnull = os.open(os.devnull, os.O_WRONLY)
                        os.dup2(devnull, 2)
                        call = getattr(os, name)
                        if name == "execve":
                            call("/usr/bin/true", ["true"], dict(os.environ))
                        elif name == "execl":
                            call("/usr/bin/true", "true")
                        else:
                            call("/usr/bin/true", ["true"])
                    except AssertionError:
                        os._exit(99)
                    finally:
                        os._exit(98)
                _pid, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), _state_guard.REFUSED_IN_CHILD)

    def test_posix_spawn_in_a_forked_child_exits_the_child(self):
        pid = os.fork()
        if pid == 0:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2)
                os.posix_spawn("/bin/sh", ["sh", "-c", "true"], dict(os.environ))
            except AssertionError:
                os._exit(99)
            finally:
                os._exit(98)
        _pid, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), _state_guard.REFUSED_IN_CHILD)

    def test_posix_spawn_of_a_shell_with_the_inherited_env_is_rejected(self):
        for spawn in (os.posix_spawn, os.posix_spawnp):
            with self.subTest(spawn=spawn.__name__), self.assertRaises(AssertionError):
                os.waitpid(spawn("/bin/sh", ["sh", "-c", "true"], dict(os.environ)), 0)

    def test_posix_spawn_with_an_argv_override_is_rejected(self):
        real, _hooks = fake_real_home_root(self, self.base)
        env = _state_guard.scratch_env(os.path.join(self.base, "home"))
        with self.assertRaises(AssertionError):
            os.waitpid(os.posix_spawn("/usr/bin/env", ["env", f"HOME={real}", "sh", "-c", "true"], env), 0)

    def test_posix_spawn_of_a_plain_command_and_popen_still_run(self):
        os.waitpid(os.posix_spawn("/usr/bin/true", ["true"], dict(os.environ)), 0)
        env = dict(os.environ)
        env["HOME"] = os.path.join(self.base, "home")
        env["XDG_CONFIG_HOME"] = os.path.join(self.base, "xc")
        env["XDG_STATE_HOME"] = os.path.join(self.base, "xs")
        env.pop("KIRO_HOOKS_DIR", None)
        # close_fds=False and an absolute path send Popen through os.posix_spawn.
        subprocess.run(["/usr/bin/true"], close_fds=False, check=True)
        subprocess.run(["/bin/sh", "-c", "true"], env=env, close_fds=False, check=True)


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
