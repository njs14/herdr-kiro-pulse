"""Keep the tests off the real herdr-kiro state dir and hook log.

The real paths are fixed when this module is first imported, before any test
module points XDG_STATE_HOME and HERDR_KIRO_LOG at a temp dir. They are built
by hand: hook.state_dir() would create the real dir.

The real herdr agent-detection dir, where install writes kiro.toml, and the
real Kiro hooks dirs (~/.kiro/hooks and an exported KIRO_HOOKS_DIR) are only
watched: a test that needs them must point HOME, XDG_CONFIG_HOME, or
KIRO_HOOKS_DIR at a temp dir. The rest of the herdr config dir holds the live
server socket, so it is not watched.

While a module is isolated, subprocess.Popen, and so run, call, and
check_output, refuses a shell or script run whose env could reach those dirs. A
shell behind env, nice, timeout, exec, or VAR=val words counts as a shell run,
and the env checked is the one it gets after those words, env -S, env -u, and
env -i are applied. os.posix_spawn and os.posix_spawnp get the same check, and os.system, os.exec*,
and os.spawn* always raise. In a forked child a refusal exits the child instead.
A child Python that calls them is not checked.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
from unittest import mock

KEYS = ("XDG_STATE_HOME", "HERDR_KIRO_LOG")

REAL_HOME = os.path.expanduser("~")

_bases = [os.path.join(REAL_HOME, ".local", "state")]
if os.environ.get("XDG_STATE_HOME"):
    _bases.append(os.environ["XDG_STATE_HOME"])
STATE_BASES = tuple(sorted(set(_bases)))
REAL_DIRS = tuple(sorted({os.path.join(base, "herdr-kiro") for base in _bases}))
_logs = {os.path.join(path, "hook.log") for path in REAL_DIRS}
if os.environ.get("HERDR_KIRO_LOG"):
    _logs.add(os.environ["HERDR_KIRO_LOG"])
REAL_LOGS = tuple(sorted(_logs))

_configs = [os.path.join(REAL_HOME, ".config")]
if os.environ.get("XDG_CONFIG_HOME"):
    _configs.append(os.environ["XDG_CONFIG_HOME"])
CONFIG_BASES = tuple(sorted(set(_configs)))
REAL_CONFIG_DIRS = tuple(sorted({os.path.join(base, "herdr", "agent-detection") for base in _configs}))

_hooks = [os.path.join(REAL_HOME, ".kiro", "hooks")]
if os.environ.get("KIRO_HOOKS_DIR"):
    _hooks.append(os.environ["KIRO_HOOKS_DIR"])
REAL_HOOK_DIRS = tuple(sorted(set(_hooks)))
HOOK_ROOTS = tuple(sorted({os.path.join(REAL_HOME, ".kiro")} | set(_hooks[1:])))

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SCRIPTS = {"herdr_kiro_state.py", "herdr_kiro_doctor.py"}
# Commands that run the next word as the command, with the options that take a value.
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-P"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "exec": {"-a"},
}


def _real(path):
    return os.path.realpath(path)


def _inside(path, roots):
    path = _real(path)
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in map(_real, roots))


def scratch_env(home):
    """A copy of this env with HOME, config, state, and hooks all under home."""
    env = dict(os.environ)
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    env["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
    env.pop("HERDR_KIRO_LOG", None)
    env.pop("KIRO_HOOKS_DIR", None)
    env.pop("HERDR_SESSION", None)
    return env


def scratch_env_problem(env):
    """Say why a script env could reach the real home, herdr config, state, or hooks, or return None."""
    if env is None:
        return "env is None, so the script inherits this process env"
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "KIRO_HOOKS_DIR"):
        value = env.get(key)
        if value and not os.path.isabs(value):
            return f"{key} {value} is relative, so it resolves against the child's cwd, which this check cannot see"
    home = env.get("HOME")
    if not home:
        return "HOME is not set"
    if _real(home) == _real(REAL_HOME):
        return f"HOME {home} is the real home"
    for name, roots in (
        (".kiro", HOOK_ROOTS),
        (".config", CONFIG_BASES),
        (os.path.join(".local", "state"), STATE_BASES),
    ):
        if _inside(os.path.join(home, name), roots):
            return f"HOME {home} has a {name} that resolves to the real one"
    config = env.get("XDG_CONFIG_HOME")
    if not config:
        return "XDG_CONFIG_HOME is not set, so the script falls back to $HOME/.config"
    if _inside(config, CONFIG_BASES):
        return f"XDG_CONFIG_HOME {config} is the inherited herdr config"
    state = env.get("XDG_STATE_HOME")
    if not state:
        return "XDG_STATE_HOME is not set, so the script falls back to $HOME/.local/state"
    if _inside(state, STATE_BASES):
        return f"XDG_STATE_HOME {state} is the inherited herdr-kiro state"
    hooks = env.get("KIRO_HOOKS_DIR")
    if hooks and _inside(hooks, HOOK_ROOTS):
        return f"KIRO_HOOKS_DIR {hooks} is the real Kiro hooks dir"
    return None


def _argv(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(item) for item in args]


def _is_assignment(word):
    name, eq, _value = word.partition("=")
    return bool(eq) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _parse(argv, env):
    """Split off leading VAR=val words and env, nice, timeout, or exec wrappers.

    Return the command left and the env it runs with: env with the VAR=val words
    applied, and cleared by env -i or a bare - and unset by env -u on the way.
    """
    argv = list(argv)
    env = dict(env)
    while argv:
        if _is_assignment(argv[0]):
            key, _eq, value = argv.pop(0).partition("=")
            env[key] = value
            continue
        name = os.path.basename(argv.pop(0))
        if name not in WRAPPERS:
            return [name, *argv], env
        while argv and argv[0].startswith("-") and argv[0] != "-":
            option = argv.pop(0)
            if option == "--":
                break
            if option in ("-S", "--split-string") and argv:
                argv[:1] = shlex.split(argv[0])
            elif option.startswith("-S") and not option.startswith("--"):
                argv[:0] = shlex.split(option[2:])
            elif option.startswith("--split-string="):
                argv[:0] = shlex.split(option.partition("=")[2])
            elif name == "env" and option in ("-i", "--ignore-environment"):
                env = {}
            elif name == "env" and option in ("-u", "--unset") and argv:
                env.pop(argv.pop(0), None)
            elif name == "env" and option.startswith("--unset="):
                env.pop(option.partition("=")[2], None)
            elif name == "env" and option.startswith("-u") and not option.startswith("--"):
                env.pop(option[2:], None)
            elif option in WRAPPERS[name] and argv:
                argv.pop(0)
        if argv and argv[0] == "-":
            argv.pop(0)
            if name == "env":
                env = {}
        if name == "timeout" and argv:
            argv.pop(0)
    return argv, env


def _command(argv):
    """argv without leading VAR=val words and env, nice, timeout, or exec wrappers and their options."""
    return _parse(argv, {})[0]


def _run_problem(args, shell, env):
    """Say why a shell or script run could reach the real dirs, or return None. Other runs are not checked."""
    if not _is_script_run(args, shell):
        return None
    if env is None or shell:
        return scratch_env_problem(env)
    return scratch_env_problem(_parse(_argv(args), env)[1])


def _is_script_run(args, shell):
    if shell:
        return True
    argv = _argv(args)
    if not argv:
        return False
    command = _command(argv)
    if command and os.path.basename(command[0]) in SHELLS:
        return True
    return any(item.endswith(".sh") or os.path.basename(item) in SCRIPTS for item in argv)


_REAL_POPEN = subprocess.Popen


class CheckedPopen(_REAL_POPEN):
    """Popen that raises before a shell or script run whose env could reach real dirs."""

    def __init__(self, args, *rest, **kwargs):
        problem = _run_problem(args, kwargs.get("shell"), kwargs.get("env"))
        if problem:
            raise AssertionError(f"{_argv(args)}: {problem}")
        super().__init__(args, *rest, **kwargs)


# os calls that start a program without Popen. No guarded module needs them.
_OS_RUNS = """
system execl execle execlp execlpe execv execve execvp execvpe
spawnl spawnle spawnlp spawnlpe spawnv spawnve spawnvp spawnvpe
"""
OS_RUNS = tuple(name for name in _OS_RUNS.split() if hasattr(os, name))
# Popen itself calls os.posix_spawn on some runs, so these get the same check as Popen instead.
OS_SPAWNS = tuple(name for name in ("posix_spawn", "posix_spawnp") if hasattr(os, name))


REFUSED_IN_CHILD = 125


def _fail(message, pid):
    """Raise in the test process. A forked child exits instead, so it never unwinds into a second test run."""
    if os.getpid() != pid:
        os.write(2, f"state guard: {message}\n".encode(errors="replace"))
        os._exit(REFUSED_IN_CHILD)
    raise AssertionError(message)


def _refuse(name, pid):
    def refuse(*_args, **_kwargs):
        _fail(f"os.{name} skips the state guard's run check; use subprocess with a scratch env", pid)

    return refuse


def _checked_spawn(real, pid):
    def spawn(path, argv, env, *rest, **kwargs):
        # path is what runs. argv[0] is only the name it sees.
        command = [path, *list(argv)[1:]]
        problem = _run_problem(command, False, env)
        if problem:
            _fail(f"{_argv(command)}: {problem}", pid)
        return real(path, argv, env, *rest, **kwargs)

    return spawn


def _stat(path):
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return (False, None, None)
    return (True, info.st_mtime_ns, info.st_size)


def _files(root):
    found = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _file_stats(root):
    """Each file under root with its stat, so an overwrite in place shows."""
    found = {}
    for name in _files(root):
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        found[name] = (info.st_mtime_ns, info.st_size, info.st_mode)
    return found


def snapshot():
    logs = {path: _stat(path) for path in REAL_LOGS}
    dirs = {path: (os.path.isdir(path), _files(path)) for path in REAL_DIRS}
    configs = {path: (os.path.isdir(path), _file_stats(path)) for path in REAL_CONFIG_DIRS}
    hooks = {path: (os.path.isdir(path), _file_stats(path)) for path in REAL_HOOK_DIRS}
    return logs, dirs, configs, hooks


def _tail(path, size):
    try:
        with open(path, "rb") as handle:
            handle.seek(size or 0)
            return handle.read()[-600:]
    except OSError:
        return b""


def changes(before, after):
    lines = []
    for path, was in before[0].items():
        now = after[0][path]
        if now != was:
            lines.append(f"{path}: (exists, mtime_ns, size) {was} -> {now}; new bytes {_tail(path, was[2])!r}")
    for path, (was_dir, was_files) in before[1].items():
        now_dir, now_files = after[1][path]
        if now_dir != was_dir:
            lines.append(f"{path}: dir exists {was_dir} -> {now_dir}")
        if now_files != was_files:
            added = sorted(now_files - was_files)
            removed = sorted(was_files - now_files)
            lines.append(f"{path}: files added {added} removed {removed}")
    for watched, current in zip(before[2:], after[2:]):
        for path, (was_dir, was_files) in watched.items():
            now_dir, now_files = current[path]
            if now_dir != was_dir:
                lines.append(f"{path}: dir exists {was_dir} -> {now_dir}")
            if now_files != was_files:
                names = sorted(set(was_files) | set(now_files))
                moved = [name for name in names if was_files.get(name) != now_files.get(name)]
                lines.append(f"{path}: files changed {moved}")
    return lines


class Isolation:
    """Point the hook at a temp state dir and log for one test module."""

    def __init__(self, module):
        self.module = module
        self.saved = {}
        self.root = ""
        self.before = None
        self.patches = []

    def start(self):
        self.before = snapshot()
        self.saved = {key: os.environ.get(key) for key in KEYS}
        self.root = tempfile.mkdtemp(prefix="herdr-kiro-tests-")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.root, "state")
        os.environ["HERDR_KIRO_LOG"] = os.path.join(self.root, "hook.log")
        self.patches = [mock.patch.object(subprocess, "Popen", CheckedPopen)]
        pid = os.getpid()
        self.patches += [mock.patch.object(os, name, _refuse(name, pid)) for name in OS_RUNS]
        self.patches += [mock.patch.object(os, name, _checked_spawn(getattr(os, name), pid)) for name in OS_SPAWNS]
        for patch in self.patches:
            patch.start()

    def stop(self):
        try:
            while self.patches:
                self.patches.pop().stop()
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        finally:
            if self.root:
                shutil.rmtree(self.root, ignore_errors=True)
        lines = changes(self.before, snapshot())
        if lines:
            raise AssertionError(
                f"{self.module} touched the real herdr-kiro state or herdr config:\n" + "\n".join(lines)
            )
