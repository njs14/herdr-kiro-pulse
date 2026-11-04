"""Grace rules for the Kiro hook.

Herdr drops a working report when a foreground turn repaints onto the idle
prompt, about 0.2s after Stop, and it keeps the old sequence. Re-sending
working after that drop is what holds the pane for the grace period.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prototype"))

import herdr_kiro_state as hook


def pin_owner(pid, start="100.000000"):
    """Report a fine start token for this pid while the patch is active."""
    real = hook.hires_start

    def hires(asked):
        try:
            if int(asked) == int(pid):
                return start
        except (TypeError, ValueError):
            return real(asked)
        return real(asked)

    return mock.patch.object(hook, "hires_start", side_effect=hires)


def effective(reports, drop_at, moment):
    """Status Herdr would show. A drop forgets working reports issued before it."""
    chosen = None
    for stamped, kind in reports:
        if stamped <= moment:
            chosen = (stamped, kind)
    if chosen is None:
        return "idle"
    stamped, kind = chosen
    if kind == "working" and drop_at is not None and stamped < drop_at <= moment:
        return "idle"
    return kind


def simulate(policy):
    """One session stops at t=1. Herdr drops working at t=1.2. Ticks every 0.25s."""
    state = hook.fresh_state()
    state = hook.apply_event(state, "UserPromptSubmit", "parent", 0.0)
    reports = []
    drop_at = 1.2
    for step in range(0, 24):
        moment = step * 0.25
        if moment >= 1.0 and "parent" in state["busy"]:
            state = hook.apply_event(state, "Stop", "parent", 1.0)
        action = policy(state, moment)
        if action == "working":
            reports.append((moment, "working"))
        elif action == "idle" and (not reports or reports[-1][1] != "idle"):
            reports.append((moment, "idle"))
    return reports, drop_at


def old_policy(state, moment):
    """The prototype: one working report, then a single idle report when grace ends."""
    if state["busy"]:
        return "working" if moment < 0.25 else None
    if moment >= state["grace_until"]:
        return "idle"
    return None


def new_policy(state, moment):
    banner = False
    return hook.next_action(len(state["busy"]), state["grace_until"], banner, moment)


class GraceTests(unittest.TestCase):
    def test_old_policy_is_idle_during_the_grace(self):
        reports, drop_at = simulate(old_policy)
        self.assertEqual(effective(reports, drop_at, 2.0), "idle")

    def test_reassert_holds_working_until_grace_ends(self):
        reports, drop_at = simulate(new_policy)
        grace_end = 1.0 + hook.SETTLE_SECS
        self.assertEqual(effective(reports, drop_at, grace_end - 0.05), "working")
        self.assertEqual(effective(reports, drop_at, grace_end + 0.25), "idle")
        idle_at = next(stamped for stamped, kind in reports if kind == "idle")
        self.assertGreaterEqual(idle_at, grace_end - 0.01)

    def test_parallel_sessions_stay_busy_until_both_stop(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "UserPromptSubmit", "a", 1)
        state = hook.apply_event(state, "UserPromptSubmit", "b", 1.1)
        state = hook.apply_event(state, "Stop", "a", 3)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 3.2),
            "working",
        )
        self.assertNotIn("a", state["busy"])
        self.assertIn("b", state["busy"])
        state = hook.apply_event(state, "Stop", "b", 4)
        self.assertEqual(state["grace_until"], 4 + hook.SETTLE_SECS)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, state["grace_until"] - 0.05),
            "working",
        )
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, state["grace_until"] + 0.05),
            "idle",
        )

    def test_banner_holds_through_a_quiet_watch(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "Stop", "parent", 5)
        state = hook.note_banner(state, True, 20)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], True, 20),
            "working",
        )
        self.assertGreater(state["grace_until"], 20)
        quiet = hook.note_banner(state, False, 21)
        self.assertEqual(
            hook.next_action(len(quiet["busy"]), quiet["grace_until"], False, quiet["grace_until"] - 0.05),
            "working",
        )
        self.assertEqual(
            hook.next_action(len(quiet["busy"]), quiet["grace_until"], False, quiet["grace_until"] + 0.05),
            "idle",
        )

    def test_stale_session_is_dropped(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "UserPromptSubmit", "stuck", 0)
        later = hook.normalize(state, hook.STALE_SECS + 1)
        self.assertEqual(later["busy"], {})
        self.assertEqual(hook.next_action(0, later["grace_until"], False, hook.STALE_SECS + 1), "idle")

    def test_chooser_footer_is_an_approval(self):
        footer = "esc to close · ↑↓ to navigate · ↵ to select · Tab to edit"
        text = "\n".join(
            [
                "Print the approval probe string requires approval",
                "❯ Allow once",
                "  Deny once",
                "  Create allow rule to approve future similar actions",
                footer,
            ]
        )
        self.assertTrue(hook.approval_text(text))

    def test_model_output_phrases_are_not_an_approval(self):
        text = "\n".join(
            [
                "I will always allow this pattern.",
                "You can press esc to close the panel.",
                "always deny is also a phrase a reply might contain.",
                "› ask a question or describe a task",
            ]
        )
        self.assertFalse(hook.approval_text(text))

    def test_parallel_steps_with_session_ids_stripped(self):
        key = hook.session_of({"cwd": "/work", "hook_event_name": "SessionStart"}, ppid=77)
        self.assertEqual(key, hook.AGGREGATE)
        self.assertEqual(key, hook.session_of({"cwd": "/work"}, ppid=78))
        state = hook.fresh_state()
        for moment in range(10):
            state = hook.apply_event(state, "SessionStart", key, moment)
            state = hook.apply_event(state, "PreToolUse", key, moment + 0.1)
        self.assertEqual(state["aggregate"], 10)
        self.assertEqual(len(state["busy"]), 1)
        state = hook.apply_event(state, "Stop", key, 20)
        self.assertEqual(state["aggregate"], 9)
        self.assertIn(hook.AGGREGATE, state["busy"])
        for moment in range(9):
            state = hook.apply_event(state, "Stop", key, 21 + moment)
        self.assertEqual(state["aggregate"], 0)
        self.assertEqual(state["busy"], {})
        self.assertEqual(state["grace_until"], 29 + hook.SETTLE_SECS)

    def test_stripped_parallel_stays_busy_until_both_stop(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "SessionStart", hook.AGGREGATE, 1)
        state = hook.apply_event(state, "SessionStart", hook.AGGREGATE, 1.1)
        state = hook.apply_event(state, "PreToolUse", hook.AGGREGATE, 1.2)
        self.assertEqual(state["aggregate"], 2)
        self.assertEqual(len(state["busy"]), 1)
        state = hook.apply_event(state, "Stop", hook.AGGREGATE, 3)
        self.assertEqual(state["aggregate"], 1)
        self.assertIn(hook.AGGREGATE, state["busy"])
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 3.2),
            "working",
        )
        state = hook.apply_event(state, "Stop", hook.AGGREGATE, 4)
        self.assertEqual(state["aggregate"], 0)
        self.assertEqual(state["busy"], {})
        self.assertEqual(state["grace_until"], 4 + hook.SETTLE_SECS)

    def test_banner_line_ignores_model_text(self):
        self.assertTrue(hook.workflow_banner_from("▶ watch-step running 0/0 · ctrl+x expand · ctrl+g monitor\n"))
        self.assertFalse(hook.workflow_banner_from("Status: ▶ deploy running\n"))
        self.assertFalse(hook.workflow_banner_from("▶ name running without the hints\n"))

    def test_day_of_month_lstart_matches(self):
        padded = "Sun Nov  1 01:02:03 2026"
        plain = "Sun Nov 1 01:02:03 2026"
        self.assertEqual(hook.start_token(padded), hook.start_token(plain))
        self.assertTrue(hook.start_token(padded).isdigit())

    def test_comm_basename_keeps_a_path_with_spaces(self):
        self.assertEqual(
            hook.comm_basename("/Users/Jane Doe/Applications/kiro-cli"),
            "kiro-cli",
        )

    def test_transcript_path_distinguishes_stripped_payloads(self):
        first = hook.session_of({"transcript_path": "/tmp/one.jsonl", "cwd": "/work"}, ppid=5)
        second = hook.session_of({"transcript_path": "/tmp/two.jsonl", "cwd": "/work"}, ppid=5)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, "unknown")
        self.assertEqual(hook.session_of({"session_id": "sess_a", "cwd": "/work"}, ppid=5), "sess_a")

    def test_repeat_gap_is_inside_the_grace(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "UserPromptSubmit", "step-1", 0)
        state = hook.apply_event(state, "Stop", "step-1", 2)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 2.1),
            "working",
        )
        state = hook.apply_event(state, "UserPromptSubmit", "step-2", 2.15)
        self.assertEqual(state["grace_until"], 0.0)
        self.assertIn("step-2", state["busy"])

    def test_watcher_exits_after_idle_and_a_new_session_restarts_it(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda _state: None)
        reads = {"n": 0}

        def read(_pane):
            reads["n"] += 1
            return "› ask a question or describe a task\n"

        def sleep(_delay):
            if reads["n"] > 2:
                raise AssertionError("watcher kept polling after idle")

        def report(*_args, **_kwargs):
            return True

        previous_delay = hook.RELEASE_DELAY
        hook.RELEASE_DELAY = 0
        try:
            with (
                mock.patch.object(hook, "pane_text", read),
                mock.patch.object(hook, "send_report", report),
                mock.patch.object(hook, "release_authority", report),
                mock.patch.object(hook, "agent_lookup", lambda _pane: "idle"),
                mock.patch.object(hook.time, "sleep", sleep),
            ):
                hook.watch_loop("w1:p1", path)
        finally:
            hook.RELEASE_DELAY = previous_delay
        self.assertEqual(reads["n"], 1)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 0)

        spawned = {}

        class Proc:
            pid = 4321

        real_popen = subprocess.Popen

        def popen(args, *extra, **kwargs):
            command = args[0] if isinstance(args, (list, tuple)) and args else ""
            if command == "ps":
                return real_popen(args, *extra, **kwargs)
            spawned["args"] = args
            return Proc()

        def mark_busy(state):
            state["busy"]["sess"] = time.time()

        hook.update(path, mark_busy)
        with mock.patch.object(hook.subprocess, "Popen", popen):
            hook.ensure_watcher("w1:p1", path)
        self.assertIn("args", spawned)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 4321)

    def test_reset_seq_does_not_fall_below_the_high_water(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def raise_seq(state):
            state["seq"] = 50

        hook.update(path, raise_seq)
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["seq"] = 0
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        state = hook.update(path, lambda _state: None)
        self.assertGreaterEqual(state["seq"], 50)

    def test_state_file_is_private(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda _state: None)
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_log_stays_bounded_and_private(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "hook.log")
        previous = os.environ.get("HERDR_KIRO_LOG")
        os.environ["HERDR_KIRO_LOG"] = path
        limit = hook.LOG_LIMIT
        keep = hook.LOG_KEEP
        hook.LOG_LIMIT = 400
        hook.LOG_KEEP = 180
        try:
            for index in range(40):
                hook.log(f"row {index} " + ("x" * 30))
            self.assertLess(os.path.getsize(path), 700)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        finally:
            hook.LOG_LIMIT = limit
            hook.LOG_KEEP = keep
            if previous is None:
                os.environ.pop("HERDR_KIRO_LOG", None)
            else:
                os.environ["HERDR_KIRO_LOG"] = previous

    def test_shell_wrapper_reuses_the_ancestor_key(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "prototype"))
        code = (
            f"import sys; sys.path.insert(0, {root!r}); import herdr_kiro_state as hook; print(hook.session_of({{}}))"
        )
        quoted = shlex.quote(code)

        def run():
            completed = subprocess.run(
                ["sh", "-c", "python3 -c " + quoted],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout.strip()

        first = run()
        second = run()
        self.assertEqual(first, second)
        self.assertEqual(first, hook.AGGREGATE)

    def test_dead_kiro_drops_a_busy_session(self):
        state = hook.apply_event(
            hook.fresh_state(),
            "UserPromptSubmit",
            "sess",
            1,
            owner=424242,
            owner_started="old",
        )

        def gone(_pid):
            return None

        with mock.patch.object(hook, "process_snapshot", gone):
            later = hook.normalize(state, 10)
        self.assertEqual(later["busy"], {})

    def test_alive_rejects_a_reused_pid(self):
        def snap(_pid):
            return {"ppid": 1, "lstart": "NOW", "comm": "python"}

        with (
            mock.patch.object(hook, "process_snapshot", snap),
            mock.patch.object(hook, "_linux_start_ticks", return_value=None),
        ):
            self.assertFalse(hook.alive(50, started="sec:THEN"))
            self.assertFalse(hook.alive(50, started="sec:NOW"))
            self.assertFalse(hook.alive(50, started="NOW"))
            self.assertFalse(hook.alive(50, started=""))
        with (
            mock.patch.object(hook, "process_snapshot", snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        ):
            self.assertTrue(hook.alive(50, started="100.000000"))
            self.assertFalse(hook.alive(50, started="200.000000"))

    def test_release_authority_reports_success(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda _state: None)
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            return True

        with mock.patch.object(hook, "herdr", fake_herdr):
            self.assertTrue(hook.release_authority("w1:p9", path))
        self.assertEqual(calls[0][0], "release-agent")
        self.assertIn("w1:p9", calls[0])

    def test_one_missing_agent_does_not_count_as_confirmed(self):
        self.assertFalse(hook.gone_confirmed(10.0, 10.2, 1, False))
        self.assertTrue(hook.gone_confirmed(10.0, 12.0, 3, False))
        self.assertTrue(hook.gone_confirmed(None, 0.0, 0, True))

    def test_missing_agent_is_a_blip_while_the_owner_lives(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: holder.poll() is not None or holder.kill())
        hook.begin_tick()
        pinned = pin_owner(holder.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": holder.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 3

        hook.update(path, seed)
        sleeps = {"n": 0}

        def sleep(_delay):
            sleeps["n"] += 1
            if sleeps["n"] > 2:
                raise SystemExit("stop")

        def fake_wait(_self, _pids, timeout):
            sleep(timeout)
            return False

        with (
            mock.patch.object(hook, "pane_text", lambda _pane: "› ask a question or describe a task\n"),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "gone"),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "blocked", "id": "crew_approval", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("w1:p9", path)
        holder.kill()
        holder.wait(timeout=5)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("sess", json.load(handle)["busy"])

    def test_dead_owner_with_banner_stops_the_watcher(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        owner = subprocess.Popen(["sleep", "30"])
        reports = []
        pinned = pin_owner(owner.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 1

        hook.update(path, seed)
        owner.kill()
        owner.wait(timeout=5)

        def report(*args, **_kwargs):
            reports.append(args)
            return True

        banner = "▶ two-step running 0/2 · ctrl+x expand · ctrl+g monitor\n"
        waits = {"n": 0}
        real_wait = hook.OwnerExitWatch.wait

        def limited(self, pids, timeout):
            waits["n"] += 1
            if waits["n"] > 4:
                raise AssertionError("watcher kept reporting after the owner died")
            return real_wait(self, pids, min(timeout, 0.05))

        with (
            mock.patch.object(hook, "pane_text", lambda _pane: banner),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", report),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "working", "id": "workflow_banner_working", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", limited),
        ):
            hook.watch_loop("w1:p9", path)
        self.assertFalse(any(len(args) >= 3 and args[2] == "working" for args in reports))
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["watcher"], 0)
        self.assertEqual(saved["busy"], {})

    def test_session_end_then_process_exit_stops_the_watcher(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        hook.begin_tick()
        pinned = pin_owner(owner.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 2

        hook.update(path, seed)
        reports = []
        phase = {"n": 0}
        real_wait = hook.OwnerExitWatch.wait

        def staged(self, pids, timeout):
            phase["n"] += 1
            if phase["n"] == 2:

                def clear(state):
                    state["busy"] = {}
                    state["owners"] = {}
                    state["owner_started"] = {}
                    state["grace_until"] = 0.0

                hook.update(path, clear)
                return False
            if phase["n"] == 3:
                owner.kill()
                owner.wait(timeout=5)
                return real_wait(self, pids, min(timeout, 0.3))
            if phase["n"] > 3:
                raise AssertionError("watcher kept reporting after the process exited")
            return False

        banner = "▶ two-step running 0/2 · ctrl+x expand · ctrl+g monitor\n"
        with (
            mock.patch.object(hook, "pane_text", lambda _pane: banner),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", lambda *args, **_kwargs: reports.append(args) or True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "working", "id": "workflow_banner_working", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", staged),
        ):
            hook.watch_loop("w1:p9", path)
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["watcher"], 0)
        self.assertLessEqual(sum(1 for args in reports if len(args) >= 3 and args[2] == "working"), 2)

    def test_owner_exit_watch_wakes_when_the_process_dies(self):
        proc = subprocess.Popen(["sleep", "30"])
        watch = hook.OwnerExitWatch()
        try:
            proc.kill()
            proc.wait(timeout=5)
            started = time.time()
            woke = watch.wait([proc.pid], 2.0)
            elapsed = time.time() - started
        finally:
            watch.close()
            if proc.poll() is None:
                proc.kill()
        self.assertTrue(woke)
        self.assertLess(elapsed, 1.0)

    def test_same_session_prompt_bumps_generation(self):
        state = hook.apply_event(hook.fresh_state(), "SessionStart", "sess", 1.0)
        seen = state["generation"]
        state = hook.apply_event(state, "UserPromptSubmit", "sess", 2.0)
        self.assertNotEqual(state["generation"], seen)
        self.assertIn("sess", state["busy"])
        self.assertFalse(hook.release_if_current(state, seen, ["sess"]))
        self.assertIn("sess", state["busy"])

    def test_late_release_does_not_fire_after_the_slot_moves(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def seed(state):
            state["watcher"] = 4242
            state["generation"] = 4
            state["busy"] = {"sess": time.time()}

        hook.update(path, seed)
        calls = []
        with (
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.object(hook.os, "getpid", lambda: 111),
        ):
            self.assertFalse(hook.finish_if_unchanged("w1:p9", path, {"generation": 4, "busy": {"sess": 1.0}}, "idle"))
        self.assertEqual(calls, [])
        with open(path, encoding="utf-8") as handle:
            self.assertIn("sess", json.load(handle)["busy"])

    def test_newer_session_survives_a_stale_clear(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "SessionStart", "older", 1.0)
        seen = state["generation"]
        state = hook.apply_event(state, "SessionStart", "newer", 2.0)
        self.assertNotEqual(state["generation"], seen)
        self.assertFalse(hook.release_if_current(state, seen, ["older"]))
        self.assertIn("older", state["busy"])
        self.assertIn("newer", state["busy"])
        self.assertFalse(state["retiring"])

    def test_retiring_watcher_does_not_block_the_next_one(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def mark(state):
            state["watcher"] = os.getpid()
            state["watcher_started"] = "alive"
            state["retiring"] = True

        hook.update(path, mark)
        spawned = {}

        class Proc:
            pid = 777

        def popen(*_args, **_kwargs):
            spawned["yes"] = True
            return Proc()

        with (
            mock.patch.object(hook, "alive", lambda *_args, **_kwargs: True),
            mock.patch.object(hook.subprocess, "Popen", popen),
            mock.patch.object(hook, "process_snapshot", lambda _pid: {"ppid": 1, "lstart": "9", "comm": "python"}),
        ):
            hook.ensure_watcher("w1:p1", path)
        self.assertTrue(spawned.get("yes"))

    def test_owner_death_is_visible_after_normalize(self):
        state = hook.apply_event(hook.fresh_state(), "UserPromptSubmit", "sess", 1.0, owner=424242, owner_started="old")

        def gone(_pid):
            return None

        with mock.patch.object(hook, "process_snapshot", gone):
            later = hook.normalize(state, 10)
        self.assertEqual(later["busy"], {})
        self.assertTrue(later["owners_died"])
        self.assertTrue(hook.should_stop_for_dead_owner(later, False))
        self.assertFalse(hook.should_stop_for_dead_owner(later, True))
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def seed(current):
            current.clear()
            current.update(state)

        with mock.patch.object(hook, "process_snapshot", gone):
            first = hook.update(path, seed)
            second = hook.update(path, lambda _current: None)
        self.assertTrue(first["owners_died"])
        self.assertTrue(second["owners_died"])
        self.assertEqual(second["busy"], {})

    def test_failed_explain_is_not_cached_as_idle(self):
        hook._SCREEN_CACHE.clear()
        calls = {"n": 0}

        class Completed:
            returncode = 1
            stdout = ""

        def run(*_args, **_kwargs):
            calls["n"] += 1
            return Completed()

        with mock.patch.object(hook.subprocess, "run", run):
            first = hook.screen_view("same screen")
            second = hook.screen_view("same screen")
        self.assertEqual(first["state"], "unknown")
        self.assertFalse(first["ok"])
        self.assertEqual(second["state"], "unknown")
        self.assertEqual(calls["n"], 2)

    def test_ps_uses_the_c_locale_and_rejects_an_unparsed_table(self):
        hook.begin_tick()
        seen = {}

        class Completed:
            returncode = 0
            stdout = "not a process row\n"

        def run(_cmd, **kwargs):
            seen["env"] = kwargs.get("env") or {}
            return Completed()

        with mock.patch.object(hook.subprocess, "run", run):
            table = hook.process_table()
        self.assertEqual(seen["env"].get("LC_ALL"), "C")
        self.assertEqual(table, "error")

    def test_model_text_does_not_arm_the_idle_clear(self):
        text = "The step is still thinking about the file list.\n"
        self.assertFalse(hook.prompt_visible(text))

    def test_shell_after_exit_counts_as_settled(self):
        shell = "ubuntu@host:/workspace$\n"
        self.assertTrue(hook.screen_is_settled(shell))
        self.assertFalse(hook.screen_is_quiet(shell))
        footer = "› Kiro is working · 1s · Type to steer · Ctrl+S to queue\n"
        self.assertFalse(hook.screen_is_settled(footer))

    def test_quiet_prompt_does_not_report_working(self):
        quiet = "› ask a question or describe a task\n"
        self.assertIsNone(hook.report_for(quiet, "working"))
        self.assertEqual(hook.report_for(quiet, "idle"), None)

    def test_widget_screen_is_blocked_and_not_reported_working(self):
        if shutil.which("herdr") is None:
            self.skipTest("herdr is not on PATH")
        screen = (
            " ▶ approve-probe running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            " ⚠ 1 tool approval pending\n"
            " ❯ (a) Approve all pending\n"
            "   (f) Approve all pending and auto-approve future subagent requests\n"
            "   (c) Configure individually (agent monitor)\n"
            "   (x) Exit (cancel subagents)\n"
        )
        self.assertTrue(hook.blocker_visible(screen))
        self.assertTrue(hook.workflow_banner_from(screen))
        winner = hook.highest_rule(screen)
        self.assertEqual(winner["state"], "blocked")
        self.assertEqual(winner["id"], "crew_approval")
        self.assertEqual(hook.report_for(screen, "working"), "blocked")

    def test_duplicate_start_clears_when_the_screen_stays_idle(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "SessionStart", hook.AGGREGATE, 1)
        state = hook.apply_event(state, "SessionStart", hook.AGGREGATE, 2)
        self.assertEqual(state["aggregate"], 2)
        quiet = "› ask a question or describe a task\n"
        self.assertTrue(hook.screen_is_quiet(quiet))
        hook.release_stuck(state)
        self.assertEqual(state["aggregate"], 0)
        self.assertEqual(state["busy"], {})
        self.assertEqual(hook.next_action(0, state["grace_until"], False, 3), "idle")

    def test_quiet_screen_waits_before_clearing_a_live_slot(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "SessionStart", hook.AGGREGATE, 1)
        quiet = "› ask a question or describe a task\n"
        self.assertFalse(hook.workflow_banner_from(quiet))
        self.assertGreater(hook.IDLE_CLEAR_SECS, 0)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 1 + hook.IDLE_CLEAR_SECS - 0.1),
            "working",
        )

    def test_narrow_banner_matches_and_model_text_does_not(self):
        full = "▶ parallel-steps running 0/2 · ctrl+x expand · ctrl+g monitor"
        self.assertEqual(len(full), 61)
        self.assertTrue(hook.workflow_banner_from(full[:54] + "\n"))
        wrapped = "▶ parallel-steps running 0/2 · ctrl+x expand · ctrl+\ng monitor\n"
        self.assertTrue(hook.workflow_banner_from(wrapped))
        self.assertFalse(hook.workflow_banner_from("Status: ▶ deploy running\n"))

    def test_outer_kiro_cli_is_the_owner_not_the_chat_child(self):
        chain = {
            10: {"ppid": 9, "lstart": "chat", "comm": "kiro-cli-chat"},
            9: {"ppid": 8, "lstart": "outer", "comm": "kiro-cli"},
            8: {"ppid": 1, "lstart": "sh", "comm": "bash"},
        }

        def snap(pid):
            return chain.get(pid)

        with mock.patch.object(hook.os, "getppid", return_value=10), mock.patch.object(hook, "process_snapshot", snap):
            self.assertEqual(hook.kiro_ancestor(), (9, "outer"))

    def test_kqueue_wait_reports_a_process_exit(self):
        seen = {}

        class Queue:
            def control(self, changes, _count, _timeout):
                seen["changes"] = list(changes)
                return [object()]

            def close(self):
                seen["closed"] = True

        def kevent(*args):
            return args

        watch = hook.OwnerExitWatch()
        with (
            mock.patch.object(hook.select, "kqueue", Queue, create=True),
            mock.patch.object(hook.select, "KQ_FILTER_PROC", 20, create=True),
            mock.patch.object(hook.select, "KQ_NOTE_EXIT", 1, create=True),
            mock.patch.object(hook.select, "KQ_EV_ADD", 2, create=True),
            mock.patch.object(hook.select, "KQ_EV_ENABLE", 4, create=True),
            mock.patch.object(hook.select, "kevent", kevent, create=True),
        ):
            woke = watch.wait([4321], 1)
        watch.close()
        self.assertTrue(woke)
        self.assertTrue(seen.get("changes"))

    def test_a_live_process_with_init_as_parent_is_not_dead(self):
        snap = {"ppid": 1, "lstart": "then", "comm": "kiro-cli-chat"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        ):
            self.assertFalse(hook.owner_is_dead(50, "100.000000"))
            self.assertTrue(hook.owner_is_dead(50, "200.000000"))
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "_linux_start_ticks", return_value=None),
        ):
            self.assertFalse(hook.owner_is_dead(50, "sec:then"))
            self.assertTrue(hook.owner_is_dead(50, "then"))
            self.assertFalse(hook.owner_is_dead(50, ""))

    def test_exit_prompt_text_does_not_release_a_live_owner(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        hook.begin_tick()
        pinned = pin_owner(owner.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 1

        hook.update(path, seed)
        screen = "› ask a question or describe a task\n Press Ctrl+C or Ctrl+D again to exit\n"

        waits = {"n": 0}

        def fake_wait(_self, _pids, _timeout):
            waits["n"] += 1
            if waits["n"] > 2:
                raise SystemExit("stop")
            return False

        with (
            mock.patch.object(hook, "pane_text", lambda _pane: screen),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "working", "id": "workflow_banner_working", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("w1:p9", path)
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertIn("sess", saved["busy"])
        self.assertNotEqual(saved["watcher"], 0)
        owner.kill()

    def test_no_owner_and_sustained_not_found_exits(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda _state: None)
        hits = {"n": 0}

        def fake_wait(_self, _pids, _timeout):
            hits["n"] += 1
            if hits["n"] > 6:
                raise AssertionError("watcher stayed up with no owner")
            return False

        with (
            mock.patch.object(hook, "pane_text", lambda _pane: "plain shell\n"),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "gone"),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook, "GONE_CONFIRM_SECS", 0),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
        ):
            hook.watch_loop("w1:p9", path)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 0)

    def test_later_busy_event_records_a_fine_token(self):
        # The first event can see no start token. A later busy event for the
        # session already in busy has to store a fine one. sec: plus lstart
        # is not that token.
        calls = {"n": 0}

        def hires(_pid):
            calls["n"] += 1
            if calls["n"] == 1:
                return ""
            return "100.000000"

        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "hires_start", hires),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
        ):
            state = hook.apply_event(
                hook.fresh_state(),
                "SessionStart",
                "sess",
                1.0,
                owner=50,
                owner_started="10",
            )
            self.assertEqual(state["owner_ident"], {})
            self.assertEqual(state["owner_started"].get("sess"), "10")
            state = hook.apply_event(state, "UserPromptSubmit", "sess", 2.0, owner=50, owner_started="10")
        self.assertEqual(state["owner_ident"].get("sess"), "100.000000")

    def test_later_prompt_drops_a_sec_owner_ident(self):
        # A stored sec: token is not the owner. The next prompt must not
        # leave it in place, and alive stays false for that token.
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": 50}
        state["owner_started"] = {"sess": "10"}
        state["owner_ident"] = {"sess": "sec:10"}
        state["generation"] = 1
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
        ):
            state = hook.apply_event(state, "UserPromptSubmit", "sess", 2.0, owner=50, owner_started="10")
            self.assertFalse(hook.alive(50, "sec:10"))
        self.assertIn("sess", state["busy"])
        self.assertNotIn("sess", state["owner_ident"])

    def test_chat_pid_death_is_owner_exit_after_confirm(self):
        state = hook.fresh_state()
        state["owners"] = {"sess": 50}
        state["owner_started"] = {"sess": "outer"}
        state["owner_ident"] = {"sess": "100.000000"}
        state["chat_pid"] = 60
        state["chat_started"] = "chat"
        state["chat_ident"] = "200.000000"

        def snap(pid):
            if pid == 50:
                return {"ppid": 1, "lstart": "outer", "comm": "kiro-cli"}
            return None

        def hires(pid):
            if int(pid) == 50:
                return "100.000000"
            return ""

        with (
            mock.patch.object(hook, "process_snapshot", snap),
            mock.patch.object(hook, "hires_start", hires),
            mock.patch.object(hook, "chat_under", lambda _pid: None),
        ):
            kind, hits = hook.tracked_exit(state, 0)
            self.assertIsNone(kind)
            kind, hits = hook.tracked_exit(state, hits)
            self.assertIsNone(kind)
            kind, _hits = hook.tracked_exit(state, hits)
            self.assertEqual(kind, "chat")

    def test_explain_failure_under_a_banner_stays_working(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        hook.begin_tick()
        pinned = pin_owner(owner.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 1

        hook.update(path, seed)
        reports = []

        def fake_wait(_self, _pids, _timeout):
            if len(reports) >= 1:
                raise SystemExit("stop")
            return False

        banner = "▶ two-step running 0/2 · ctrl+x expand · ctrl+g monitor\n"
        with (
            mock.patch.object(hook, "pane_text", lambda _pane: banner),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", lambda *args, **_kwargs: reports.append(args) or True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "unknown", "id": None, "ok": False}),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("w1:p9", path)
        self.assertTrue(any(len(args) >= 3 and args[2] == "working" for args in reports))
        owner.kill()

    def test_dead_owner_does_not_spin_when_another_watcher_holds_the_slot(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        hook.begin_tick()
        pinned = pin_owner(owner.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 4

        hook.update(path, seed)
        calls = []
        phase = {"n": 0}

        def staged(_self, _pids, _timeout):
            phase["n"] += 1
            if phase["n"] == 1:
                return False
            if phase["n"] == 2:
                owner.kill()
                owner.wait(timeout=5)

                def steal(state):
                    state["watcher"] = 4242

                hook.update(path, steal)
                return True
            raise AssertionError("watcher kept calling herdr after the owner died")

        banner = "▶ two-step running 0/2 · ctrl+x expand · ctrl+g monitor\n"
        with (
            mock.patch.object(hook, "pane_text", lambda _pane: banner),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", lambda *args, **_kwargs: calls.append(args) or True),
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "working", "id": "workflow_banner_working", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", staged),
        ):
            hook.watch_loop("w1:p9", path)
        self.assertLess(len(calls), 15)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 4242)

    def test_slot_holder_gone_is_esrch_only(self):
        import errno

        state = {"watcher": 4242}
        with mock.patch.object(hook.os, "kill", side_effect=OSError(errno.EPERM, "no")):
            self.assertFalse(hook.slot_holder_gone(state))
        with mock.patch.object(hook.os, "kill", side_effect=OSError(errno.ESRCH, "gone")):
            self.assertTrue(hook.slot_holder_gone(state))
        state["watcher"] = os.getpid()
        self.assertFalse(hook.slot_holder_gone(state))
        state["watcher"] = -1
        self.assertFalse(hook.slot_holder_gone(state))

    def test_dead_slot_holder_exits_without_waiting_for_the_owner(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        dead = subprocess.Popen(["sleep", "30"])
        dead.kill()
        dead.wait(timeout=5)
        hook.begin_tick()
        pinned = pin_owner(owner.pid)
        pinned.start()
        self.addCleanup(pinned.stop)

        def seed(state):
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
            state["owner_started"] = {"sess": ""}
            state["owner_ident"] = {"sess": hook.hires_start(state["owners"]["sess"])}
            state["generation"] = 4

        hook.update(path, seed)
        calls = []
        logs = []
        phase = {"n": 0}

        def staged(_self, _pids, _timeout):
            phase["n"] += 1
            if phase["n"] == 1:
                return False
            if phase["n"] == 2:

                def steal(state):
                    state["watcher"] = dead.pid

                hook.update(path, steal)
                return True
            raise AssertionError("watcher kept waiting after the holder was gone")

        started = time.time()
        banner = "▶ two-step running 0/2 · ctrl+x expand · ctrl+g monitor\n"
        with (
            mock.patch.object(hook, "pane_text", lambda _pane: banner),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", lambda *args, **_kwargs: calls.append(args) or True),
            mock.patch.object(hook, "herdr", lambda *args: calls.append(args) or True),
            mock.patch.object(hook, "log", logs.append),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "working", "id": "workflow_banner_working", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", staged),
        ):
            hook.watch_loop("w1:p9", path)
        elapsed = time.time() - started
        self.assertLess(elapsed, 1.0)
        self.assertLess(len(calls), 8)
        self.assertIn("watcher exit slot holder gone", logs)
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["watcher"], dead.pid)
        self.assertIn("sess", saved["busy"])
        owner.kill()
        owner.wait(timeout=5)

    def test_sweep_honors_tmpdir_and_removes_old_rule_dirs(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        notes = os.path.join(base, "herdr-kiro-notes")
        os.makedirs(notes)
        kept = os.path.join(notes, "note.txt")
        with open(kept, "w", encoding="utf-8") as handle:
            handle.write("user")
        old_stamp = time.time() - 180
        os.utime(notes, (old_stamp, old_stamp))
        similar = os.path.join(base, "herdr-kiro-rules-999999-left")
        os.makedirs(similar)
        owned = os.path.join(base, "herdr-kiro-rules-999999-ours")
        os.makedirs(owned)
        with open(os.path.join(owned, "herdr-kiro-owned"), "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        previous = tempfile.tempdir
        tempfile.tempdir = None
        try:
            with mock.patch.dict(os.environ, {"TMPDIR": base}):
                hook.sweep_stale_rules()
        finally:
            tempfile.tempdir = previous
        self.assertTrue(os.path.isfile(kept))
        self.assertTrue(os.path.isdir(similar))
        self.assertFalse(os.path.isdir(owned))

    def test_sweep_keeps_a_dir_when_the_pid_check_is_eperm(self):
        import errno

        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        owned = os.path.join(base, "herdr-kiro-rules-999999-ours")
        os.makedirs(owned)
        with open(os.path.join(owned, "herdr-kiro-owned"), "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")

        def kill(_pid, _sig):
            raise OSError(errno.EPERM, "not permitted")

        previous = tempfile.tempdir
        tempfile.tempdir = None
        try:
            with mock.patch.dict(os.environ, {"TMPDIR": base}), mock.patch.object(hook.os, "kill", kill):
                hook.sweep_stale_rules()
        finally:
            tempfile.tempdir = previous
        self.assertTrue(os.path.isdir(owned))

    def test_session_end_clears_the_slot_like_stop(self):
        state = hook.apply_event(hook.fresh_state(), "UserPromptSubmit", "sess", 1.0)
        self.assertIn("sess", state["busy"])
        state = hook.apply_event(state, "SessionEnd", "sess", 2.0)
        self.assertNotIn("sess", state["busy"])
        self.assertEqual(state["grace_until"], 2.0 + hook.SETTLE_SECS)

    def test_sigterm_removes_the_rules_dir_and_a_dead_pid_leftover(self):
        dead = 4_000_000
        try:
            os.kill(dead, 0)
        except OSError:
            pass
        else:
            self.skipTest(f"pid {dead} is alive")
        stale = f"/tmp/herdr-kiro-rules-{dead}-leftover"
        os.makedirs(stale, exist_ok=True)
        with open(os.path.join(stale, "herdr-kiro-owned"), "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        previous_env = hook._EXPLAIN_ENV
        previous_dir = hook._RULES_DIR
        hook.clear_explain_cache()
        rules = ""
        try:
            hook.explain_env()
            if isinstance(hook._RULES_DIR, str):
                rules = hook._RULES_DIR
            self.assertTrue(os.path.isdir(rules))
            self.assertFalse(os.path.isdir(stale))
            with self.assertRaises(SystemExit):
                hook._on_term(signal.SIGTERM, None)
            self.assertFalse(os.path.isdir(rules))
        finally:
            if rules and os.path.isdir(rules):
                shutil.rmtree(rules, True)
            hook._EXPLAIN_ENV = previous_env
            hook._RULES_DIR = previous_dir

    def test_pane_id_and_watch_path_stay_inside_the_state_dir(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        state_home = os.path.join(base, "state")
        outside = os.path.join(base, "outside.json")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret")
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = state_home
        try:
            self.assertIsNone(hook.resolved_state_file("../etc/passwd"))
            self.assertIsNone(hook.resolved_state_file(".."))
            self.assertIsNone(hook.resolved_state_file("a/b"))
            self.assertIsNone(hook.resolved_state_file("a b"))
            self.assertIsNone(hook.resolved_state_file(""))
            self.assertIsNone(hook.resolved_state_file("a" * 65))
            self.assertIsNone(hook.resolved_state_file("-watch"))
            self.assertIsNone(hook.resolved_state_file("-"))
            self.assertTrue(hook.pane_id_ok("w1-p1"))
            path = hook.resolved_state_file("w1:p1")
            self.assertTrue(path.endswith(os.path.join("herdr-kiro", "w1_p1.json")))
            self.assertTrue(hook.inside_state_dir(path))
            env = dict(os.environ)
            script = os.path.abspath(hook.__file__)
            completed = subprocess.run(
                [sys.executable, script, "--watch", "w1:p1", outside],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            with open(outside, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "secret")
            linked = os.path.join(hook.state_dir(), "w1_p1.json")
            os.symlink(outside, linked)
            completed = subprocess.run(
                [sys.executable, script, "--watch", "w1:p1", linked],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            with open(outside, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "secret")
            env["HERDR_ENV"] = "1"
            env["HERDR_PANE_ID"] = "../escaped"
            completed = subprocess.run(
                [sys.executable, script, "SessionStart"],
                env=env,
                input="{}",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(base, "escaped.json")))
            env["HERDR_PANE_ID"] = "-watch"
            completed = subprocess.run(
                [sys.executable, script, "SessionStart"],
                env=env,
                input="{}",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(hook.state_dir(), "-watch.json")))
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous

    def test_screen_text_is_written_in_the_state_dir(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        seen = {}
        real_run = subprocess.run

        def run(args, **kwargs):
            if args and args[0] == "ps":
                return real_run(args, **kwargs)
            path = args[args.index("--file") + 1]
            seen["path"] = path
            self.assertEqual(
                os.path.dirname(os.path.realpath(path)),
                os.path.realpath(hook.state_dir()),
            )
            self.assertTrue(os.path.basename(path).startswith("herdr-kiro-screen-"))
            self.assertTrue(os.path.isfile(path))
            return subprocess.CompletedProcess(
                args, 0, stdout='{"state":"idle","matched_rule":{"id":"live_prompt_idle"}}'
            )

        previous_state = os.environ.get("XDG_STATE_HOME")
        previous_env = hook._EXPLAIN_ENV
        previous_dir = hook._RULES_DIR
        hook._SCREEN_CACHE.clear()
        hook.clear_explain_cache()
        os.environ["XDG_STATE_HOME"] = os.path.join(base, "xdg")
        rules = ""
        try:
            with mock.patch.object(hook.subprocess, "run", run):
                view = hook.screen_view("pane text")
            if isinstance(hook._RULES_DIR, str):
                rules = hook._RULES_DIR
        finally:
            if previous_state is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous_state
            hook._SCREEN_CACHE.clear()
            if rules and os.path.isdir(rules):
                hook.remove_tree(rules)
            hook._EXPLAIN_ENV = previous_env
            hook._RULES_DIR = previous_dir
        self.assertEqual(view["id"], "live_prompt_idle")
        self.assertFalse(os.path.exists(seen["path"]))

    def test_hires_start_is_finer_than_a_second_and_comm_must_match(self):
        hook.begin_tick()
        ident = hook.watcher_identity(os.getpid())
        self.assertIsNotNone(ident)
        if sys.platform == "darwin":
            self.assertRegex(ident["start"], r"^sec:\d+$")
        else:
            self.assertRegex(ident["start"], r"^\d+\.\d{6}$")
        self.assertTrue(ident["comm"])
        self.assertTrue(hook.watcher_matches(os.getpid(), str(os.getuid()), ident["start"], ident["comm"]))
        self.assertFalse(hook.watcher_matches(os.getpid(), str(os.getuid()), ident["start"], "not-the-comm"))
        self.assertFalse(hook.watcher_matches(os.getpid(), str(os.getuid()), "1", ident["comm"]))
        self.assertFalse(hook.watcher_matches(os.getpid(), "0", ident["start"], ident["comm"]))

    def test_macos_start_token_comes_from_ps(self):
        pid = 4242
        stdout = f" {pid} 7 Mon Nov  2 01:02:03 2026 /usr/bin/python3\n"
        completed = subprocess.CompletedProcess(["ps", "-eo", "pid=,ppid=,lstart=,comm="], 0, stdout=stdout, stderr="")

        def run(args, **_kwargs):
            if args and args[0] == "ps":
                return completed
            raise AssertionError(args)

        hook.begin_tick()
        try:
            with (
                mock.patch.object(hook, "_linux_start_ticks", return_value=None),
                mock.patch.object(hook.subprocess, "run", side_effect=run),
            ):
                token = hook.hires_start(pid)
                ident = hook.watcher_identity(pid)
        finally:
            hook.begin_tick()
        expected = hook.start_token("Mon Nov  2 01:02:03 2026")
        self.assertEqual(token, "sec:" + expected)
        self.assertRegex(token, r"^sec:\d+$")
        self.assertEqual(ident["start"], token)
        self.assertEqual(ident["comm"], "python3")
        self.assertFalse(ident["error"])

    def test_rules_delete_does_not_follow_symlinks(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        outside = os.path.join(base, "outside")
        os.makedirs(outside)
        kept = os.path.join(outside, "keep.txt")
        with open(kept, "w", encoding="utf-8") as handle:
            handle.write("safe")
        tree = os.path.join(base, "tree")
        os.makedirs(os.path.join(tree, "nested"))
        with open(os.path.join(tree, "nested", "gone.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        os.symlink(outside, os.path.join(tree, "linkdir"))
        os.symlink(kept, os.path.join(tree, "linkfile"))
        top = os.path.join(base, "top-link")
        os.symlink(outside, top)
        hook.remove_tree(top)
        self.assertTrue(os.path.islink(top))
        hook.remove_tree(tree)
        self.assertFalse(os.path.exists(tree))
        with open(kept, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "safe")
        self.assertTrue(os.path.isdir(outside))

    def test_idle_after_working_or_blocked_is_one_report(self):
        # herdr 0.9.5 maps an unseen Working or Blocked -> Idle report to
        # agent_status done (api_helpers.rs 96-107). Explain still returns
        # the screen rule. That split is the finished-not-yet-seen status.
        # The idle report is the completion. An unknown report in front of
        # it is a different transition and hides done.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")

        def seed(state, reported):
            state["reported"] = reported
            state["seq"] = 4

        def states_for(reported, kind):
            hook.update(path, lambda state: seed(state, reported))
            calls = []

            def fake_herdr(*args):
                calls.append(args)
                return True

            with mock.patch.object(hook, "herdr", fake_herdr):
                self.assertTrue(hook.send_report("w1:p1", path, kind))
            return [args[args.index("--state") + 1] for args in calls]

        self.assertEqual(states_for("working", "idle"), ["idle"])
        self.assertEqual(states_for("blocked", "idle"), ["idle"])
        self.assertEqual(states_for("idle", "idle"), ["idle"])
        self.assertEqual(states_for("working", "working"), ["working"])

    def test_finished_turn_stays_done_until_seen_and_does_not_release(self):
        # done is idle with seen false. Working|Blocked -> Idle on a
        # background pane clears seen (actions.rs 22-29, 1846-1847).
        # Marking the pane seen flips get back to idle and leaves the
        # completion in place. release-agent after that idle drops done
        # on the herdr 0.9.5 binary, so a finished turn does not release.
        for prior in ("working", "blocked"):
            pane = _ScreenCompletion()
            pane.screen(prior)
            directory = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, directory, True)
            path = os.path.join(directory, "pane.json")

            def seed(state, prior=prior):
                state["reported"] = prior
                state["seq"] = 4
                state["watcher"] = os.getpid()
                state["generation"] = 1
                state["busy"] = {}

            hook.update(path, seed)
            calls = []

            def fake_herdr(*args, pane=pane, calls=calls):
                calls.append(args)
                if args[0] == "report-agent":
                    pane.report(args[args.index("--state") + 1])
                elif args[0] == "release-agent":
                    pane.release()
                return True

            with mock.patch.object(hook, "herdr", fake_herdr):
                self.assertTrue(hook.send_report("w1:p1", path, "idle"))
                self.assertTrue(
                    hook.finish_if_unchanged(
                        "w1:p1",
                        path,
                        {"generation": 1, "busy": {}},
                        "idle",
                    )
                )
            pane.screen("idle")
            self.assertEqual(
                [args[args.index("--state") + 1] for args in calls if args[0] == "report-agent"],
                ["idle"],
            )
            self.assertFalse(any(args[0] == "release-agent" for args in calls))
            self.assertEqual(pane.status(), "done")
            self.assertEqual(pane.completion, "idle")
            pane.see()
            self.assertEqual(pane.status(), "idle")
            self.assertEqual(pane.completion, "idle")

    def test_app_hc_read_failure_does_not_emit_done_via_app_quiet(self):
        # A failed App read is not an empty screen. app_quiet must not report
        # idle for that tick, and a held working banner still counts. A later
        # successful empty read may report idle. That hook word is not a release.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        hook.update(path, lambda state: state.update({"busy": {}, "grace_until": 0, "reported": "working"}))
        banner = "▶ two-step running 0/2 · ctrl+x expand · ctrl+g monitor\n"
        pending = [None, banner, None, ""]
        reports = []
        releases = []
        pane_reads = {"n": 0}
        clock = {"now": 1000.0}

        def hc_screen():
            clock["now"] += 10
            if not pending:
                raise SystemExit("once")
            return pending.pop(0)

        def view(text):
            if "running" in (text or ""):
                return {"state": "working", "id": "workflow_banner_working", "ok": True}
            return {"state": "idle", "id": None, "ok": True}

        def pane_read(*_args, **_kwargs):
            pane_reads["n"] += 1
            raise subprocess.SubprocessError("pane read")

        def report(*args, **_kwargs):
            reports.append(args[2])
            return True

        def release(*_args, **_kwargs):
            releases.append("release")
            return True

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": os.path.join(directory, "hook.log"),
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_screen),
            mock.patch.object(hook.subprocess, "check_output", pane_read),
            mock.patch.object(hook, "process_table", lambda: {}),
            mock.patch.object(hook, "herdr", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "screen_view", view),
            mock.patch.object(hook, "send_report", report),
            mock.patch.object(hook, "release_authority", release),
            mock.patch.object(hook.time, "time", lambda: clock["now"]),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda _self, _pids, _timeout: False),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("t7", path)
        self.assertEqual(reports[0], "working")
        self.assertEqual(reports[-1], "idle")
        self.assertEqual(reports.count("idle"), 1)
        self.assertEqual(pane_reads["n"], 0)
        self.assertEqual(releases, [])

    def test_tui_inside_app_failed_read_is_not_app_quiet(self):
        # A failed herdr pane read is "". With HC_TERMINAL_ID also set, that
        # string must not count as an empty App screen. The TUI quiet rule
        # still reports idle when the prompt is up, and that report does not
        # write OSC or release. HC_TERMINAL_ID alone still skips a failed hc read.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-tui-quiet-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda state: state.update({"busy": {}, "grace_until": 0, "reported": "working"}))
        prompt = "ask a question or describe a task\n"
        both = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_BIN_PATH": "",
            "HERDR_SESSION": "",
            "HERDR_KIRO_LOG": os.path.join(directory, "hook.log"),
            "XDG_STATE_HOME": directory,
        }
        clock = {"now": 1000.0}
        reads = {"n": 0}
        hc = {"n": 0}
        calls = []
        osc = []

        def pane_read(*_args, **_kwargs):
            reads["n"] += 1
            clock["now"] += 10
            if reads["n"] > 4:
                raise SystemExit("once")
            if reads["n"] == 1:
                raise subprocess.SubprocessError("pane read")
            return prompt

        def hc_screen():
            hc["n"] += 1
            raise AssertionError("hc read")

        def fake_herdr(*args):
            calls.append((reads["n"], tuple(args)))
            return True

        def capture(payload, _pids):
            osc.append(payload)
            return "/dev/pts/0"

        def view(_text):
            return {"state": "idle", "id": None, "ok": True}

        def refuse_run(*_args, **_kwargs):
            raise AssertionError("command")

        with (
            mock.patch.dict(os.environ, both, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_screen),
            mock.patch.object(hook.subprocess, "check_output", pane_read),
            mock.patch.object(hook.subprocess, "run", refuse_run),
            mock.patch.object(hook, "process_table", lambda: {}),
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "screen_view", view),
            mock.patch.object(hook.herdr_kiro_osc, "write_to_pane", capture),
            mock.patch.object(hook.time, "time", lambda: clock["now"]),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda _self, _pids, _timeout: False),
        ):
            hook.watch_loop("w1:p1", path)
        idle = [(tick, args[args.index("--state") + 1]) for tick, args in calls if args[0] == "report-agent"]
        self.assertEqual(idle, [(2, "idle")])
        self.assertEqual([args[1] for _tick, args in calls if args[0] == "report-agent"], ["w1:p1"])
        self.assertFalse(any(args[0] == "release-agent" for _tick, args in calls))
        self.assertEqual(osc, [])
        self.assertEqual(hc["n"], 0)

        app_dir = tempfile.mkdtemp(prefix="herdr-kiro-app-quiet-")
        self.addCleanup(shutil.rmtree, app_dir, True)
        app_path = os.path.join(app_dir, "t7.json")
        hook.update(app_path, lambda state: state.update({"busy": {}, "grace_until": 0, "reported": "working"}))
        app = dict(both)
        app["HERDR_ENV"] = ""
        app["HERDR_PANE_ID"] = ""
        app["HERDR_KIRO_LOG"] = os.path.join(app_dir, "hook.log")
        app["XDG_STATE_HOME"] = app_dir
        calls.clear()
        osc.clear()
        hc["n"] = 0
        clock["now"] = 1000.0

        def hc_fail():
            hc["n"] += 1
            clock["now"] += 10
            if hc["n"] > 1:
                raise SystemExit("once")
            return

        def refuse_pane(*_args, **_kwargs):
            raise AssertionError("pane read")

        with (
            mock.patch.dict(os.environ, app, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_fail),
            mock.patch.object(hook.subprocess, "check_output", refuse_pane),
            mock.patch.object(hook.subprocess, "run", refuse_run),
            mock.patch.object(hook, "process_table", lambda: {}),
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "screen_view", view),
            mock.patch.object(hook.herdr_kiro_osc, "write_to_pane", capture),
            mock.patch.object(hook.time, "time", lambda: clock["now"]),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda _self, _pids, _timeout: False),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("t7", app_path)
        self.assertEqual(calls, [])
        self.assertEqual(osc, [])
        self.assertGreaterEqual(hc["n"], 1)


class _ScreenCompletion:
    """Background pane. Hook overrides fallback until release exposes it."""

    def __init__(self):
        self.fallback = "unknown"
        self.hook = None
        self.state = "unknown"
        self.seen = True
        self.completion = None

    def status(self):
        if self.state == "idle" and not self.seen:
            return "done"
        return self.state

    def _transition(self, new):
        previous = self.state
        if new == previous:
            return
        if new == "idle" and previous in ("working", "blocked"):
            self.seen = False
            self.completion = new
        elif new != "idle":
            self.seen = True
        self.state = new

    def _recompute(self):
        self._transition(self.hook if self.hook is not None else self.fallback)

    def screen(self, kind):
        self.fallback = kind
        self._recompute()

    def report(self, kind):
        self.hook = kind
        self._recompute()

    def release(self):
        self.hook = None
        self._recompute()

    def see(self):
        self.seen = True


if __name__ == "__main__":
    unittest.main()
