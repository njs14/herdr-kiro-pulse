"""Grace rules for the Kiro hook.

The hook sends one working report. It does not send that report again for
the rest of a SessionStart grace. A named Stop does not start that grace.
"""

import ast
import builtins
import contextlib
import copy
import errno
import inspect
import io
import json
import os
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prototype"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _state_guard
import herdr_kiro_state as hook

_ISOLATION = _state_guard.Isolation(__name__)


def setUpModule():
    _ISOLATION.start()


def tearDownModule():
    _ISOLATION.stop()


def pinned_start(real, pins, asked):
    """Fractional token for a still-running pinned pid.

    A dead pid keeps no pin. A reused pid number gets the real start,
    including sec: plus a whole-second lstart, and that stays unproven.
    """
    try:
        number = int(asked)
    except (TypeError, ValueError):
        return real(asked)
    pinned = pins.get(number)
    if pinned is None:
        return real(asked)
    token, proc = pinned
    if proc.poll() is not None:
        return real(asked)
    return token


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


def reset_explain_env():
    hook._EXPLAIN_ENV = None
    hook._RULES_DIR = None


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


class GraceTests(unittest.TestCase):
    def test_old_policy_is_idle_during_the_grace(self):
        reports, drop_at = simulate(old_policy)
        self.assertEqual(effective(reports, drop_at, 2.0), "idle")

    def test_one_working_report_and_the_session_start_grace(self):
        # A named Stop does not arm the grace. The SessionStart count
        # returning to zero does, and a later Stop does not arm it again.
        named = hook.apply_event(hook.fresh_state(), "UserPromptSubmit", "parent", 0.0)
        named = hook.apply_event(named, "Stop", "parent", 1.0)
        self.assertEqual(named["grace_until"], 0.0)
        self.assertEqual(hook.next_action(0, named["grace_until"], False, 1.1), "idle")
        state = hook.fresh_state()
        state = hook.apply_event(state, "SessionStart", hook.AGGREGATE, 0.0)
        state = hook.apply_event(state, "Stop", hook.AGGREGATE, 1.0)
        self.assertEqual(state["grace_until"], 1.0 + hook.SETTLE_SECS)
        self.assertEqual(hook.next_action(0, state["grace_until"], False, 1.2), "working")
        again = hook.apply_event(state, "Stop", hook.AGGREGATE, 1.3)
        self.assertEqual(again["grace_until"], 1.0 + hook.SETTLE_SECS)
        self.assertEqual(
            hook.next_action(0, again["grace_until"], False, again["grace_until"] + 0.05),
            "idle",
        )

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
        self.assertEqual(state["grace_until"], 0.0)
        self.assertEqual(hook.next_action(0, state["grace_until"], False, 4.05), "idle")

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
        key = hook.session_of({"cwd": "/work", "hook_event_name": "SessionStart"})
        self.assertEqual(key, hook.AGGREGATE)
        self.assertEqual(key, hook.session_of({"cwd": "/work"}))
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
        first = hook.session_of({"transcript_path": "/tmp/one.jsonl", "cwd": "/work"})
        second = hook.session_of({"transcript_path": "/tmp/two.jsonl", "cwd": "/work"})
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, "unknown")
        self.assertEqual(hook.session_of({"session_id": "sess_a", "cwd": "/work"}), "sess_a")

    def test_repeat_gap_is_inside_the_grace(self):
        state = hook.fresh_state()
        state = hook.apply_event(state, "UserPromptSubmit", "step-1", 0)
        state = hook.apply_event(state, "Stop", "step-1", 2)
        self.assertEqual(state["grace_until"], 0.0)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 2.1),
            "idle",
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

        previous_delay = hook.IDLE_DELAY
        hook.IDLE_DELAY = 0
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
            hook.IDLE_DELAY = previous_delay
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

        def touch(state):
            state["grace_until"] = 1

        state = hook.update(path, touch)
        self.assertGreaterEqual(state["seq"], 50)
        with open(path, "rb") as handle:
            saved = handle.read()
        hook.update(path, lambda _state: None)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), saved)

    def test_state_file_is_private(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def touch(state):
            state["grace_until"] = 1

        hook.update(path, touch)
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

    def test_a_live_unproven_pid_is_alive_and_not_painted(self):
        def snap(_pid):
            return {"ppid": 1, "lstart": "NOW", "comm": "python"}

        with (
            mock.patch.object(hook, "process_snapshot", snap),
            mock.patch.object(hook, "_linux_start_ticks", return_value=None),
        ):
            self.assertTrue(hook.alive(50))
            self.assertTrue(hook.alive(50))
            self.assertTrue(hook.alive(50))
            self.assertTrue(hook.alive(50))
            self.assertFalse(hook.identity_proven(50, "sec:NOW"))
            self.assertFalse(hook.identity_proven(50, "NOW"))
            self.assertFalse(hook.fine_start("NOW"))
            self.assertFalse(hook.fine_start("sec:NOW"))
        with (
            mock.patch.object(hook, "process_snapshot", snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        ):
            self.assertTrue(hook.alive(50))
            self.assertTrue(hook.alive(50))
            self.assertTrue(hook.identity_proven(50, "100.000000"))
            self.assertFalse(hook.identity_proven(50, "200.000000"))

    def test_pane_transition_covers_every_pair(self):
        states = ("no_owner", "proven", "weak", "exited", "idle_clear")
        events = (
            "start",
            "prompt",
            "stop",
            "idle_clear",
            "process_exit",
            "pid_reuse",
            "piped_chat",
            "blocked",
            "aggregate_zero",
            "record_aggregate",
            "touch_aggregate",
            "touch_partial",
            "record_named",
            "reap",
        )
        working = {("proven", "start"), ("proven", "prompt")}
        done = {
            ("proven", "stop"),
            ("proven", "idle_clear"),
            ("proven", "process_exit"),
            ("proven", "piped_chat"),
            ("idle_clear", "stop"),
            ("idle_clear", "idle_clear"),
            ("idle_clear", "process_exit"),
            ("idle_clear", "piped_chat"),
            ("no_owner", "stop"),
            ("no_owner", "piped_chat"),
        }
        slots = {"record_aggregate", "touch_aggregate", "touch_partial", "record_named"}
        self.assertEqual(hook.OWNER_STATES, states)
        self.assertEqual(hook.OWNER_EVENTS, events)
        self.assertEqual(len(states) * len(events), 70)
        for state in states:
            for event in events:
                got = hook.pane_transition(state, event)
                pair = (state, event)
                if pair in working:
                    self.assertEqual(got, "working", pair)
                elif pair in done:
                    self.assertEqual(got, "done", pair)
                elif pair == ("proven", "blocked"):
                    self.assertEqual(got, "paint_blocked", pair)
                elif pair == ("proven", "aggregate_zero"):
                    self.assertEqual(got, "refresh", pair)
                elif event == "reap":
                    self.assertEqual(got, "reap", pair)
                elif event in slots:
                    self.assertEqual(got, event, pair)
                else:
                    self.assertEqual(got, "none", pair)

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
        self.assertFalse(hook.gone_confirmed(10.0, 10.2, 1))
        self.assertTrue(hook.gone_confirmed(10.0, 12.0, 3))
        self.assertFalse(hook.gone_confirmed(None, 0.0, 0))

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
        self.assertIn("sess", saved["busy"])
        self.assertEqual(saved.get("phase"), "")

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

    def test_dead_owner_stays_until_the_exit_write(self):
        state = hook.apply_event(hook.fresh_state(), "UserPromptSubmit", "sess", 1.0, owner=424242, owner_started="old")
        state["phase"] = "proven"
        state["owner_ident"] = {"sess": "100.000000"}

        def gone(_pid):
            return None

        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def seed(current):
            current.clear()
            current.update(state)

        with mock.patch.object(hook, "process_snapshot", gone):
            first = hook.update(path, seed)
            second = hook.update(path, lambda _current: None)
        self.assertEqual(second["owners"].get("sess"), 424242)
        self.assertIn("sess", second["busy"])
        self.assertEqual(first["phase"], "proven")

        def refuse(*_args, **_kwargs):
            return False

        with (
            mock.patch.object(hook, "process_snapshot", gone),
            mock.patch.object(hook, "paint_status", refuse),
            mock.patch.object(hook, "_write_held_fd", refuse),
        ):
            write, ok = hook.commit_pane(second, "process_exit")
        self.assertEqual((write, ok, second["phase"]), ("done", False, "proven"))
        self.assertEqual(second["owners"].get("sess"), 424242)

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
        self.assertTrue(shutil.which("herdr"), "herdr is not on PATH")
        screen = (
            " ▶ approve-probe running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            " ⚠ 1 tool approval pending\n"
            " ❯ (a) Approve all pending\n"
            "   (f) Approve all pending and auto-approve future subagent requests\n"
            "   (c) Configure individually (agent monitor)\n"
            "   (x) Exit (cancel subagents)\n"
        )
        self.assertTrue(hook.workflow_banner_from(screen))
        view = hook.screen_view(screen)
        self.assertEqual(view["state"], "blocked")
        self.assertEqual(view["id"], "crew_approval")
        snap = hook.ScreenSnapshot(screen, view)
        self.assertTrue(hook.blocker_visible(snap))
        self.assertFalse(hook.screen_is_settled(snap))
        self.assertEqual(hook.report_for(snap, "working"), "blocked")

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
            self.assertEqual(hook.kiro_lineage(), (9, "outer", 10, "chat"))

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
            self.assertIsNotNone(hook.process_snapshot(50))
            self.assertIsNotNone(hook.process_snapshot(50))
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "_linux_start_ticks", return_value=None),
        ):
            self.assertIsNotNone(hook.process_snapshot(50))
            self.assertIsNotNone(hook.process_snapshot(50))
            self.assertIsNotNone(hook.process_snapshot(50))

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

        released = []

        def release(*_args, **_kwargs):
            released.append("release")
            return True

        with (
            mock.patch.object(hook, "pane_text", lambda _pane: screen),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "release_authority", release),
            mock.patch.object(
                hook, "screen_view", lambda _text: {"state": "working", "id": "workflow_banner_working", "ok": True}
            ),
            mock.patch.object(hook.OwnerExitWatch, "wait", fake_wait),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("w1:p9", path)
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        # Count is zero, so the banner is refresh. Refresh leaves the watcher
        # unset and does not release the live owner.
        self.assertIn("sess", saved["busy"])
        self.assertEqual(saved["owners"]["sess"], owner.pid)
        self.assertEqual(saved["watcher"], 0)
        self.assertEqual(released, [])
        owner.kill()

    def test_no_owner_and_sustained_not_found_exits(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")

        def seed(state):
            state["seq"] = 1

        hook.update(path, seed)
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
            self.assertEqual(state["owners"].get("sess"), 50)
            self.assertNotIn("owner_started", state)
            state = hook.apply_event(state, "UserPromptSubmit", "sess", 2.0, owner=50, owner_started="10")
        self.assertEqual(state["owner_ident"].get("sess"), "100.000000")

    def test_later_prompt_drops_a_sec_owner_ident(self):
        # A stored sec: token is not the owner. The next prompt must not
        # leave it in place. The live pid is not an exit.
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": 50}
        state["owner_ident"] = {"sess": "sec:10"}
        state["generation"] = 1
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
        ):
            state = hook.apply_event(state, "UserPromptSubmit", "sess", 2.0, owner=50, owner_started="10")
            self.assertTrue(hook.alive(50))
            self.assertEqual(hook.owner_phase(state), "weak")
        self.assertIn("sess", state["busy"])
        self.assertNotIn("sess", state["owner_ident"])

    def test_a_gone_chat_child_is_not_an_owner_exit(self):
        state = hook.fresh_state()
        state["owners"] = {"sess": 50}
        state["owner_ident"] = {"sess": "100.000000"}
        state["chat_pid"] = 60
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
        ):
            self.assertEqual(hook.owner_phase(state), "proven")
            self.assertTrue(hook.alive(50))
            self.assertFalse(hook.alive(60))

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
            state["aggregate"] = 1
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": owner.pid}
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
        self.assertEqual(state["grace_until"], 0.0)

    def test_sigterm_removes_the_rules_dir_and_a_dead_pid_leftover(self):
        dead = None
        for candidate in range(4_194_303, 4_190_000, -1):
            try:
                os.kill(candidate, 0)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    dead = candidate
                    break
        self.assertIsNotNone(dead, "no dead pid in range")
        stale = f"/tmp/herdr-kiro-rules-{dead}-leftover"
        os.makedirs(stale, exist_ok=True)
        with open(os.path.join(stale, "herdr-kiro-owned"), "w", encoding="utf-8") as handle:
            handle.write(f"uid {os.getuid()}\n")
        previous_env = hook._EXPLAIN_ENV
        previous_dir = hook._RULES_DIR
        reset_explain_env()
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
        reset_explain_env()
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

    def test_watcher_identity_start_follows_proc_ticks(self):
        hook.begin_tick()
        ident = hook.watcher_identity(os.getpid())
        self.assertIsNotNone(ident)
        if hook._linux_start_ticks(os.getpid()):
            self.assertRegex(ident["start"], r"^\d+\.\d{6}$")
        else:
            self.assertRegex(ident["start"], r"^sec:\d+$")
        self.assertTrue(ident["comm"])

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

        def seed(state):
            state["seq"] = 4

        def states_for(kind):
            hook.update(path, seed)
            calls = []

            def fake_herdr(*args):
                calls.append(args)
                return True

            with mock.patch.object(hook, "herdr", fake_herdr):
                self.assertTrue(hook.send_report("w1:p1", path, kind))
            return [args[args.index("--state") + 1] for args in calls]

        self.assertEqual(states_for("idle"), ["idle"])
        self.assertEqual(states_for("working"), ["working"])

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

            def seed(state):
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
        # A failed App read is none, including after a held working banner.
        # An empty read is not the Kiro CLI 3.0 chevron, so it does not end
        # the turn. The banner tick reports working. The failed reads do not.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
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
            mock.patch.object(
                hook, "process_table", lambda: {owner.pid: {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}}
            ),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "herdr", lambda *_args, **_kwargs: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "screen_view", view),
            mock.patch.object(hook, "send_report", report),
            mock.patch.object(hook, "release_authority", release),
            mock.patch.object(hook.time, "time", lambda: clock["now"]),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda _self, _pids, _timeout: False),
            self.assertRaises(SystemExit),
        ):
            hook.update(
                path,
                lambda state: state.update(
                    {
                        "aggregate": 1,
                        "busy": {},
                        "grace_until": 0,
                        "proven_pid": owner.pid,
                        "proven_ident": "100.000000",
                    }
                ),
            )
            hook.watch_loop("t7", path)
        self.assertEqual(reports, ["working"])
        self.assertNotIn("idle", reports)
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
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        prompt = "› ask a question or describe a task\n"
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

        def view(_text):
            return {"state": "idle", "id": None, "ok": True}

        def refuse_run(argv, *_args, **_kwargs):
            # A host without /proc asks lsof for stdout. An empty result
            # holds no fd. ps is not a fallback, and neither is a herdr command.
            command = argv[0] if isinstance(argv, (list, tuple)) and argv else ""
            if os.path.basename(str(command)) in ("lsof", "ps"):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            raise AssertionError("command")

        with (
            mock.patch.dict(os.environ, both, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_screen),
            mock.patch.object(hook.subprocess, "check_output", pane_read),
            mock.patch.object(hook.subprocess, "run", refuse_run),
            mock.patch.object(
                hook, "process_table", lambda: {owner.pid: {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}}
            ),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "herdr", fake_herdr),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "screen_view", view),
            mock.patch.object(hook.time, "time", lambda: clock["now"]),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda _self, _pids, _timeout: False),
        ):
            hook.update(
                path,
                lambda state: state.update(
                    {
                        "busy": {},
                        "grace_until": 0,
                        "proven_pid": owner.pid,
                        "proven_ident": "100.000000",
                    }
                ),
            )
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
        hook.update(app_path, lambda state: state.update({"busy": {}, "grace_until": 0}))
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
            mock.patch.object(hook.time, "time", lambda: clock["now"]),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda _self, _pids, _timeout: False),
            self.assertRaises(SystemExit),
        ):
            hook.watch_loop("t7", app_path)
        self.assertEqual(calls, [])
        self.assertEqual(osc, [])
        self.assertGreaterEqual(hc["n"], 1)

    def test_dead_pid_pin_does_not_make_a_sec_process_proven(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        start = "1700000000.222222"
        pins = {proc.pid: (start, proc)}

        def real(_pid):
            return "sec:10"

        self.assertEqual(pinned_start(real, pins, proc.pid), start)
        self.assertTrue(hook.fine_start(pinned_start(real, pins, proc.pid)))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        reported = pinned_start(real, pins, proc.pid)
        self.assertEqual(reported, "sec:10")
        self.assertFalse(hook.fine_start(reported))


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


class PaneFindingTests(unittest.TestCase):
    def test_the_price_line_is_not_the_idle_prompt(self):
        self.assertFalse(hook.prompt_visible("the price is $\n"))
        self.assertFalse(hook.prompt_visible("ask a question or describe a task\n"))
        self.assertFalse(hook.prompt_visible("Please ask a question or describe a task now\n"))
        self.assertTrue(hook.prompt_visible("› ask a question or describe a task\n"))

    def test_a_fractional_proc_token_is_proven(self):
        forms = (
            ("1793839197.630000", True),
            ("100.000000", True),
            ("sec:1793839197", False),
            ("1793839197", False),
            ("", False),
        )
        for token, proven in forms:
            self.assertEqual(hook.fine_start(token), proven, token)

    def test_pane_next_covers_every_pair(self):
        moves = {
            ("proven", "stop"): "idle_clear",
            ("proven", "idle_clear"): "idle_clear",
            ("proven", "piped_chat"): "idle_clear",
            ("proven", "process_exit"): "exited",
            ("proven", "pid_reuse"): "weak",
            ("weak", "process_exit"): "exited",
            ("idle_clear", "process_exit"): "exited",
            ("idle_clear", "pid_reuse"): "weak",
        }
        for state in hook.OWNER_STATES:
            for event in hook.OWNER_EVENTS:
                want = moves.get((state, event), state)
                self.assertEqual(hook.pane_next(state, event), want, (state, event))

    def test_the_hook_passes_every_event(self):
        quiet = {"chat_pid": 0}
        piped = {"chat_pid": 77}
        fifo = mock.Mock(st_mode=stat.S_IFIFO)
        rows = [
            (hook.hook_event(quiet, "SessionStart"), "start"),
            (hook.hook_event(quiet, "PostToolUse"), "start"),
            (hook.hook_event(quiet, "UserPromptSubmit"), "prompt"),
            (hook.hook_event(quiet, "Stop"), "stop"),
            (hook.hook_event(quiet, "SessionEnd"), "stop"),
            (hook.screen_event(quiet, "banner"), "start"),
            (hook.screen_event(quiet, "continue"), "prompt"),
            (hook.screen_event(quiet, "timer"), "idle_clear"),
            (hook.screen_event(quiet, "settled"), "stop"),
            (hook.screen_event(quiet, "reuse"), "pid_reuse"),
            (hook.screen_event(quiet, "exit"), "process_exit"),
        ]
        with mock.patch.object(hook.os, "stat", return_value=fifo):
            rows.append((hook.hook_event(piped, "Stop"), "piped_chat"))
            rows.append((hook.hook_event(piped, "SessionEnd"), "piped_chat"))
            rows.append((hook.screen_event(piped, "timer"), "piped_chat"))
            rows.append((hook.screen_event(piped, "settled"), "piped_chat"))
        for got, want in rows:
            self.assertEqual(got, want)

    def _live_owner(self):
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": 50}
        state["owner_ident"] = {"sess": "100.000000"}
        state["chat_pid"] = 60
        state["chat_ident"] = "200.000000"
        state["generation"] = 4
        return state

    def test_one_pane_and_the_findings(self):
        hook._close_held_fd()
        state = self._live_owner()
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}

        def hires(pid):
            number = int(pid)
            if number == 50:
                return "100.000000"
            if number == 60:
                return "200.000000"
            return ""

        painted = []

        def paint(kind, pid, token, held_fd=-1, rule_id=""):
            painted.append((kind, pid, token, held_fd, rule_id))
            return True

        with (
            mock.patch.dict(os.environ, {"HERDR_ENV": "", "HERDR_PANE_ID": "", "HC_TERMINAL_ID": "t7"}, clear=False),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", hires),
            mock.patch.object(hook, "paint_status", paint),
        ):
            self.assertTrue(hook.identity_proven(50, "100.000000"))
            self.assertFalse(hook.identity_proven(50, "sec:10"))
            self.assertFalse(hook.identity_proven(50, "10"))
            self.assertIsNotNone(hook.process_snapshot(50))
            self.assertEqual(hook.owner_phase(state), "proven")
            state["aggregate"] = 1
            write, ok = hook.commit_pane(state, "start")
            self.assertEqual((write, ok, state["phase"]), ("working", True, "proven"))
            self.assertEqual(painted, [("working", 50, "100.000000", -1, "")])
            painted.clear()
            write, _ok = hook.commit_pane(state, "idle_clear")
            self.assertEqual(write, "done")
            self.assertEqual(painted, [("idle", 50, "100.000000", -1, "")])
            self.assertEqual(state["phase"], "idle_clear")
            self.assertEqual(state["proven_pid"], 50)
            self.assertIn("sess", state["owners"])
            painted.clear()
            write, _ok = hook.commit_pane(state, "stop")
            self.assertEqual(write, "done")
            self.assertEqual(painted, [("idle", 50, "100.000000", -1, "")])
            self.assertEqual(state["phase"], "idle_clear")
            self.assertEqual(hook.pane_transition(hook.owner_phase(state), "start"), "none")
            self.assertEqual(hook.pane_transition(hook.owner_phase(state), "prompt"), "none")

    def test_tui_inside_the_app_paints_nothing(self):
        state = self._live_owner()
        env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HC_TERMINAL_ID": "t7"}
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            write, ok = hook.commit_pane(state, "idle_clear")
        self.assertEqual((write, ok), ("done", True))
        held.assert_not_called()
        paint.assert_not_called()

    def test_finish_does_not_write_done(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-finish-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda _state: None)
        with (
            mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
            mock.patch.object(hook, "release_authority"),
            mock.patch.object(hook, "herdr", lambda *_args: True),
        ):
            hook.finish_if_unchanged("t7", path, hook.fresh_state(), "owner exit")
        held.assert_not_called()

    def test_session_end_keeps_the_proven_pane(self):
        state = self._live_owner()
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "paint_status", lambda *_args, **_kwargs: True),
        ):
            event = hook.hook_event(state, "SessionEnd")
            self.assertEqual(event, "stop")
            hook.commit_pane(state, event)
            revised = hook.apply_event(state, "SessionEnd", "sess", 2.0, 50, "10", 0)
            self.assertEqual(revised["phase"], "idle_clear")
            self.assertEqual(revised["proven_pid"], 50)
            self.assertEqual(revised["owners"].get("sess"), 50)

    def test_a_live_reused_pid_stays_weak(self):
        state = self._live_owner()
        state["owner_ident"] = {"sess": "200.000000"}
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            self.assertIsNotNone(hook.process_snapshot(50))
            self.assertEqual(hook.owner_phase(state), "weak")
            write, _ok = hook.commit_pane(state, hook.screen_event(state, "reuse"))
            self.assertEqual(hook.owner_phase(state), "weak")
        self.assertEqual((write, state["phase"], state["owners"].get("sess")), ("none", "", 50))
        paint.assert_not_called()

    def test_a_stored_fine_token_is_not_replaced(self):
        state = self._live_owner()
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "200.000000"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            state = hook.apply_event(state, "UserPromptSubmit", "sess", 2.0, owner=50, owner_started="10")
            self.assertEqual(state["owner_ident"].get("sess"), "100.000000")
            self.assertEqual(hook.owner_phase(state), "weak")
            write, _ok = hook.commit_pane(state, "prompt")
        self.assertEqual(write, "none")
        paint.assert_not_called()

    def test_a_second_pid_is_not_painted(self):
        state = self._live_owner()
        state["owners"]["other"] = 70
        state["owner_ident"]["other"] = "300.000000"
        state["busy"]["other"] = 2.0
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}

        def hires(pid):
            return {50: "100.000000", 70: "300.000000"}.get(int(pid), "")

        painted = []

        def paint(kind, pid, token, held_fd=-1, rule_id=""):
            painted.append((kind, pid, token, held_fd, rule_id))
            return True

        with (
            mock.patch.dict(os.environ, {"HERDR_ENV": "", "HERDR_PANE_ID": "", "HC_TERMINAL_ID": "t7"}, clear=False),
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", hires),
            mock.patch.object(hook, "paint_status", paint),
        ):
            state["aggregate"] = 1
            write, _ok = hook.commit_pane(state, "prompt")
        self.assertEqual(write, "working")
        self.assertEqual(painted, [("working", 50, "100.000000", -1, "")])
        self.assertNotIn(70, [pid for _kind, pid, _token, _fd, _rule in painted])

    def test_weak_exit_does_not_release(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-weak-exit-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")

        def seed(current):
            current["busy"] = {"sess": 1.0}
            current["owners"] = {"sess": 4242}
            current["owner_ident"] = {"sess": "100.000000"}
            current["phase"] = "weak"
            current["generation"] = 2

        hook.update(path, seed)
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: None),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook, "release_authority") as release,
            mock.patch.object(hook, "herdr", lambda *_args: True),
        ):
            stopped = hook.apply_owner_exit("t7", path)
        self.assertTrue(stopped)
        paint.assert_not_called()
        release.assert_not_called()
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["phase"], "weak")
        self.assertEqual(saved["owners"].get("sess"), 4242)

    def test_stale_idle_clear_does_not_paint(self):
        state = self._live_owner()
        state["generation"] = 5
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            write, ok = hook.commit_if_current(state, "idle_clear", 4)
        self.assertEqual((write, ok), ("none", False))
        self.assertEqual(state["phase"], "")
        self.assertIn("sess", state["owners"])
        paint.assert_not_called()

    def test_ps_error_and_eperm_are_not_death(self):
        def denied(_pid, _sig):
            raise OSError(errno.EPERM, "eperm")

        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: {"error": True}),
            mock.patch.object(hook.os, "kill", denied),
        ):
            self.assertTrue(hook.alive(50))
            self.assertIsNotNone(hook.process_snapshot(50))

    def test_send_report_does_not_paint_working_or_done(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-report-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")

        def seed(current):
            current["owners"] = {"sess": 50}
            current["owner_ident"] = {"sess": "100.000000"}

        hook.update(path, seed)
        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": ""}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1") as direct,
        ):
            self.assertTrue(hook.send_report("t7", path, "working"))
            self.assertTrue(hook.send_report("t7", path, "idle"))
        paint.assert_not_called()
        direct.assert_not_called()

    def test_stop_on_a_live_weak_owner_keeps_the_row(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        hook.begin_tick()
        state = hook.apply_event(hook.fresh_state(), "UserPromptSubmit", "sess", 1.0, proc.pid, "")
        state["owner_ident"] = {"sess": "sec:old"}
        state["phase"] = "weak"
        with mock.patch.object(hook, "hires_start", lambda _pid: "sec:old"):
            nxt = hook.apply_event(state, "Stop", "sess", 2.0)
            write, _ok = hook.commit_pane(nxt, "stop")
        self.assertEqual(write, "none")
        self.assertEqual(nxt["owners"].get("sess"), proc.pid)
        self.assertIn("sess", nxt["busy"])
        self.assertEqual(nxt["phase"], "weak")
        self.assertEqual(nxt["grace_until"], 0.0)

    def test_unresolved_tty_leaves_the_proven_row(self):
        state = hook.fresh_state()
        state["owners"] = {"sess": 4242}
        state["owner_ident"] = {"sess": "100.000000"}
        state["phase"] = "proven"
        state["busy"] = {"sess": time.time()}
        hook._close_held_fd()
        with (
            mock.patch.object(hook, "alive", lambda _pid: False),
            mock.patch.object(hook.herdr_kiro_osc, "_tty_opens", lambda _pid: iter(())),
        ):
            write, ok = hook.commit_pane(state, "stop")
        self.assertEqual(write, "done")
        self.assertFalse(ok)
        self.assertEqual(state["phase"], "proven")
        self.assertEqual(state["owners"].get("sess"), 4242)

    def test_settled_stop_does_not_paint_a_newer_generation(self):
        state = hook.fresh_state()
        state["generation"] = 4
        state["phase"] = "proven"
        state["owners"] = {"sess": 50}
        state["owner_ident"] = {"sess": "100.000000"}
        state["busy"] = {"sess": 1.0}
        write, ok = hook.commit_if_current(state, "stop", 3)
        self.assertEqual((write, ok), ("none", False))
        self.assertEqual(state["phase"], "proven")

    def test_gone_proven_outer_still_writes_done_and_releases(self):
        events = ("Stop", "SessionEnd", "UserPromptSubmit")
        for event in events:
            with self.subTest(event=event):
                self._gone_outer_event(event, phase="proven")
                self._gone_outer_event(event, phase="")

    def _gone_outer_event(self, event, phase):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        hook.begin_tick()
        # Where /proc start ticks are absent, hires_start is sec: plus a
        # whole-second lstart. That form is unproven. The exit under test
        # is a fractional token that matched while the pid was alive.
        start = "100.000000"
        self.assertTrue(hook.fine_start(start))
        hook._close_held_fd()
        with pin_owner(proc.pid, start):
            hook._hold_live_owner(proc.pid, start)
            self.assertGreaterEqual(hook._pane_fd, 0)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        hook.begin_tick()
        directory = tempfile.mkdtemp(prefix="herdr-kiro-gone-")
        self.addCleanup(shutil.rmtree, directory, True)
        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "XDG_STATE_HOME": directory,
            "HERDR_KIRO_LOG": os.path.join(directory, "hook.log"),
        }
        calls = []
        payload = json.dumps({"session_id": "sess"})
        with mock.patch.dict(os.environ, env, clear=False):
            path = hook.state_path("t7")

            def seed(state):
                state["busy"] = {"sess": time.time()}
                state["owners"] = {"sess": proc.pid}
                state["owner_ident"] = {"sess": start}
                state["phase"] = phase
                state["seq"] = 4

            hook.update(path, seed)
            with (
                mock.patch.object(sys, "argv", ["herdr-kiro", event]),
                mock.patch.object(sys, "stdin", io.StringIO(payload)),
                mock.patch.object(hook, "herdr", lambda *args: calls.append(tuple(args)) or True),
                mock.patch.object(hook, "ensure_watcher", lambda *_args: None),
                mock.patch.object(hook, "pane_text", lambda *_args: ""),
                mock.patch.object(hook, "kiro_lineage", lambda: (0, "", 0, "")),
            ):
                self.assertEqual(hook.main(), 0)
        done = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self.assertNotIn(done, _read_pty(master))
            self.assertNotIn(("release-agent", "t7"), [args[:2] for args in calls])
            self.assertEqual(saved["owners"].get("sess"), proc.pid)
        else:
            self.assertIn(done, _read_pty(master))
            self.assertIn(("release-agent", "t7"), [args[:2] for args in calls])
            self.assertNotIn("sess", saved.get("owners") or {})
        hook._close_held_fd()

    def test_gone_chat_leaves_the_wait_set(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        directory = tempfile.mkdtemp(prefix="herdr-kiro-chat-wait-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        seen = []

        def wait(_self, pids, _timeout):
            seen.append(list(pids))
            if len(seen) >= 2:
                raise SystemExit("stop")
            return False

        def seed(state):
            state["watcher"] = os.getpid()
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": proc.pid}
            state["owner_ident"] = {"sess": "sec:old"}
            state["phase"] = "weak"
            state["chat_pid"] = 424242
            state["seq"] = 4

        hook.update(path, seed)
        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook.OwnerExitWatch, "wait", wait),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "pane_text", lambda _pane: "› ask a question or describe a task\n"),
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:old"),
            self.assertRaises(SystemExit),
        ):
            hook._watch_loop("t7", path)
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotIn(424242, seen[-1])
        self.assertIn(proc.pid, seen[-1])
        self.assertTrue(hook.alive(proc.pid))

    def test_eperm_is_not_an_immediate_wake(self):
        watch = hook.OwnerExitWatch()
        slept = []

        def kill(_pid, _sig):
            raise OSError(errno.EPERM, "denied")

        def sleep(delay):
            slept.append(delay)

        with (
            mock.patch.object(hook.os, "kill", kill),
            mock.patch.object(hook.time, "sleep", sleep),
            mock.patch.object(hook.time, "time", side_effect=[0.0, 0.0, 1.0]),
        ):
            self.assertFalse(watch._wait_kill([50], 0.2))
        self.assertEqual(slept, [0.05])

        slept.clear()

        def open_fd(_pid):
            raise OSError(errno.EPERM, "denied")

        with (
            mock.patch.object(hook.os, "pidfd_open", open_fd, create=True),
            mock.patch.object(hook.time, "sleep", sleep),
        ):
            self.assertFalse(watch._wait_pidfd([50], 0.2))
        self.assertEqual(slept, [0.2])

    def test_failed_settled_done_retries_with_a_sleep(self):
        # idle_clear plus stop still owes state=done. With no stdout tty the
        # write fails. That is not a finished idle, and the retry sleeps.
        directory = tempfile.mkdtemp(prefix="herdr-kiro-retry-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        calls = []
        slept = []

        def seed(state):
            state["watcher"] = os.getpid()
            state["generation"] = 1
            state["busy"] = {}
            state["owners"] = {}
            state["phase"] = "idle_clear"
            state["grace_until"] = 0.0
            state["proven_pid"] = 4242
            state["proven_ident"] = "100.000000"
            state["seq"] = 4

        hook.update(path, seed)
        hook._close_held_fd()

        def sleep(delay):
            slept.append(delay)

        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook, "EXIT_RETRY_CAP", 2),
            mock.patch.object(hook, "process_snapshot", lambda _pid: None),
            mock.patch.object(hook, "_hold_live_owner", lambda *_args: None),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda *_args: False),
            mock.patch.object(hook.time, "sleep", sleep),
            mock.patch.object(hook, "herdr", lambda *args: calls.append(tuple(args)) or True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "pane_text", lambda _pane: "› ask a question or describe a task\n"),
        ):
            hook._watch_loop("t7", path)
        self.assertEqual(slept, [hook.TICK_SECS])
        self.assertFalse(any(args[0] == "release-agent" for args in calls))
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["phase"], "idle_clear")
        self.assertEqual(saved["watcher"], 0)

    def test_proc_ticks_and_boot_time_are_fractional_seconds(self):
        with (
            mock.patch.object(hook, "_linux_start_ticks", return_value=123456),
            mock.patch.object(hook, "_linux_boot_time", return_value=1700000000),
            mock.patch.object(hook.os, "sysconf", return_value=100),
        ):
            token = hook.hires_start(50)
        self.assertEqual(token, "%.6f" % (1700000000 + (123456 / 100.0)))
        self.assertTrue(hook.fine_start(token))
        self.assertFalse(token.startswith("sec:"))
        source = inspect.getsource(hook.hires_start)
        self.assertNotIn("including on Linux", source)
        self.assertIn("fractional", source)

    def test_pidfile_stores_the_fractional_proc_token(self):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-pidfile-")
        self.addCleanup(shutil.rmtree, directory, True)
        state_file = os.path.join(directory, "pane.json")
        token = "%.6f" % (1700000000 + (123456 / 100.0))
        with (
            mock.patch.object(hook, "_linux_start_ticks", return_value=123456),
            mock.patch.object(hook, "_linux_boot_time", return_value=1700000000),
            mock.patch.object(hook.os, "sysconf", return_value=100),
            mock.patch.object(hook, "process_snapshot", lambda _pid: {"ppid": 1, "lstart": "10", "comm": "python"}),
        ):
            hook.write_pidfile(state_file, 50)
        with open(os.path.join(directory, "pids", "50"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(lines[1], token)
        self.assertTrue(hook.fine_start(lines[1]))
        source = inspect.getsource(hook.write_pidfile)
        self.assertNotIn("including on Linux", source)

    def test_verify_says_a_linux_proc_token_is_a_signal(self):
        path = os.path.join(os.path.dirname(__file__), "..", "VERIFY.md")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("including on Linux", text)
        self.assertIn("fractional `fine_start` token", text)
        self.assertIn("Linux `/proc` token is a signal", text)

    def test_held_fd_refuses_a_fine_token_that_misses_the_live_start(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(slave)
        hook._held_ident = "100.000000"
        hook._held_pid = proc.pid
        payload = b"\x1b]7501;state=done:app=kiro-cli\x1b\\"
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        self.assertFalse(hook._pid_gone(proc.pid))
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "200.000000"),
        ):
            wrote = hook.paint_status("idle", proc.pid, "100.000000", held_fd=hook._pane_fd)
        self.assertFalse(wrote)
        self.assertEqual(_read_pty(master), b"")
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        ):
            wrote = hook.paint_status("idle", proc.pid, "100.000000", held_fd=hook._pane_fd)
        self.assertTrue(wrote)
        self.assertIn(payload, _read_pty(master))

    def test_held_fd_of_another_pid_writes_nothing(self):
        held_master, held_slave = os.openpty()
        owner_master, owner_slave = os.openpty()
        self.addCleanup(os.close, held_master)
        self.addCleanup(os.close, held_slave)
        self.addCleanup(os.close, owner_master)
        self.addCleanup(os.close, owner_slave)
        owner = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=owner_slave,
            stderr=subprocess.DEVNULL,
        )
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        self.addCleanup(lambda: other.poll() is not None or other.kill())
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(held_slave)
        hook._held_ident = "100.000000"
        held_fd = hook._pane_fd
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        owner_name = os.ttyname(owner_slave)
        opened = []
        real_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            text = os.fsdecode(path) if isinstance(path, bytes) else path
            if text in ("/dev/tty", owner_name):
                opened.append(text)
            return real_open(path, flags, *args, **kwargs)

        for name, held_pid in (("other-pid", other.pid), ("unrecorded", 0)):
            with self.subTest(name=name):
                hook._held_pid = held_pid
                state = hook.fresh_state()
                state["busy"] = {"sess": 1.0}
                state["owners"] = {"sess": owner.pid}
                state["owner_ident"] = {"sess": "100.000000"}
                state["phase"] = "proven"
                _read_pty(held_master)
                _read_pty(owner_master)
                opened.clear()
                with (
                    mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
                    mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
                    mock.patch.object(hook.herdr_kiro_osc, "write_pid", side_effect=AssertionError("tty")),
                    mock.patch.object(os, "open", spy_open),
                ):
                    write, ok = hook.commit_pane(state, "stop")
                self.assertEqual((write, ok), ("done", False))
                self.assertEqual(state["phase"], "proven")
                self.assertEqual(state["owners"].get("sess"), owner.pid)
                self.assertEqual(state.get("proven_pid") or 0, 0)
                self.assertEqual(state.get("proven_ident") or "", "")
                self.assertEqual(hook._pane_fd, held_fd)
                self.assertEqual(hook._held_pid, held_pid)
                self.assertEqual(_read_pty(held_master), b"")
                self.assertEqual(_read_pty(owner_master), b"")
                self.assertEqual(opened, [])

    def test_unconfirmed_hold_does_not_open_stdout_for_working(self):
        held_master, held_slave = os.openpty()
        owner_master, owner_slave = os.openpty()
        self.addCleanup(os.close, held_master)
        self.addCleanup(os.close, held_slave)
        self.addCleanup(os.close, owner_master)
        self.addCleanup(os.close, owner_slave)
        owner = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=owner_slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(held_slave)
        hook._held_ident = "100.000000"
        hook._held_pid = 0
        held_fd = hook._pane_fd
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        state = hook.fresh_state()
        state["aggregate"] = 1
        state["busy"] = {hook.AGGREGATE: 1.0}
        state["owners"] = {hook.AGGREGATE: owner.pid}
        state["owner_ident"] = {hook.AGGREGATE: "100.000000"}
        state["proven_pid"] = owner.pid
        state["proven_ident"] = "100.000000"
        state["phase"] = "proven"
        before = _owner_fields(state)
        _read_pty(held_master)
        _read_pty(owner_master)
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", side_effect=AssertionError("live stdout")),
        ):
            write, ok = hook.commit_pane(state, "prompt")
        self.assertEqual((write, ok), ("working", False))
        self.assertEqual(state["phase"], "proven")
        self.assertEqual(_owner_fields(state), before)
        self.assertEqual(hook._pane_fd, held_fd)
        self.assertEqual(_read_pty(held_master), b"")
        self.assertEqual(_read_pty(owner_master), b"")

    def test_unconfirmed_hold_does_not_paint_blocked(self):
        held_master, held_slave = os.openpty()
        owner_master, owner_slave = os.openpty()
        self.addCleanup(os.close, held_master)
        self.addCleanup(os.close, held_slave)
        self.addCleanup(os.close, owner_master)
        self.addCleanup(os.close, owner_slave)
        owner = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=owner_slave,
            stderr=subprocess.DEVNULL,
        )
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: owner.poll() is not None or owner.kill())
        self.addCleanup(lambda: other.poll() is not None or other.kill())
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(held_slave)
        hook._held_ident = "100.000000"
        held_fd = hook._pane_fd
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        calls = []
        real_write = hook.herdr_kiro_osc.write_pid

        def spy(payload, pid, real_write=real_write, calls=calls):
            calls.append((payload, pid))
            return real_write(payload, pid)

        for name, held_pid in (("unrecorded", 0), ("other-pid", other.pid)):
            with self.subTest(name=name):
                hook._held_pid = held_pid
                state = hook.fresh_state()
                state["aggregate"] = 1
                state["busy"] = {hook.AGGREGATE: 1.0}
                state["owners"] = {hook.AGGREGATE: owner.pid}
                state["owner_ident"] = {hook.AGGREGATE: "100.000000"}
                state["proven_pid"] = owner.pid
                state["proven_ident"] = "100.000000"
                state["phase"] = "proven"
                before = copy.deepcopy(state)
                calls.clear()
                _read_pty(held_master)
                _read_pty(owner_master)
                with (
                    mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
                    mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
                    mock.patch.object(hook.herdr_kiro_osc, "write_pid", spy),
                ):
                    write, ok = hook.commit_pane(state, "blocked")
                self.assertEqual((write, ok), ("paint_blocked", False))
                self.assertEqual(state, before)
                self.assertEqual(calls, [])
                self.assertEqual(hook._pane_fd, held_fd)
                self.assertEqual(hook._held_pid, held_pid)
                self.assertEqual(_read_pty(held_master), b"")
                self.assertEqual(_read_pty(owner_master), b"")

    def test_reap_unlinks_only_a_confirmed_dead_or_mismatched_pidfile(self):
        denied = hook.PidFileView(1, errno.EPERM, "1.000000", "2.000000", False)
        dead = hook.PidFileView(1, errno.ESRCH, "", "", False)
        unknown = hook.PidFileView(1, None, "", "1.000000", False)
        snapped = hook.PidFileView(1, None, "1.000000", "2.000000", True)
        matched_comm = hook.PidFileView(1, None, "1.000000", "1.000000", False)
        self.assertEqual(hook.classify_pidfile(denied), "keep")
        self.assertEqual(hook.classify_pidfile(dead), "reap")
        self.assertEqual(hook.classify_pidfile(unknown), "keep")
        self.assertEqual(hook.classify_pidfile(snapped), "keep")
        self.assertEqual(hook.classify_pidfile(matched_comm), "keep")
        root = tempfile.mkdtemp(prefix="herdr-kiro-reap-")
        self.addCleanup(shutil.rmtree, root, True)
        pid_dir = os.path.join(root, "herdr-kiro", "pids")
        os.makedirs(pid_dir)
        dead_pid, bad_pid, ok_pid, renamed_pid = 424242, 434343, 444444, 454545
        files = {
            str(dead_pid): f"{os.getuid()}\nnot-a-start\nstale\n",
            str(bad_pid): f"{os.getuid()}\n1700000000.999999\nnot-the-comm\n",
            str(ok_pid): f"{os.getuid()}\n100.000000\nkiro-cli\n",
            str(renamed_pid): f"{os.getuid()}\n100.000000\nold-comm\n",
        }
        for name, text in files.items():
            with open(os.path.join(pid_dir, name), "w", encoding="utf-8") as handle:
                handle.write(text)

        def kill(pid, _sig, real_kill=os.kill):
            if pid == dead_pid:
                raise OSError(errno.ESRCH, "gone")
            if pid in (bad_pid, ok_pid, renamed_pid):
                return None
            return real_kill(pid, _sig)

        def snap(pid):
            if pid == ok_pid:
                return {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
            if pid == renamed_pid:
                return {"ppid": 1, "lstart": "10", "comm": "renamed"}
            if pid == bad_pid:
                return {"ppid": 1, "lstart": "10", "comm": "other"}
            return None

        def start(pid):
            return "100.000000" if pid in (ok_pid, renamed_pid) else "200.000000"

        env = {"XDG_STATE_HOME": root}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook.os, "kill", kill),
            mock.patch.object(hook, "process_snapshot", snap),
            mock.patch.object(hook, "hires_start", start),
        ):
            hook.commit_pane(hook.fresh_state(), "prompt")
            for name in files:
                self.assertTrue(os.path.isfile(os.path.join(pid_dir, name)), name)
            hook.commit_pane(hook.fresh_state(), "reap")
            self.assertFalse(os.path.isfile(os.path.join(pid_dir, str(dead_pid))))
            self.assertFalse(os.path.isfile(os.path.join(pid_dir, str(bad_pid))))
            self.assertTrue(os.path.isfile(os.path.join(pid_dir, str(ok_pid))))
            self.assertTrue(os.path.isfile(os.path.join(pid_dir, str(renamed_pid))))

    def test_live_sec_idle_clear_is_weak_and_reaches_pid_reuse(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        self.assertFalse(hook._pid_gone(proc.pid))
        for token in ("sec:10", ""):
            with self.subTest(token=token):
                state = self._live_owner()
                state["owners"] = {"sess": proc.pid}
                state["phase"] = "idle_clear"
                if token:
                    state["owner_ident"] = {"sess": token}
                else:
                    state["owner_ident"] = {}
                with (
                    mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
                    mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
                    mock.patch.object(hook, "paint_status", return_value=True) as paint,
                ):
                    self.assertEqual(hook.owner_phase(state), "weak")
                    for event in ("stop", "idle_clear", "piped_chat", "process_exit", "pid_reuse"):
                        phase = hook.owner_phase(state)
                        self.assertEqual(hook.pane_transition(phase, event), "none", event)
                    write, _ok = hook.commit_pane(state, "pid_reuse")
                self.assertEqual((write, state["phase"]), ("none", "idle_clear"))
                self.assertEqual(hook.owner_phase(state), "weak")
                self.assertEqual(state["owners"].get("sess"), proc.pid)
                paint.assert_not_called()
                self.assertEqual(hook.pane_next("idle_clear", "pid_reuse"), "weak")

    def test_live_weak_idle_clear_does_not_paint_the_held_fd(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        hook._pane_fd = os.dup(slave)
        hook._held_ident = "100.000000"
        hook._held_pid = proc.pid
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        self.assertFalse(hook._pid_gone(proc.pid))
        for token in ("sec:10", ""):
            with self.subTest(token=token):
                state = self._live_owner()
                state["owners"] = {"sess": proc.pid}
                state["phase"] = "idle_clear"
                state["owner_ident"] = {"sess": token} if token else {}
                with (
                    mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
                    mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
                    mock.patch.object(hook, "paint_status", return_value=True) as paint,
                    mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
                ):
                    for event in ("stop", "idle_clear", "piped_chat", "process_exit"):
                        sample = hook.fresh_state()
                        sample.update(state)
                        sample["owner_ident"] = dict(state["owner_ident"])
                        sample["owners"] = dict(state["owners"])
                        sample["busy"] = dict(state["busy"])
                        write, _ok = hook.commit_pane(sample, event)
                        self.assertEqual(write, "none", event)
                paint.assert_not_called()
                held.assert_not_called()
        self.assertEqual(_read_pty(master), b"")

    def test_sec_owner_process_exit_does_not_release(self):
        for token in ("sec:10", ""):
            with self.subTest(token=token):
                directory = tempfile.mkdtemp(prefix="herdr-kiro-sec-exit-")
                self.addCleanup(shutil.rmtree, directory, True)
                path = os.path.join(directory, "pane.json")

                def seed(current, token=token):
                    current["busy"] = {"sess": 1.0}
                    current["owners"] = {"sess": 4242}
                    current["owner_ident"] = {"sess": token} if token else {}
                    current["phase"] = "idle_clear"
                    current["generation"] = 2

                hook.update(path, seed)
                with (
                    mock.patch.object(hook, "process_snapshot", lambda _pid: None),
                    mock.patch.object(hook, "paint_status", return_value=True) as paint,
                    mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
                    mock.patch.object(hook, "release_authority") as release,
                    mock.patch.object(hook, "herdr", lambda *_args: True),
                ):
                    stopped = hook.apply_owner_exit("t7", path)
                self.assertTrue(stopped)
                paint.assert_not_called()
                held.assert_not_called()
                release.assert_not_called()
                with open(path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.assertEqual(saved["owners"].get("sess"), 4242)
                self.assertEqual(saved["phase"], "idle_clear")

    def test_stop_does_not_drop_a_dead_sec_row(self):
        snap_gone = None
        for event in ("Stop", "SessionEnd"):
            with self.subTest(event=event):
                state = hook.fresh_state()
                state["busy"] = {"sess": 1.0}
                state["owners"] = {"sess": 424242}
                state["owner_ident"] = {"sess": "sec:10"}
                state["phase"] = "idle_clear"
                state["generation"] = 3
                with (
                    mock.patch.object(hook, "process_snapshot", lambda _pid: snap_gone),
                    mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
                    mock.patch.object(hook, "paint_status", return_value=True) as paint,
                ):
                    self.assertEqual(hook.owner_phase(state), "weak")
                    self.assertEqual(hook.pane_transition(hook.owner_phase(state), "stop"), "none")
                    revised = hook.apply_event(state, event, "sess", 2.0, 424242, "sec:10")
                self.assertEqual(revised["owners"].get("sess"), 424242)
                self.assertIn("sess", revised["busy"])
                paint.assert_not_called()

    def test_watcher_does_not_write_phase_outside_the_chooser(self):
        source = inspect.getsource(hook._run_watch)
        self.assertNotIn('["phase"] =', source)
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        directory = tempfile.mkdtemp(prefix="herdr-kiro-phase-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        events = []
        real_commit = hook.commit_pane

        def spy(state, event, rule_id="", **kwargs):
            if not kwargs.get("watch"):
                events.append(event)
            return real_commit(state, event, rule_id, **kwargs)

        def wait(_self, _pids, _timeout):
            if events:
                raise SystemExit("stop")
            return False

        def seed(state):
            state["watcher"] = os.getpid()
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": proc.pid}
            state["owner_ident"] = {"sess": "sec:old"}
            state["phase"] = "idle_clear"
            state["generation"] = 2

        hook.update(path, seed)
        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": "", "HERDR_KIRO_LOG": path + ".log"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook, "commit_pane", spy),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook.OwnerExitWatch, "wait", wait),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "pane_text", lambda _pane: "not the prompt\n"),
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:old"),
            self.assertRaises(SystemExit),
        ):
            hook._watch_loop("t7", path)
        self.assertIn("pid_reuse", events)
        paint.assert_not_called()
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["phase"], "idle_clear")
        self.assertEqual(hook.owner_phase(saved), "weak")
        self.assertEqual(saved["owners"].get("sess"), proc.pid)

    def test_empty_app_screen_does_not_end_the_turn(self):
        self.assertFalse(hook.prompt_visible(""))
        self.assertFalse(hook.screen_is_quiet(""))
        self.assertFalse(hook.screen_is_quiet("\n"))
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        directory = tempfile.mkdtemp(prefix="herdr-kiro-empty-app-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        reports = []
        reads = {"n": 0}

        def hc_screen():
            reads["n"] += 1
            if reads["n"] > 2:
                raise SystemExit("stop")
            return ""

        def seed(state):
            state["watcher"] = os.getpid()
            state["busy"] = {}
            state["owners"] = {"sess": proc.pid}
            state["owner_ident"] = {"sess": "100.000000"}
            state["phase"] = "proven"
            state["grace_until"] = 0.0
            state["generation"] = 1

        hook.update(path, seed)
        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": "", "HERDR_KIRO_LOG": path + ".log"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook, "_hc_screen", hc_screen),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook, "send_report", lambda *args, **_kwargs: reports.append(args[2]) or True),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}),
            mock.patch.object(hook.OwnerExitWatch, "wait", lambda *_args: False),
            self.assertRaises(SystemExit),
        ):
            hook._watch_loop("t7", path)
        self.assertNotIn("idle", reports)
        paint.assert_not_called()
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["phase"], "proven")

    def test_one_owner_record_exits_only_when_that_process_is_gone(self):
        first = subprocess.Popen(["sleep", "30"])
        second = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: first.poll() is not None or first.kill())
        self.addCleanup(lambda: second.poll() is not None or second.kill())
        state = hook.fresh_state()
        state["owners"] = {"sess": first.pid, "later": second.pid}
        state["owner_ident"] = {"sess": "100.000000", "later": "200.000000"}
        self.assertEqual([pid for pid, _token in hook._outer_pairs(state)], [first.pid])
        self.assertFalse(hook._owner_exited(state, [first.pid, second.pid]))
        first.kill()
        first.wait(timeout=5)
        pairs = hook._outer_pairs(state)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], second.pid)
        self.assertFalse(hook._owner_exited(state, [first.pid, second.pid]))
        empty = hook.fresh_state()
        self.assertFalse(hook._owner_exited(empty, [first.pid, second.pid]))
        second.kill()
        second.wait(timeout=5)
        self.assertTrue(hook._owner_exited(state, [first.pid, second.pid]))
        self.assertTrue(hook._owner_exited(empty, [first.pid, second.pid]))

    def test_ready_exit_sleeps_and_stops_at_the_cap(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        os.kill(proc.pid, signal.SIGKILL)
        watch = hook.OwnerExitWatch()
        self.addCleanup(watch.close)
        delays = []

        def sleep(delay):
            delays.append(delay)

        try:
            with mock.patch.object(hook.time, "sleep", sleep):
                for _ in range(12):
                    self.assertTrue(watch.wait([proc.pid], 0.2))
        finally:
            proc.wait(timeout=5)
        self.assertGreaterEqual(len(delays), 12)
        self.assertTrue(all(0 < delay <= hook.POLL_BACKOFF_CAP for delay in delays))
        self.assertEqual(delays[-1], hook.POLL_BACKOFF_CAP)
        self.assertNotIn(proc.pid, watch._fds)

        kqueue_delays = []

        class Queue:
            def control(self, _changes, _count, _timeout):
                return [object()]

            def close(self):
                return None

        def kevent(*args):
            return args

        kqueue_watch = hook.OwnerExitWatch()
        self.addCleanup(kqueue_watch.close)
        with (
            mock.patch.object(hook.select, "kqueue", Queue, create=True),
            mock.patch.object(hook.select, "KQ_FILTER_PROC", 20, create=True),
            mock.patch.object(hook.select, "KQ_NOTE_EXIT", 1, create=True),
            mock.patch.object(hook.select, "KQ_EV_ADD", 2, create=True),
            mock.patch.object(hook.select, "KQ_EV_ENABLE", 4, create=True),
            mock.patch.object(hook.select, "kevent", kevent, create=True),
            mock.patch.object(hook.time, "sleep", kqueue_delays.append),
        ):
            self.assertTrue(kqueue_watch.wait([4321], 1))
            self.assertTrue(kqueue_watch.wait([4321], 1))
        self.assertEqual(kqueue_delays[0], hook.TICK_SECS)
        self.assertEqual(kqueue_delays[1], min(hook.TICK_SECS * 2, hook.POLL_BACKOFF_CAP))
        self.assertLessEqual(max(kqueue_delays), hook.POLL_BACKOFF_CAP)

        lookups = {"n": 0}
        loop_delays = []
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: holder.poll() is not None or holder.kill())
        os.kill(holder.pid, signal.SIGKILL)
        directory = tempfile.mkdtemp(prefix="herdr-kiro-spin-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")

        def seed(state):
            state["watcher"] = os.getpid()
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": holder.pid}
            state["owner_ident"] = {"sess": "sec:old"}
            state["phase"] = "weak"

        hook.update(path, seed)

        def loop_sleep(delay):
            loop_delays.append(delay)
            if len(loop_delays) >= 4:
                raise SystemExit("bounded")

        def lookup(_pane):
            lookups["n"] += 1
            if lookups["n"] > 8:
                raise AssertionError("busy loop")
            return "working"

        env = {"HC_TERMINAL_ID": "t7", "HERDR_ENV": "", "HERDR_PANE_ID": "", "HERDR_KIRO_LOG": path + ".log"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook.time, "sleep", loop_sleep),
            mock.patch.object(hook, "agent_lookup", lookup),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "pane_text", lambda _pane: "not the prompt\n"),
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:old"),
            self.assertRaises(SystemExit),
        ):
            hook._watch_loop("t7", path)
        self.assertGreaterEqual(len(loop_delays), 4)
        self.assertLessEqual(lookups["n"], 8)
        self.assertTrue(all(delay <= hook.POLL_BACKOFF_CAP for delay in loop_delays))
        holder.wait(timeout=5)


def _owner_fields(state):
    """The owner row a none result must leave unchanged."""
    proven = state.get("proven_pid") or 0
    ident = state.get("proven_ident") or ""
    return (
        dict(state.get("owners") or {}),
        dict(state.get("owner_ident") or {}),
        dict(state.get("busy") or {}),
        proven,
        ident,
    )


def _copy_pane(state):
    copied = dict(state)
    for key in ("owners", "owner_ident", "busy"):
        raw = state.get(key)
        if isinstance(raw, dict):
            copied[key] = dict(raw)
    return copied


# One row per lifecycle or security finding from the fb5a842 reviews.
# The five required rows are the unproven pid, the held fd whose pid is
# not the owner, the gone chat child, the empty owner list, and the
# bounded wait.
PRIOR_FINDING_ROWS = (
    {"name": "weak-or-unproven-pid", "case": "unproven_pid", "token": "sec:10"},
    {"name": "empty-token-pid", "case": "unproven_pid", "token": ""},
    {"name": "fine-token-mismatch-is-not-painted", "case": "fine_mismatch"},
    {"name": "held-fd-pid-is-not-the-owner", "case": "held_other_pid"},
    {"name": "held-proof-requires-the-owner-pid", "case": "held_proof"},
    {"name": "chat-child-gone-outer-alive", "case": "chat_gone"},
    {"name": "one-owner-record", "case": "one_owner"},
    {"name": "empty-owner-list", "case": "empty_owners"},
    {"name": "bounded-wait", "case": "bounded_wait"},
    {"name": "weak-exit-then-stop-leaves-the-row", "case": "none_keeps_row"},
    {"name": "done-drop-is-not-a-later-none", "case": "done_then_none"},
    {"name": "missing-or-ambiguous-tty-writes-nothing", "case": "tty_closed"},
    {"name": "dev-tty-is-a-whole-path", "case": "tty_path"},
    {"name": "empty-screen-does-not-end", "case": "empty_screen"},
    {"name": "question-panel-does-not-settle", "case": "question_panel"},
    {"name": "no-owner-chevron-does-not-end", "case": "no_owner_chevron"},
    {"name": "no-owner-slot-still-ends-on-stop", "case": "no_owner_stop"},
    {"name": "watcher-does-not-assign-phase", "case": "watcher_phase"},
)


class PriorFindingTableTests(unittest.TestCase):
    """Table of the lifecycle and security findings. Each row has to pass."""

    def test_prior_findings_stay_closed(self):
        for row in PRIOR_FINDING_ROWS:
            with self.subTest(finding=row["name"]):
                getattr(self, "_row_" + row["case"])(row)

    def _sleep_proc(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda proc=proc: proc.poll() is not None or proc.kill())
        return proc

    def _row_unproven_pid(self, row):
        proc = self._sleep_proc()
        token = row["token"]
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": proc.pid}
        state["owner_ident"] = {"sess": token} if token else {}
        state["phase"] = "idle_clear"
        state["generation"] = 1
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(slave)
        hook._held_ident = "100.000000"
        hook._held_pid = proc.pid
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        events = ("stop", "idle_clear", "piped_chat", "process_exit", "pid_reuse")
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
        ):
            self.assertEqual(hook.owner_phase(state), "weak")
            self.assertFalse(hook._held_proves(token, proc.pid))
            for event in events:
                sample = _copy_pane(state)
                before = _owner_fields(sample)
                write, _ok = hook.commit_pane(sample, event)
                self.assertEqual(write, "none", event)
                self.assertEqual(_owner_fields(sample), before, event)
            write, ok = hook.commit_pane(state, "idle_clear")
            self.assertEqual((write, ok), ("none", True))
        self.assertEqual(state["owners"].get("sess"), proc.pid)
        self.assertIn("sess", state["busy"])
        self.assertEqual(state["phase"], "idle_clear")
        paint.assert_not_called()
        held.assert_not_called()
        self.assertEqual(_read_pty(master), b"")
        hook._close_held_fd()

    def _row_fine_mismatch(self, _row):
        proc = self._sleep_proc()
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": proc.pid}
        state["owner_ident"] = {"sess": "200.000000"}
        state["phase"] = "proven"
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
        ):
            self.assertEqual(hook.owner_phase(state), "weak")
            before = _owner_fields(state)
            write, _ok = hook.commit_pane(state, "stop")
            self.assertEqual(write, "none")
            self.assertEqual(_owner_fields(state), before)
            self.assertEqual(hook.owner_phase(state), "weak")
            self.assertEqual(state["phase"], "proven")
            write, _ok = hook.commit_pane(state, "pid_reuse")
            self.assertEqual(write, "none")
            self.assertEqual(_owner_fields(state), before)
            self.assertEqual(state["phase"], "proven")
            self.assertEqual(state["owners"].get("sess"), proc.pid)
        paint.assert_not_called()
        held.assert_not_called()

    def _row_held_other_pid(self, _row):
        held_master, held_slave = os.openpty()
        owner_master, owner_slave = os.openpty()
        self.addCleanup(os.close, held_master)
        self.addCleanup(os.close, held_slave)
        self.addCleanup(os.close, owner_master)
        self.addCleanup(os.close, owner_slave)
        owner = subprocess.Popen(
            ["sleep", "30"],
            stdin=subprocess.DEVNULL,
            stdout=owner_slave,
            stderr=subprocess.DEVNULL,
        )
        other = self._sleep_proc()
        self.addCleanup(lambda owner=owner: owner.poll() is not None or owner.kill())
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(held_slave)
        hook._held_ident = "100.000000"
        held_fd = hook._pane_fd
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        owner_name = os.ttyname(owner_slave)
        opened = []
        real_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            text = os.fsdecode(path) if isinstance(path, bytes) else path
            if text in ("/dev/tty", owner_name):
                opened.append(text)
            return real_open(path, flags, *args, **kwargs)

        for held_pid in (other.pid, 0):
            hook._held_pid = held_pid
            state = hook.fresh_state()
            state["busy"] = {"sess": 1.0}
            state["owners"] = {"sess": owner.pid}
            state["owner_ident"] = {"sess": "100.000000"}
            state["phase"] = "proven"
            _read_pty(held_master)
            _read_pty(owner_master)
            opened.clear()
            with (
                mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
                mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
                mock.patch.object(hook.herdr_kiro_osc, "write_pid", side_effect=AssertionError("tty")),
                mock.patch.object(os, "open", spy_open),
            ):
                self.assertFalse(hook._held_proves("100.000000", owner.pid))
                write, ok = hook.commit_pane(state, "stop")
            self.assertEqual((write, ok), ("done", False))
            self.assertEqual(state["phase"], "proven")
            self.assertEqual(state["owners"].get("sess"), owner.pid)
            self.assertEqual(state["owner_ident"].get("sess"), "100.000000")
            self.assertEqual(state.get("proven_pid") or 0, 0)
            self.assertEqual(state.get("proven_ident") or "", "")
            self.assertIn("sess", state["busy"])
            self.assertEqual(hook._pane_fd, held_fd)
            self.assertEqual(_read_pty(held_master), b"")
            self.assertEqual(_read_pty(owner_master), b"")
            self.assertEqual(opened, [])
        hook._close_held_fd()

    def _row_held_proof(self, _row):
        _master, slave = os.openpty()
        self.addCleanup(os.close, _master)
        self.addCleanup(os.close, slave)
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        hook._pane_fd = os.dup(slave)
        hook._held_ident = "100.000000"
        hook._held_pid = 70
        start = "100.000000"
        self.assertFalse(hook._held_proves(start, 60))
        self.assertFalse(hook._held_proves(start, 0))
        self.assertTrue(hook._held_proves(start, 70))
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": 60}
        state["owner_ident"] = {"sess": start}
        state["phase"] = "exited"
        state["proven_pid"] = 60
        state["proven_ident"] = start
        before = _owner_fields(state)

        def gone(_pid, _sig=0):
            raise OSError(errno.ESRCH, "gone")

        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: None),
            mock.patch.object(hook.os, "kill", gone),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            self.assertFalse(hook._chooser_drops_row(state, "Stop"))
            self.assertFalse(hook._chooser_drops_row(state, "SessionEnd"))
            revised = hook.apply_event(state, "Stop", "sess", 2.0)
        self.assertEqual(_owner_fields(revised), before)
        self.assertIn("sess", revised["busy"])
        paint.assert_not_called()
        hook._close_held_fd()

    def _row_chat_gone(self, _row):
        outer = self._sleep_proc()
        chat = self._sleep_proc()
        chat.kill()
        chat.wait(timeout=5)
        state = hook.fresh_state()
        state["owners"] = {"sess": outer.pid}
        state["owner_ident"] = {"sess": "100.000000"}
        state["chat_pid"] = chat.pid
        pairs = hook._outer_pairs(state)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], outer.pid)
        self.assertTrue(hook._pid_gone(chat.pid))
        self.assertFalse(hook._pid_gone(outer.pid))
        self.assertFalse(hook._owner_exited(state, [outer.pid, chat.pid]))

    def _row_one_owner(self, _row):
        first = self._sleep_proc()
        second = self._sleep_proc()
        state = hook.fresh_state()
        state["owners"] = {"sess": first.pid, "later": second.pid}
        state["owner_ident"] = {"sess": "100.000000", "later": "200.000000"}
        self.assertEqual([pid for pid, _token in hook._outer_pairs(state)], [first.pid])
        self.assertFalse(hook._owner_exited(state, [first.pid, second.pid]))
        first.kill()
        first.wait(timeout=5)
        pairs = hook._outer_pairs(state)
        self.assertEqual([pid for pid, _token in pairs], [second.pid])
        self.assertFalse(hook._owner_exited(state, [first.pid, second.pid]))

    def _row_empty_owners(self, _row):
        proc = self._sleep_proc()
        directory = tempfile.mkdtemp(prefix="herdr-kiro-empty-owners-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t7.json")
        seen = []

        def wait(_self, pids, _timeout):
            seen.append(list(pids))
            if len(seen) == 2:

                def clear(state):
                    state["owners"] = {}

                hook.update(path, clear)
            if len(seen) >= 3:
                raise SystemExit("stop")
            return False

        def seed(state):
            state["watcher"] = os.getpid()
            state["busy"] = {"sess": time.time()}
            state["owners"] = {"sess": proc.pid}
            state["owner_ident"] = {"sess": "sec:old"}
            state["phase"] = "weak"
            state["seq"] = 4

        hook.update(path, seed)
        env = {
            "HC_TERMINAL_ID": "t7",
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HERDR_KIRO_LOG": path + ".log",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(hook.OwnerExitWatch, "wait", wait),
            mock.patch.object(hook, "herdr", lambda *_args: True),
            mock.patch.object(hook, "agent_lookup", lambda _pane: "working"),
            mock.patch.object(hook, "pane_text", lambda _pane: ""),
            mock.patch.object(hook, "screen_view", lambda _text: hook._unknown_screen()),
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:old"),
            self.assertRaises(SystemExit),
        ):
            hook._watch_loop("t7", path)
        self.assertEqual(seen[0], [])
        self.assertEqual(seen[1], [proc.pid])
        self.assertEqual(seen[2], [])
        self.assertTrue(hook.alive(proc.pid))

    def _row_bounded_wait(self, _row):
        watch = hook.OwnerExitWatch()
        self.addCleanup(watch.close)
        slept = []

        def kill(_pid, _sig):
            raise OSError(errno.ESRCH, "gone")

        def sleep(delay):
            slept.append(delay)

        calls = hook.POLL_SPIN_CAP + 2
        with (
            mock.patch.object(hook.os, "kill", kill),
            mock.patch.object(hook.time, "sleep", sleep),
        ):
            for _ in range(calls):
                self.assertTrue(watch._wait_kill([4242], 2.0))
        cap = min(hook.TICK_SECS * hook.POLL_SPIN_CAP, hook.POLL_BACKOFF_CAP)
        self.assertEqual(len(slept), calls)
        self.assertEqual(slept[0], hook.TICK_SECS)
        self.assertEqual(slept[-1], cap)
        self.assertTrue(all(0 < delay <= hook.POLL_BACKOFF_CAP for delay in slept))
        self.assertEqual(watch._spins, hook.POLL_SPIN_CAP)
        self.assertTrue(watch.early)

    def _row_none_keeps_row(self, _row):
        def gone(_pid, _sig=0):
            raise OSError(errno.ESRCH, "gone")

        for token, phase in (("100.000000", "weak"), ("sec:10", "idle_clear"), ("", "idle_clear")):
            state = hook.fresh_state()
            state["busy"] = {"sess": 1.0}
            state["owners"] = {"sess": 4242}
            state["owner_ident"] = {"sess": token} if token else {}
            state["phase"] = phase
            state["proven_pid"] = 0
            state["proven_ident"] = ""
            before = _owner_fields(state)
            with (
                mock.patch.object(hook, "process_snapshot", lambda _pid: None),
                mock.patch.object(hook.os, "kill", gone),
                mock.patch.object(hook, "hires_start", lambda _pid, token=token: token),
                mock.patch.object(hook, "paint_status", return_value=True) as paint,
                mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
                mock.patch.object(hook, "release_authority") as release,
            ):
                write, _ok = hook.commit_pane(state, "process_exit")
                self.assertEqual(write, "none", token)
                self.assertEqual(_owner_fields(state), before, token)
                self.assertEqual(state["phase"], phase, token)
                self.assertFalse(hook._chooser_drops_row(state, "Stop"), token)
                revised = hook.apply_event(state, "Stop", "sess", 2.0)
            self.assertEqual(revised["owners"].get("sess"), 4242, token)
            self.assertIn("sess", revised["busy"], token)
            paint.assert_not_called()
            held.assert_not_called()
            release.assert_not_called()

    def _row_done_then_none(self, _row):
        def gone(_pid, _sig=0):
            raise OSError(errno.ESRCH, "gone")

        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": 4242}
        state["owner_ident"] = {"sess": "100.000000"}
        state["phase"] = "proven"
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: None),
            mock.patch.object(hook.os, "kill", gone),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
        ):
            write, ok = hook.commit_pane(state, "stop")
            self.assertEqual((write, ok), ("done", True))
            self.assertEqual(state["phase"], "exited")
            self.assertNotIn("sess", state["owners"])
            self.assertNotIn("sess", state["busy"])
            self.assertEqual(state["proven_pid"], 4242)
            again = hook.apply_event(state, "Stop", "sess", 2.0)
            self.assertEqual(hook._PANE_OUTCOME["write"], "none")
            self.assertIs(again, state)
            self.assertEqual(state["proven_pid"], 4242)
        paint.assert_called()
        held.assert_not_called()

    def _row_tty_closed(self, _row):
        osc = hook.herdr_kiro_osc
        self.assertEqual(osc._one_stdout(""), "")
        self.assertEqual(osc._one_stdout("p1\n"), "")
        self.assertEqual(osc._one_stdout("f1\nn/dev/pts/0\nf1\nn/dev/pts/1\n"), "")
        self.assertEqual(osc._one_stdout("f1\nn/dev/tty\n"), "")

    def _row_tty_path(self, _row):
        osc = hook.herdr_kiro_osc
        self.assertEqual(osc._one_stdout("f1\nn/dev/pts/0\n"), "/dev/pts/0")
        self.assertEqual(osc._one_stdout("f1\nn/dev/ttys000\n"), "/dev/ttys000")
        self.assertTrue(osc._TTY.fullmatch("/dev/ttys000"))
        self.assertIsNone(osc._TTY.fullmatch("/dev/tty"))

    def _row_empty_screen(self, _row):
        self.assertFalse(hook.prompt_visible(""))
        self.assertFalse(hook.screen_is_quiet(""))
        self.assertFalse(hook.screen_is_quiet("\n"))

    def _row_question_panel(self, _row):
        text = "› ask a question or describe a task\n"
        view = {"state": "blocked", "id": "question_panel", "ok": True}
        snap = hook.ScreenSnapshot(text, view)
        self.assertTrue(hook.blocker_visible(snap))
        self.assertFalse(hook.screen_is_settled(snap))
        self.assertFalse(hook.screen_is_quiet(snap))
        failed = hook.ScreenSnapshot(None, view)
        self.assertIsNone(hook.report_for(failed, "working"))
        self.assertFalse(hook.screen_is_settled(failed))
        footer = "esc to close, arrows to navigate, and enter to select\n"
        self.assertTrue(hook.approval_text(footer))
        self.assertFalse(hook.screen_is_settled(footer))

    def _row_no_owner_chevron(self, _row):
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        state["generation"] = 1
        with mock.patch.object(hook, "paint_status", return_value=True) as paint:
            write, _ok = hook.commit_pane(state, hook.screen_event(state, "timer"))
            self.assertEqual(write, "none")
        self.assertIn("sess", state["busy"])
        self.assertEqual(state["owners"], {})
        self.assertNotEqual(state["phase"], "idle_clear")
        paint.assert_not_called()

    def _row_no_owner_stop(self, _row):
        state = hook.fresh_state()
        state["busy"] = {"sess": 1.0}
        self.assertEqual(hook.pane_transition("no_owner", "stop"), "done")
        revised = hook.apply_event(state, "Stop", "sess", 2.0)
        self.assertNotIn("sess", revised["busy"])
        self.assertEqual(revised["owners"], {})

    def _row_watcher_phase(self, _row):
        source = inspect.getsource(hook._run_watch)
        self.assertNotIn('["phase"] =', source)
        self.assertNotIn("['phase'] =", source)


SETTLED_ROWS = (
    {"name": "weak-owner-at-zero", "case": "weak_zero"},
    {"name": "proven-owner-at-zero", "case": "proven_zero"},
    {"name": "none-never-mutates", "case": "none_pure"},
    {"name": "proven-blocked", "case": "proven_blocked"},
    {"name": "weak-blocked", "case": "weak_blocked"},
    {"name": "no-owner-blocked", "case": "no_owner_blocked"},
    {"name": "watcher-blocked", "case": "watcher_blocked"},
    {"name": "blocked-exit-prompt", "case": "exit_prompt"},
    {"name": "blocked-exit-process", "case": "exit_process"},
    {"name": "blocked-exit-idle-clear", "case": "exit_idle"},
    {"name": "emit-osc-only-from-commit-pane", "case": "emit_only"},
)


class SettledDecisionTests(unittest.TestCase):
    def test_settled_decisions(self):
        paints = set(hook._PANE_WRITES.values())
        self.assertEqual(
            paints,
            {
                "working",
                "done",
                "paint_blocked",
                "refresh",
                "reap",
                "record_aggregate",
                "touch_aggregate",
                "touch_partial",
                "record_named",
            },
        )
        for row in SETTLED_ROWS:
            with self.subTest(row["name"]):
                getattr(self, "_row_" + row["case"])(row)

    def _live(self):
        snap = {"ppid": 1, "lstart": "10", "comm": "kiro-cli"}
        return (
            mock.patch.object(hook, "process_snapshot", lambda _pid, snap=snap: snap),
            mock.patch.object(hook, "hires_start", lambda _pid: "100.000000"),
        )

    def _pane_fields(self, state):
        return {
            "phase": state.get("phase"),
            "busy": dict(state.get("busy") or {}),
            "owners": dict(state.get("owners") or {}),
            "owner_ident": dict(state.get("owner_ident") or {}),
            "aggregate": state.get("aggregate"),
            "proven_pid": state.get("proven_pid"),
            "proven_ident": state.get("proven_ident"),
        }

    def _aggregate(self, phase, token, stamp=10.0):
        state = hook.fresh_state()
        state["phase"] = phase
        state["aggregate"] = 0
        state["busy"] = {hook.AGGREGATE: stamp}
        state["owners"] = {hook.AGGREGATE: 50}
        state["owner_ident"] = {hook.AGGREGATE: token}
        state["seq"] = 4
        return state

    def _row_weak_zero(self, _row):
        state = self._aggregate("weak", "sec:10")
        before = self._pane_fields(state)
        with self._live()[0], mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"):
            self.assertEqual(hook.owner_phase(state), "weak")
            self.assertEqual(hook.pane_transition("weak", "aggregate_zero"), "none")
            revised = hook.apply_event(state, "Stop", hook.AGGREGATE, 25.0)
        self.assertEqual(self._pane_fields(revised), before)

    def _row_proven_zero(self, _row):
        state = self._aggregate("proven", "100.000000")
        before = self._pane_fields(state)
        with self._live()[0], self._live()[1]:
            self.assertEqual(hook.owner_phase(state), "proven")
            self.assertEqual(hook.pane_transition("proven", "aggregate_zero"), "refresh")
            revised = hook.apply_event(state, "Stop", hook.AGGREGATE, 25.0)
        self.assertEqual(revised["busy"][hook.AGGREGATE], 25.0)
        changed = self._pane_fields(revised)
        changed["busy"] = dict(before["busy"])
        self.assertEqual(changed, before)

    def _row_none_pure(self, _row):
        def gone(_pid, _sig=0):
            raise OSError(errno.ESRCH, "gone")

        weak = hook.fresh_state()
        weak["phase"] = "weak"
        weak["busy"] = {"sess": 1.0}
        weak["owners"] = {"sess": 4242}
        weak["owner_ident"] = {"sess": "sec:10"}
        before = self._pane_fields(weak)
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: None),
            mock.patch.object(hook.os, "kill", gone),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
            mock.patch.object(hook, "_write_held_fd", return_value=True) as held,
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1") as osc,
        ):
            write, ok = hook.commit_pane(weak, "process_exit")
            self.assertEqual((write, ok), ("none", True))
            self.assertEqual(self._pane_fields(weak), before)
            live = hook.fresh_state()
            live["phase"] = "idle_clear"
            live["busy"] = {"sess": 1.0}
            live["owners"] = {"sess": 50}
            live["owner_ident"] = {"sess": "sec:10"}
            live_before = self._pane_fields(live)
            with mock.patch.object(hook, "process_snapshot", lambda _pid: {"ppid": 1, "lstart": "10", "comm": "kiro"}):
                self.assertEqual(hook.owner_phase(live), "weak")
                write, ok = hook.commit_pane(live, "pid_reuse")
            self.assertEqual((write, ok), ("none", True))
            self.assertEqual(self._pane_fields(live), live_before)
        paint.assert_not_called()
        held.assert_not_called()
        osc.assert_not_called()

    def _blocked_owner(self, phase, token):
        state = hook.fresh_state()
        state["phase"] = phase
        state["busy"] = {"sess": 1.0}
        state["owners"] = {"sess": 50}
        state["owner_ident"] = {"sess": token}
        state["seq"] = 4
        return state

    def _row_proven_blocked(self, _row):
        state = self._blocked_owner("proven", "100.000000")
        before = self._pane_fields(state)
        with (
            self._live()[0],
            self._live()[1],
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1") as osc,
        ):
            write, ok = hook.commit_pane(state, "blocked", "workflow_question_esc_cancel")
        self.assertEqual((write, ok), ("paint_blocked", True))
        self.assertEqual(self._pane_fields(state), before)
        payload, pid = osc.call_args.args
        self.assertEqual(pid, 50)
        self.assertIn(b"state=blocked", payload)
        self.assertIn(b"kind=question", payload)

    def _row_weak_blocked(self, _row):
        state = self._blocked_owner("weak", "sec:10")
        before = self._pane_fields(state)
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: {"ppid": 1, "lstart": "10", "comm": "kiro"}),
            mock.patch.object(hook, "hires_start", lambda _pid: "sec:10"),
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1") as osc,
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            self.assertEqual(hook.pane_transition("weak", "blocked"), "none")
            write, ok = hook.commit_pane(state, "blocked")
        self.assertEqual((write, ok), ("none", True))
        self.assertEqual(self._pane_fields(state), before)
        osc.assert_not_called()
        paint.assert_not_called()

    def _row_no_owner_blocked(self, _row):
        state = hook.fresh_state()
        before = self._pane_fields(state)
        with mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1") as osc:
            self.assertEqual(hook.pane_transition("no_owner", "blocked"), "none")
            write, ok = hook.commit_pane(state, "blocked")
        self.assertEqual((write, ok), ("none", True))
        self.assertEqual(self._pane_fields(state), before)
        osc.assert_not_called()

    def _save(self, state):
        directory = tempfile.mkdtemp(prefix="herdr-kiro-settled-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "pane.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        return path

    def _row_watcher_blocked(self, _row):
        door = inspect.getsource(hook._watch_loop)
        self.assertIn("commit_pane", door)
        self.assertNotIn("commit_blocked", door)
        self.assertNotIn("emit_osc", door)
        self.assertNotIn("send_report", door)
        source = inspect.getsource(hook._run_watch)
        self.assertIn("commit_blocked", source)
        self.assertNotIn("emit_osc", source)
        self.assertNotIn("send_report", source)
        proven = self._save(self._blocked_owner("proven", "100.000000"))
        weak = self._save(self._blocked_owner("weak", "sec:10"))
        empty = self._save(hook.fresh_state())
        with (
            self._live()[0],
            self._live()[1],
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1") as osc,
        ):
            write, ok = hook.commit_blocked(proven, "workflow_requires_approval")
            self.assertEqual((write, ok), ("paint_blocked", True))
            weak_write, weak_ok = hook.commit_blocked(weak, "workflow_requires_approval")
            empty_write, empty_ok = hook.commit_blocked(empty, "")
        self.assertEqual((weak_write, weak_ok), ("none", True))
        self.assertEqual((empty_write, empty_ok), ("none", True))
        self.assertEqual(osc.call_count, 1)
        with open(weak, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["phase"], "weak")
        self.assertEqual(saved["busy"].get("sess"), 1.0)
        self.assertEqual(saved["owners"].get("sess"), 50)
        with open(empty, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["owners"], {})
        self.assertEqual(saved["phase"], "")

    def _row_exit_prompt(self, _row):
        state = self._blocked_owner("proven", "100.000000")
        state["aggregate"] = 1
        with (
            self._live()[0],
            self._live()[1],
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            blocked, ok = hook.commit_pane(state, "blocked")
            self.assertEqual((blocked, ok, state["phase"]), ("paint_blocked", True, "proven"))
            self.assertEqual(hook.pane_transition("proven", "prompt"), "working")
            write, ok = hook.commit_pane(state, "prompt")
        self.assertEqual((write, ok, state["phase"]), ("working", True, "proven"))
        paint.assert_called_once()

    def _row_exit_process(self, _row):
        def gone(_pid, _sig=0):
            raise OSError(errno.ESRCH, "gone")

        state = self._blocked_owner("proven", "100.000000")
        with (
            self._live()[0],
            self._live()[1],
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            blocked, ok = hook.commit_pane(state, "blocked")
            self.assertEqual((blocked, ok, state["phase"]), ("paint_blocked", True, "proven"))
        with (
            mock.patch.object(hook, "process_snapshot", lambda _pid: None),
            mock.patch.object(hook.os, "kill", gone),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            self.assertEqual(hook.pane_transition("proven", "process_exit"), "done")
            write, ok = hook.commit_pane(state, "process_exit")
        self.assertEqual((write, ok), ("done", True))
        self.assertEqual(state["phase"], "exited")
        paint.assert_called()

    def _row_exit_idle(self, _row):
        state = self._blocked_owner("proven", "100.000000")
        with (
            self._live()[0],
            self._live()[1],
            mock.patch.object(hook.herdr_kiro_osc, "write_pid", return_value="/dev/pts/1"),
            mock.patch.object(hook, "paint_status", return_value=True) as paint,
        ):
            blocked, ok = hook.commit_pane(state, "blocked")
            self.assertEqual((blocked, ok, state["phase"]), ("paint_blocked", True, "proven"))
            self.assertEqual(hook.pane_transition("proven", "idle_clear"), "done")
            write, ok = hook.commit_pane(state, "idle_clear")
        self.assertEqual((write, ok), ("done", True))
        self.assertEqual(state["phase"], "idle_clear")
        paint.assert_called()

    def _row_emit_only(self, _row):
        tree = ast.parse(inspect.getsource(hook))
        found = []

        class Finder(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_AsyncFunctionDef(self, node):
                self.visit_FunctionDef(node)

            def visit_Lambda(self, node):
                self.stack.append("<lambda>")
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else ""
                if name == "emit_osc":
                    found.append(self.stack[-1] if self.stack else "<module>")
                self.generic_visit(node)

        Finder().visit(tree)
        self.assertEqual(found, ["commit_pane"])
        self.assertNotIn("emit_osc", inspect.getsource(hook.send_report))


class PaneWriteMatrixTests(unittest.TestCase):
    """Real hook and watcher runs against a temporary state directory.

    Every start, event, owner, and count is one cell. The hook cells call
    main(). The watcher cells call watch_loop(). Nothing that writes is
    stubbed, including log. The log lives in the state directory, so its
    bytes and its existence are part of the snapshot. A none result keeps
    every file byte, including which files exist, and it writes no tty and
    no log. A read, a reload, and a generation mismatch are none results:
    they do not store the seq floor. A refresh changes exactly one value in
    that directory, the busy timestamp. Seq stays, generation stays, no
    floor file is written, and no watcher pid or pid file appears.
    """

    _FLOOR = 50
    _BASE = 1_700_000_000.0
    _RAISED = 80
    _STARTS = (
        "missing",
        "unreadable",
        "stale_generation",
        "floor_ahead",
        "watcher_flag",
        "gone_outer",
        "live_outer",
    )
    _OWNERS = ("no_owner", "weak", "proven", "chat_gone")
    _COUNTS = (0, 1, 2)
    _HOOKS = (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    )
    _WATCH = ("reload", "banner", "continue", "blocked", "idle_clear", "settled")
    _QUIET = "› ask a question or describe a task\n"
    _BANNER = "▶ watch-step running 0/0 · ctrl+x expand · ctrl+g monitor\n"
    _BLOCKED = "esc to close · ↑↓ to navigate · ↵ to select\n"
    _PLAIN = "still writing the reply\n"

    _PINNED_LIVE = "1700000000.111111"
    _PINNED_GONE = "1700000000.222222"
    _PINNED_CHAT = "1700000000.333333"

    @classmethod
    def setUpClass(cls):
        cls._owner_pins = None
        cls.addClassCleanup(cls._stop_owner_pins)
        hook.begin_tick()
        master, slave = os.openpty()
        cls.live_master = master
        # pause() until tearDown. A timed sleep exits during a slow matrix
        # run, and the cell that straddles that exit no longer matches.
        cls.live = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(slave)
        cls.gone = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        cls.chat = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        hook.begin_tick()
        cls.live_token = hook.hires_start(cls.live.pid)
        cls.gone_token = hook.hires_start(cls.gone.pid)
        cls.chat_token = hook.hires_start(cls.chat.pid)
        # No /proc start ticks means sec: plus a whole-second lstart.
        # fine_start rejects that, so a live pid reports a fractional token
        # and identity_proven still has to match it. A dead pid's pin does
        # not apply, so a reused pid stays sec: and unproven.
        if not hook.fine_start(cls.live_token) or not hook.fine_start(cls.gone_token):
            cls._pin_owners_without_proc_ticks()
        if not hook.fine_start(cls.live_token) or not hook.fine_start(cls.gone_token):
            raise AssertionError("live owner has no fine start token")
        os.kill(cls.gone.pid, signal.SIGKILL)
        cls.gone.wait(timeout=5)
        os.kill(cls.chat.pid, signal.SIGKILL)
        cls.chat.wait(timeout=5)
        if cls._owner_pins is not None and hook.fine_start(hook.hires_start(cls.gone.pid)):
            raise AssertionError("dead pid pin still applies")
        hook.begin_tick()
        cls.watcher_token = hook.hires_start(os.getpid())
        if not cls.watcher_token:
            raise AssertionError("watcher token is empty")
        if cls.watcher_token in (cls.live_token, cls.gone_token, cls.chat_token):
            raise AssertionError("watcher token must stay the process start")

    @classmethod
    def _pin_owners_without_proc_ticks(cls):
        pins = {
            cls.live.pid: (cls._PINNED_LIVE, cls.live),
            cls.gone.pid: (cls._PINNED_GONE, cls.gone),
            cls.chat.pid: (cls._PINNED_CHAT, cls.chat),
        }
        real = hook.hires_start

        def hires(asked, real=real, pins=pins):
            return pinned_start(real, pins, asked)

        cls._owner_pins = mock.patch.object(hook, "hires_start", side_effect=hires)
        cls._owner_pins.start()
        cls.live_token = cls._PINNED_LIVE
        cls.gone_token = cls._PINNED_GONE
        cls.chat_token = cls._PINNED_CHAT

    @classmethod
    def _stop_owner_pins(cls):
        patch = cls._owner_pins
        cls._owner_pins = None
        if patch is not None:
            patch.stop()

    @classmethod
    def tearDownClass(cls):
        if cls.live.poll() is None:
            os.kill(cls.live.pid, signal.SIGKILL)
            cls.live.wait(timeout=5)
        os.close(cls.live_master)
        hook._close_held_fd()

    def test_every_public_entry_snapshots_the_state_directory(self):
        events = self._HOOKS + self._WATCH
        ran = []
        problems = []
        for start in self._STARTS:
            for event in events:
                for owner in self._OWNERS:
                    for count in self._COUNTS:
                        key = (start, event, owner, count)
                        ran.append(key)
                        try:
                            problem = self._cell(start, event, owner, count, "")
                        except Exception as exc:
                            problem = f"{key} raised {type(exc).__name__}: {exc}"
                        if problem:
                            problems.append(problem)
        for key in self._extra_keys():
            ran.append(key)
            start, event, owner, count, mode = key
            try:
                problem = self._cell(start, event, owner, count, mode)
            except Exception as exc:
                problem = f"{key} raised {type(exc).__name__}: {exc}"
            if problem:
                problems.append(problem)
        product = len(self._STARTS) * len(events) * len(self._OWNERS) * len(self._COUNTS)
        product += len(self._extra_keys())
        self.assertEqual(len(ran), product)
        self.assertEqual(len(set(ran)), product)
        if problems:
            preview = "\n".join(problems[:12])
            self.fail(f"{len(problems)} cells differed\n{preview}")

    def test_writers_reach_state_only_through_commit_pane(self):
        # main and watch_loop are the entries. A function that writes the
        # pane, the seq floor, the log in the state directory, a watcher pid
        # file, the tty, or OSC has to sit behind commit_pane.
        path = os.path.join(os.path.dirname(hook.__file__), "herdr_kiro_state.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        defined = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                defined.append(node.name)
        names = set(defined)
        graph = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = set()
            for child in ast.walk(node):
                called = isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                if called and child.func.id in names:
                    calls.add(child.func.id)
            graph[node.name] = calls
        writers = {
            "update",
            "write_seq_floor",
            "log",
            "write_pidfile",
            "remove_pidfile",
            "sweep_stale_pidfiles",
            "clear_watcher",
            "ensure_watcher",
            "screen_view",
            "explain_env",
            "emit_osc",
            "paint_status",
            "send_report",
            "emit_report",
            "release_authority",
            "herdr",
            "commit_blocked",
            "note_report",
            "apply_owner_exit",
            "finish_if_unchanged",
            "_write_refresh",
            "_write_held_fd",
        }
        seen = set()
        stack = ["main", "watch_loop"]
        while stack:
            name = stack.pop()
            if name in seen or name == "commit_pane":
                continue
            seen.add(name)
            stack.extend(sorted(graph.get(name, ())))
        reached = sorted(name for name in seen if name in writers)
        self.assertEqual(reached, [])

    def _extra_keys(self):
        keys = []
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            for owner in self._OWNERS:
                for count in self._COUNTS:
                    keys.append(("chooser", event, owner, count, "chooser"))
        for event in self._WATCH:
            for count in self._COUNTS:
                keys.append(("weak_pidfile", event, "weak", count, "weak_pidfile"))
        for event in ("Stop", "SessionEnd", "idle_clear", "settled", "blocked"):
            for owner in ("proven", "chat_gone"):
                for count in self._COUNTS:
                    keys.append(("held_unconfirmed", event, owner, count, "held"))
        for event in self._HOOKS + self._WATCH:
            for owner in self._OWNERS:
                for count in self._COUNTS:
                    keys.append(("app_fail", event, owner, count, "app_fail"))
        return keys

    def _cell(self, start, event, owner, count, mode):
        root = tempfile.mkdtemp(prefix="herdr-kiro-matrix-")
        previous_hup = signal.getsignal(signal.SIGHUP)
        previous_term = signal.getsignal(signal.SIGTERM)
        try:
            return self._run_cell(root, start, event, owner, count, mode)
        finally:
            signal.signal(signal.SIGHUP, previous_hup)
            signal.signal(signal.SIGTERM, previous_term)
            self._kill_watchers(root)
            hook._close_held_fd()
            shutil.rmtree(root, ignore_errors=True)

    def _run_cell(self, root, start, event, owner, count, mode):
        state_root = os.path.join(root, "herdr-kiro")
        os.makedirs(state_root, mode=0o700)
        env = {
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HC_TERMINAL_ID": "t7",
            "XDG_STATE_HOME": root,
            "HERDR_KIRO_LOG": os.path.join(state_root, "hook.log"),
            "HERDR_BIN_PATH": os.path.join(root, "missing-herdr"),
        }
        hook.begin_tick()
        self._drain(self.live_master)
        file_start = self._file_start(start)
        prepared = self._prepared_state(file_start, event, owner, count)
        watching = event in self._WATCH
        spawned = file_start in ("missing", "unreadable") and event == "SessionStart"
        if prepared is not None and (watching or self._seeds_watcher(prepared, event) or file_start == "watcher_flag"):
            prepared["watcher"] = os.getpid()
            prepared["watcher_started"] = self.watcher_token
        self._pane_mode = mode
        if mode == "held":
            self._arm_unconfirmed_hold()
        initial = self._initial_files(file_start, prepared, watching)
        if mode == "chooser":
            initial[f"pids/{self.gone.pid}"] = f"{os.getuid()}\nnot-the-start\nstale\n".encode()
        planted = {}
        if self._plants_pids(file_start, event, prepared, watching, mode):
            planted = self._planted_pidfiles()
            initial.update(planted)
        self._planted_names = tuple(planted)
        self._write_files(state_root, initial)
        before = self._snapshot(state_root)
        expected_files, expected_osc = self._expect(file_start, event, prepared, watching)
        if mode == "chooser":
            name = f"pids/{self.gone.pid}"
            expected_files[name] = initial[name]
        for name, raw in planted.items():
            expected_files[name] = raw
        clock = [self._BASE]
        with mock.patch.dict(os.environ, env, clear=False):
            # A cached explain env would skip the pid sweep. Each cell explains fresh.
            if planted or mode == "chooser":
                hook._EXPLAIN_ENV = None
                hook._SCREEN_CACHE.clear()
                hook._RULES_DIR = None
            path = hook.state_path("t7")
            self._drive(path, file_start, event, watching, clock, prepared, mode)
            self._kill_watchers(root)
            actual = self._snapshot(state_root)
            osc = self._drain(self.live_master)
        if spawned:
            problem = self._check_spawn(expected_files, actual)
            if problem:
                return f"{start} {event} {owner} count={count} {problem}"
        if actual != expected_files or osc != expected_osc:
            return self._format_problem(start, event, owner, count, before, actual, expected_files, osc, expected_osc)
        return ""

    def _file_start(self, start):
        if start == "chooser":
            return "live_outer"
        if start == "weak_pidfile":
            return "gone_outer"
        if start == "held_unconfirmed":
            return "floor_ahead"
        if start == "app_fail":
            return "live_outer"
        return start

    def _plants_pids(self, file_start, event, prepared, watching, mode):
        if mode == "app_fail" or event in ("idle_clear", "settled"):
            return True
        return self._classify_write(file_start, event, prepared, watching) in ("none", "refresh")

    def _planted_pidfiles(self):
        stale = f"{os.getuid()}\nnot-the-live-start\nstale-comm\n".encode()
        mismatch = f"{os.getuid()}\n1700000000.999999\nnot-the-comm\n".encode()
        return {
            f"pids/{self.gone.pid}": stale,
            f"pids/{self.live.pid}": mismatch,
        }

    def _classify_write(self, _file_start, event, prepared, watching):
        state = copy.deepcopy(prepared) if prepared is not None else hook.fresh_state()
        if not watching:
            chosen, _ok, _flag, _osc, _release = self._hook_fold(state, event)
            return chosen
        if hook._owner_exited(state, []):
            write, _ok, _osc = self._commit(state, "process_exit", 0.0)
            return write
        if hook._owner_is_live_weak(state):
            self._commit(state, "pid_reuse", 0.0)
        if event == "blocked":
            screen_event = "blocked"
        elif event == "banner":
            screen_event = "start"
        elif event in ("continue", "reload") and state.get("busy"):
            screen_event = "prompt"
        elif event == "idle_clear":
            screen_event = "idle_clear"
        elif event == "settled":
            screen_event = "stop"
        else:
            return "none"
        write, _ok, _osc = self._commit(state, screen_event, self._BASE)
        return write

    def _arm_unconfirmed_hold(self):
        hook._close_held_fd()
        _master, slave = os.openpty()
        hook._pane_fd = os.dup(slave)
        os.close(slave)
        os.close(_master)
        hook._held_ident = self.live_token
        hook._held_pid = 0

    def _drive(self, path, start, event, watching, clock, prepared, mode):
        def now():
            return clock[0]

        def sleep(_delay):
            return None

        def screen(_pane, path=path, event=event, start=start, mode=mode):
            if mode == "app_fail":
                return None
            if mode == "chooser":
                return self._BLOCKED
            if start == "stale_generation" and event in ("idle_clear", "settled"):
                self._bump_generation(path)
            return self._screen_for(event)

        if not watching:
            with (
                mock.patch.object(hook.time, "time", now),
                mock.patch.object(hook.time, "sleep", sleep),
                mock.patch.object(sys, "argv", ["herdr-kiro", event]),
                mock.patch.object(sys, "stdin", io.StringIO("{}")),
                mock.patch.object(hook, "pane_text", screen),
            ):
                self.assertEqual(hook.main(), 0)
            return
        limit = self._tick_limit(event, prepared)
        ticks = {"n": 0}

        def wait(_self, _pids, _timeout, clock=clock, limit=limit, event=event, ticks=ticks):
            ticks["n"] += 1
            if event == "idle_clear" and ticks["n"] == 2:
                clock[0] = self._BASE + hook.IDLE_CLEAR_SECS + 1
            if ticks["n"] > limit:
                raise SystemExit
            return False

        with (
            mock.patch.object(hook.time, "time", now),
            mock.patch.object(hook.time, "sleep", sleep),
            mock.patch.object(hook.OwnerExitWatch, "wait", wait),
            mock.patch.object(hook, "pane_text", screen),
        ):
            try:
                hook.watch_loop("t7", path)
            except SystemExit:
                return

    def _tick_limit(self, event, prepared):
        if prepared is not None and self._exit_retries(prepared):
            return hook.EXIT_RETRY_CAP + 2
        if event == "idle_clear" and prepared is not None and prepared.get("busy"):
            return 2
        return 1

    def _seeds_watcher(self, state, event):
        if event in self._WATCH:
            return True
        trial = copy.deepcopy(state)
        chosen, _ok, _flag, _osc, _release = self._hook_fold(trial, event)
        # refresh must not be given a watcher ahead of the call. The run
        # has to leave watcher 0 and create no pid file.
        return chosen not in ("none", "refresh")

    def _prepared_state(self, start, event, owner, count):
        if start in ("missing", "unreadable"):
            return None
        state = hook.fresh_state()
        state["seq"] = 4
        state["aggregate"] = count
        state["busy"] = {hook.AGGREGATE: self._BASE - 10, "old": self._BASE - 10000}
        state["grace_until"] = 3.5
        state["generation"] = 7
        self._put_owner(state, start, owner)
        if event in ("reload", "settled"):
            state["busy"] = {}
            state["grace_until"] = 0.0
        return state

    def _put_owner(self, state, start, owner):
        gone = start == "gone_outer"
        pid = self.gone.pid if gone else self.live.pid
        token = self.gone_token if gone else self.live_token
        if owner == "no_owner":
            return
        if owner == "weak":
            state["phase"] = "weak"
            state["owners"] = {hook.AGGREGATE: pid}
            state["owner_ident"] = {hook.AGGREGATE: "sec:10"}
            return
        state["phase"] = "proven"
        state["owners"] = {hook.AGGREGATE: pid}
        state["owner_ident"] = {hook.AGGREGATE: token}
        state["proven_pid"] = pid
        state["proven_ident"] = token
        if owner == "chat_gone":
            state["chat_pid"] = self.chat.pid
            state["chat_ident"] = self.chat_token

    def _initial_files(self, start, prepared, watching):
        files = {}
        if start == "missing":
            pane = None
        elif start == "unreadable":
            pane = b"not-json"
            files["t7.json.seq"] = str(self._FLOOR).encode()
        else:
            pane = self._dump(prepared)
            files["t7.json.seq"] = str(self._FLOOR).encode()
        if pane is not None:
            files["t7.json"] = pane
        if watching and prepared is not None and int(prepared.get("watcher") or 0) == os.getpid():
            files[f"pids/{os.getpid()}"] = self._pidfile(os.getpid())
        return files

    def _expect(self, start, event, prepared, watching):
        if watching:
            return self._expect_watch(start, event, prepared)
        return self._expect_hook(start, event, prepared)

    def _expect_hook(self, start, event, prepared):
        book = _Book(self._initial_book(start, prepared, False))
        osc = []
        held = {"chosen": "none", "ok": False, "release": False}

        def fold(state, osc=osc, event=event, held=held):
            chosen, ok, flag, painted, release = self._hook_fold(state, event)
            held["chosen"] = chosen
            held["ok"] = ok
            held["release"] = release
            osc.append(painted)
            return flag

        book.apply(fold, self._BASE)
        painted = b"".join(osc)
        busy = len((book.state or {}).get("busy") or {})
        grace = float((book.state or {}).get("grace_until") or 0.0)
        reported = self._report_after_hook(book, event, held["chosen"], held["ok"], held["release"])
        if held["chosen"] not in ("none", "refresh") and not self._watcher_current(book.state):
            self._spawn_into(book)
        blocked_osc = b""
        if self._chooser_follow_up(book.state, held["chosen"]):
            blocked_osc = self._osc("blocked")
            if reported == "":
                book.apply(lambda state: self._bump_report(state, "blocked"), self._BASE)
            reported = "blocked"
        if held["chosen"] not in ("none", "refresh"):
            self._record_hook_log(book, event, painted, busy, grace, reported, blocked_osc)
        return book.files, painted + blocked_osc

    def _expect_watch(self, start, event, prepared):
        book = _Book(self._initial_book(start, prepared, True))
        osc = []
        limit = self._tick_limit(event, prepared)
        clock = self._BASE
        reported = None
        quiet_since = None
        for tick in range(1, limit + 1):
            if event == "idle_clear" and tick == 2:
                clock = self._BASE + hook.IDLE_CLEAR_SECS + 1
            if book.state is None:
                book.state = hook.fresh_state()
            if hook._owner_exited(book.state, []):
                self._watch_exit(book, osc, event)
                break
            if hook._owner_is_live_weak(book.state):
                self._arm_before(book, clock, "pid_reuse")
                book.apply(lambda state: self._commit(state, "pid_reuse", 0.0)[0], clock)
            stale = start == "stale_generation" and event in ("idle_clear", "settled")
            if stale and book.files.get("t7.json"):
                book.state["generation"] = int(book.state.get("generation") or 0) + 1
                book.files["t7.json"] = self._dump(book.state)
            painted, quiet_since, reported, stopped = self._watch_screen(
                book, event, clock, quiet_since, reported, stale
            )
            osc.extend(painted)
            if stopped:
                break
        painted = b"".join(osc)
        self._flush_watch_log(book)
        return book.files, painted

    def _watch_exit(self, book, osc, event):
        mark = {"write": "none", "ok": True, "painted": b""}

        def once(state, mark=mark):
            write, ok, painted = self._commit(state, "process_exit", 0.0)
            mark["write"] = write
            mark["ok"] = ok
            mark["painted"] = painted
            return write

        held_done = self._done_via_held(book.state)
        self._arm_before(book, self._BASE, "process_exit")
        book.apply(once, self._BASE)
        osc.append(mark["painted"])
        if mark["write"] == "none":
            return
        self._arm_log(book, self._BASE)
        self._note_watch_osc(book, self._BASE, mark["painted"], held_done)
        if mark["write"] == "done" and not mark["ok"]:
            for _retry in range(hook.EXIT_RETRY_CAP - 1):
                book.apply(once, self._BASE)
                osc.append(mark["painted"])
            missed_at = self._BASE
            if event == "idle_clear":
                missed_at = self._BASE + hook.IDLE_CLEAR_SECS + 1
            self._note(book, missed_at, "watcher exit done missed")
        else:
            self._note(book, self._BASE, "watcher exit owner exit")
        self._clear_watch_slot(book)

    def _watch_screen(self, book, event, now, quiet_since, reported, stale):
        # A failed App read is none. It is not "", a prompt, a banner, a
        # start, or aggregate_zero, and it is not refresh. A held banner
        # or blocker is not painted from a screen that was not returned.
        if self._pane_mode == "app_fail":
            return [], quiet_since, reported, False
        screen = self._screen_for(event)
        painted = []
        if hook.approval_text(screen):
            mark = {"write": "none", "ok": False}

            def blocked(state, painted=painted, mark=mark):
                write, ok, osc = self._commit(state, "blocked", 0.0)
                mark["write"] = write
                mark["ok"] = ok
                if ok:
                    painted.append(osc)
                if not ok:
                    return "none"
                return write

            held_done = self._done_via_held(book.state)
            self._arm_before(book, now, "blocked")
            book.apply(blocked, now)
            if mark["write"] == "paint_blocked" and mark["ok"]:
                self._arm_log(book, now)
                self._note_watch_osc(book, now, painted[-1] if painted else b"", held_done)
                book.apply(lambda state: self._bump_report(state, "blocked"), now)
                self._note_report(book, now, "blocked")
            return painted, None, reported, False
        banner = hook.workflow_banner_from(screen)
        if banner:
            quiet_since = None
        elif hook.prompt_visible(screen) and hook.screen_is_settled(screen):
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= hook.IDLE_CLEAR_SECS and book.state.get("busy") and not stale:
                mark = {"write": "none", "ok": False}

                def clear(state, painted=painted, mark=mark):
                    write, ok, osc = self._commit(state, hook.screen_event(state, "timer"), 0.0)
                    mark["write"] = write
                    mark["ok"] = ok
                    painted.append(osc)
                    return write

                held_done = self._done_via_held(book.state)
                self._arm_before(book, now, hook.screen_event(book.state, "timer"))
                book.apply(clear, now)
                if mark["write"] == "done" and mark["ok"]:
                    self._arm_log(book, now)
                    self._note_watch_osc(book, now, painted[-1] if painted else b"", held_done)
                    self._note(book, now, "screen idle clear")
        else:
            quiet_since = None
        action = hook.next_action(len(book.state["busy"]), float(book.state["grace_until"]), banner, now)
        owners_gone = False
        if banner and not owners_gone:
            mark = {"write": "none"}

            def start(state, painted=painted, mark=mark):
                write, ok, osc = self._commit(state, "start", 0.0)
                mark["write"] = write
                mark["ok"] = ok
                painted.append(osc)
                if write == "working" and not ok:
                    return "none"
                return write

            held_done = self._done_via_held(book.state)
            self._arm_before(book, now, "start")
            book.apply(start, now)
            if mark["write"] == "working" and mark.get("ok"):
                self._arm_log(book, now)
                self._note_watch_osc(book, now, painted[-1] if painted else b"", held_done)
                book.apply(lambda state: self._bump_report(state, "working"), now)
                self._note_report(book, now, "working")
            return painted, quiet_since, reported, False
        if action == "idle" and hook.screen_is_quiet(screen):
            if reported != "idle" and not stale:
                mark = {"write": "none", "ok": False}

                def settle(state, painted=painted, mark=mark):
                    write, ok, osc = self._commit(state, hook.screen_event(state, "settled"), 0.0)
                    mark["write"] = write
                    mark["ok"] = ok
                    painted.append(osc)
                    return write

                held_done = self._done_via_held(book.state)
                self._arm_before(book, now, hook.screen_event(book.state, "settled"))
                book.apply(settle, now)
                if mark["write"] == "done" and mark["ok"]:
                    self._arm_log(book, now)
                    self._note_watch_osc(book, now, painted[-1] if painted else b"", held_done)
                    book.apply(lambda state: self._bump_report(state, "idle"), now)
                    self._note_report(book, now, "idle")
                    self._note(book, now, "watcher idle")
                    reported = "idle"
                else:
                    reported = "idle"
            held = quiet_since is not None and now - quiet_since >= hook.IDLE_DELAY
            if reported == "idle" and not book.state.get("busy") and held:
                self._clear_watch_slot(book)
                return painted, quiet_since, reported, True
            return painted, quiet_since, reported, False
        if action != "working":
            return painted, quiet_since, reported, False

        mark = {"write": "none"}

        def prompt(state, painted=painted, mark=mark, now=now):
            write, ok, osc = self._commit(state, "prompt", now)
            mark["write"] = write
            mark["ok"] = ok
            painted.append(osc)
            if write == "working" and not ok:
                return "none"
            return write

        held_done = self._done_via_held(book.state)
        self._arm_before(book, now, "prompt", now)
        book.apply(prompt, now)
        if mark["write"] == "working" and mark.get("ok"):
            self._arm_log(book, now)
            self._note_watch_osc(book, now, painted[-1] if painted else b"", held_done)
            book.apply(lambda state: self._bump_report(state, "working"), now)
            self._note_report(book, now, "working")
        return painted, quiet_since, reported, False

    def _clear_watch_slot(self, book):
        def clear(state):
            if int(state.get("watcher") or 0) == os.getpid():
                state["watcher"] = 0
                state["watcher_started"] = ""
            return ""

        book.apply(clear, self._BASE)

    def _claim(self, state, token):
        state["watcher"] = os.getpid()
        state["watcher_started"] = token
        return ""

    def _bump_report(self, state, kind):
        state["seq"] = int(state.get("seq") or 0) + 1
        if kind == "idle":
            state["grace_until"] = 0.0
        return ""

    def _report_after_hook(self, book, event, chosen, ok, release):
        if self._pane_mode == "app_fail":
            return ""
        if book.state is None:
            return ""
        action = hook.next_action(len(book.state["busy"]), float(book.state["grace_until"]), False, self._BASE)
        kind = hook.report_for("", action)
        if chosen == "done" and ok and (event in hook.END_EVENTS or release):
            book.apply(lambda state: self._bump_report(state, "idle"), self._BASE)
            return "idle"
        if kind == "working" and chosen == "working":
            book.apply(lambda state: self._bump_report(state, "working"), self._BASE)
            return "working"
        return ""

    def _chooser_follow_up(self, state, chosen):
        if self._pane_mode != "chooser" or chosen in ("none", "refresh") or state is None:
            return False
        if not self._can_paint(state):
            return False
        return hook.pane_transition(hook.owner_phase(state), "blocked") == "paint_blocked"

    def _record_hook_log(self, book, event, osc, busy, grace, reported, blocked_osc):
        lines = [f"event={event} session={hook.AGGREGATE} pane=t7 chain={hook.process_chain()}"]
        device = self._owner_device(self.live.pid)
        if osc and device:
            if b"state=done" in osc:
                kind = "done"
            elif b"state=blocked" in osc:
                kind = "blocked"
            else:
                kind = "working"
            lines.append(f"osc {kind} via {device}")
        lines.append(f"state event={event} busy={busy} grace_until={grace:.3f}")
        if book.spawned:
            lines.append("watcher spawned pid=__SPAWN__")
        if blocked_osc and device:
            lines.append(f"osc blocked via {device}")
        if reported and book.state is not None:
            seq = int(book.state.get("seq") or 0)
            lines.append(
                "herdr report-agent t7 --source "
                + hook.SOURCE
                + " --agent "
                + hook.AGENT
                + " --state "
                + reported
                + " --seq "
                + str(seq)
            )
        self._append_log(book, lines)

    def _note(self, book, clock, line):
        book.logs.append((float(clock), line))

    def _arm_before(self, book, clock, event, now=None):
        stamp = clock if now is None else now
        base = book.state if book.state is not None else hook.fresh_state()
        trial = copy.deepcopy(base)
        write, ok, _osc = self._commit(trial, event, stamp)
        if write in ("none", "refresh") or (not ok and trial == base):
            return
        self._arm_log(book, clock)

    def _arm_log(self, book, clock):
        if book.armed:
            return
        book.armed = True
        self._note(book, clock, "watcher loop pane=t7")
        book.apply(lambda state, token=self.watcher_token: self._claim(state, token), clock)
        if book.state is not None and int(book.state.get("watcher") or 0) == os.getpid():
            book.files[f"pids/{os.getpid()}"] = self._pidfile(os.getpid())

    def _note_watch_osc(self, book, clock, osc, held_done):
        if not osc:
            return
        if b"state=done" in osc:
            kind = "done"
            where = "held" if held_done else self._owner_device(self.live.pid)
        elif b"state=blocked" in osc:
            kind = "blocked"
            where = self._owner_device(self.live.pid)
        else:
            kind = "working"
            where = self._owner_device(self.live.pid)
        if where:
            self._note(book, clock, f"osc {kind} via {where}")

    def _note_report(self, book, clock, kind):
        if book.state is None:
            return
        seq = int(book.state.get("seq") or 0)
        self._note(
            book,
            clock,
            "herdr report-agent t7 --source "
            + hook.SOURCE
            + " --agent "
            + hook.AGENT
            + " --state "
            + kind
            + " --seq "
            + str(seq),
        )

    def _done_via_held(self, state):
        if state is None:
            return False
        owner = hook._pane_owner(state)
        if owner is None:
            return False
        pid, token = owner
        return hook.alive(pid) and hook.identity_proven(pid, token)

    def _flush_watch_log(self, book):
        if not book.logs:
            return
        text = "".join(f"{clock:.3f} pid={os.getpid()} {line}\n" for clock, line in book.logs)
        book.files["hook.log"] = text.encode()

    def _append_log(self, book, lines):
        text = "".join(f"{self._BASE:.3f} pid={os.getpid()} {line}\n" for line in lines)
        book.files["hook.log"] = book.files.get("hook.log", b"") + text.encode()

    def _owner_device(self, pid):
        found = list(hook.herdr_kiro_osc._tty_opens(pid))
        if len(found) != 1:
            return ""
        return found[0][1]

    def _spawn_into(self, book):
        book.spawned = True
        if book.state is None:
            return
        book.state["watcher_at"] = self._BASE
        book.state["retiring"] = False
        book.files["t7.json"] = self._dump(book.state)

    def _check_spawn(self, expected, actual):
        raw = actual.get("t7.json")
        if not raw:
            return "spawn left no pane file"
        try:
            state = json.loads(raw)
        except ValueError:
            return "spawn left a pane that is not json"
        pid = int(state.get("watcher") or 0)
        token = state.get("watcher_started") or ""
        if pid <= 1 or not token:
            return f"spawn did not record a watcher ({pid})"
        pid_name = f"pids/{pid}"
        pid_bytes = actual.get(pid_name, b"")
        if not pid_bytes.startswith(f"{os.getuid()}\n{token}\n".encode()):
            return "spawn pidfile does not name the recorded watcher"
        expected_state = json.loads(expected["t7.json"])
        expected_state["watcher"] = pid
        expected_state["watcher_started"] = token
        expected_state["watcher_at"] = self._BASE
        expected["t7.json"] = self._dump(expected_state)
        expected.pop("pids/" + str(os.getpid()), None)
        planted = {name: expected[name] for name in getattr(self, "_planted_names", ()) if name in expected}
        for name in list(expected):
            if name.startswith("pids/"):
                expected.pop(name)
        expected[pid_name] = pid_bytes
        expected.update(planted)
        raw_log = expected.get("hook.log")
        if raw_log and b"__SPAWN__" in raw_log:
            expected["hook.log"] = raw_log.replace(b"__SPAWN__", str(pid).encode())
        return ""

    def _hook_fold(self, state, event):
        owner_before = hook._pane_owner(state)
        ev = hook.choose_pane_event(state, event, hook.AGGREGATE)
        write, ok, osc = self._commit(state, ev, self._BASE, session=hook.AGGREGATE, hook_name=event)
        chosen = write
        chosen_ok = ok
        release = bool(write == "done" and ok and owner_before is not None and not hook.alive(owner_before[0]))
        if not (write == "none" or not ok or write == "refresh"):
            hook.note_generation(state, state)
        if event not in hook.END_EVENTS and ev != "aggregate_zero":
            pane_ev = hook.hook_event(state, event)
            owed = event == "SessionStart" and hook._proven_exit_owed(state)
            if pane_ev != ev and not owed:
                owner_now = hook._pane_owner(state)
                dead = owner_now is not None and not hook.alive(owner_now[0])
                write2, ok2, osc2 = self._commit(state, pane_ev, 0.0)
                write = write2
                if write2 != "none":
                    chosen = write2
                    chosen_ok = ok2
                    osc = osc2
                    if write2 == "done" and ok2 and dead:
                        release = True
        return chosen, chosen_ok, write, osc, release

    def _commit(self, state, event, now, session="", hook_name="", rule_id=""):
        count = int(state.get("aggregate") or 0)
        prompt_at_zero = count == 0 and event in ("prompt", "start")
        stop_at_zero = session == hook.AGGREGATE and count == 0 and event in ("stop", "piped_chat")
        if prompt_at_zero or stop_at_zero:
            event = "aggregate_zero"
        elif event not in hook._DIRECT_EVENTS:
            event = hook._chooser_event(state, event)
        phase = hook._phase_for_event(state, event)
        write = hook.pane_transition(phase, event)
        if write == "none":
            return write, True, b""
        if write == "refresh":
            # An unconfirmed hold is none. Refresh is only a proven owner's
            # busy timestamp, and this pid is not that owner.
            if hook._owner_held_unconfirmed(state):
                return "none", True, b""
            if hook.AGGREGATE in state["busy"]:
                state["busy"][hook.AGGREGATE] = now if now else self._BASE
            return write, True, b""
        if write == "paint_blocked":
            owner_now = hook._pane_owner(state)
            if owner_now is not None and hook._held_pid_unconfirmed(owner_now[0], owner_now[1]):
                return write, False, b""
            ok = self._can_paint(state)
            return write, ok, self._osc("blocked", rule_id) if ok else b""
        if write == "record_aggregate":
            if hook._owner_held_unconfirmed(state):
                return "none", True, b""
            state["aggregate"] = int(state.get("aggregate") or 0) + 1
            state["busy"][hook.AGGREGATE] = now
            state["grace_until"] = 0.0
            return write, True, b""
        if write == "touch_aggregate":
            if hook._owner_held_unconfirmed(state):
                return "none", True, b""
            state["busy"][hook.AGGREGATE] = now
            return write, True, b""
        if write == "touch_partial":
            # The old oracle wrote the partial stop for an unconfirmed hold.
            # That pid writes nothing: no count, no busy stamp, no grace.
            if hook._owner_held_unconfirmed(state):
                return "none", True, b""
            if count > 1:
                state["aggregate"] = count - 1
            state["busy"][hook.AGGREGATE] = now
            state["grace_until"] = 0.0
            return write, True, b""
        if write == "record_named":
            if hook._owner_held_unconfirmed(state):
                return "none", True, b""
            return write, True, b""
        if write == "reap":
            return write, True, b""
        if write == "done":
            owner_now = hook._pane_owner(state)
            if owner_now is not None and hook._held_pid_unconfirmed(owner_now[0], owner_now[1]):
                return "none", False, b""
            return self._commit_done(state, event, now, session, hook_name, phase)
        if write == "working":
            owner_now = hook._pane_owner(state)
            if owner_now is not None and hook._held_pid_unconfirmed(owner_now[0], owner_now[1]):
                return write, False, b""
            ok = self._can_paint(state)
            state["phase"] = hook.pane_next(phase, event)
            return write, ok, self._osc("working") if ok else b""
        raise AssertionError(write)

    def _commit_done(self, state, event, now, session, hook_name, phase):
        drops = bool(session) and hook_name in hook.END_EVENTS and hook._chooser_drops_row(state, hook_name)
        owner_now = hook._pane_owner(state)
        if owner_now is not None and hook.alive(owner_now[0]):
            drops = False
        stamp = now if now else self._BASE
        if owner_now is None:
            if drops:
                hook._finish_slot(state, session, stamp)
            return "done", True, b""
        hook._save_proven_owner(state)
        if not self._can_paint(state):
            return "done", False, b""
        state["phase"] = hook.pane_next(phase, event)
        owner_after = hook._pane_owner(state)
        if owner_after is not None and not hook.alive(owner_after[0]):
            hook._drop_done_owner(state, owner_after[0])
        if drops:
            hook._finish_slot(state, session, stamp)
        return "done", True, self._osc("idle")

    def _can_paint(self, state):
        if hook.in_tui():
            return False
        owner = hook._pane_owner(state)
        if owner is None:
            return False
        pid, token = owner
        if not hook.fine_start(token) or hook._pid_gone(pid):
            return False
        return hook.identity_proven(pid, token)

    def _exit_retries(self, state):
        if not hook._owner_exited(state, []):
            return False
        trial = copy.deepcopy(state)
        write, ok, _osc = self._commit(trial, "process_exit", 0.0)
        return write == "done" and not ok

    def _bump_generation(self, path):
        try:
            with open(path, encoding="utf-8") as handle:
                state = json.loads(handle.read())
        except (OSError, ValueError):
            return
        if not isinstance(state, dict):
            return
        state["generation"] = int(state.get("generation") or 0) + 1
        raw = json.dumps(state)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw)

    def _screen_for(self, event):
        if event == "banner":
            return self._BANNER
        if event == "blocked":
            return self._BLOCKED
        if event in ("idle_clear", "settled"):
            return self._QUIET
        return self._PLAIN

    def _osc(self, kind, rule_id=""):
        return hook.herdr_kiro_osc.encode(hook.herdr_kiro_osc.fields_for(kind, rule_id))

    def _watcher_current(self, state):
        if state is None:
            return False
        try:
            pid = int(state.get("watcher") or 0)
        except (TypeError, ValueError):
            return False
        return hook._process_matches(pid, state.get("watcher_started") or "")

    def _initial_book(self, start, prepared, pidfile):
        files = {}
        if start == "unreadable":
            files["t7.json"] = b"not-json"
            files["t7.json.seq"] = str(self._FLOOR).encode()
        elif prepared is not None:
            files["t7.json"] = self._dump(prepared)
            files["t7.json.seq"] = str(self._FLOOR).encode()
            if pidfile and int(prepared.get("watcher") or 0) == os.getpid():
                files[f"pids/{os.getpid()}"] = self._pidfile(os.getpid())
        return files

    def _pidfile(self, pid):
        token = hook.hires_start(pid)
        snap = hook.process_snapshot(pid)
        comm = ""
        if snap and not snap.get("error"):
            comm = snap.get("comm") or ""
        return f"{os.getuid()}\n{token}\n{comm}\n".encode()

    def _dump(self, state):
        return json.dumps(state).encode()

    def _write_files(self, root, files):
        for name, raw in files.items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(raw)

    def _snapshot(self, root):
        found = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                with open(path, "rb") as handle:
                    found[rel] = handle.read()
        return found

    def _drain(self, master):
        chunks = []
        while True:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _kill_watchers(self, root):
        needle = root.encode()
        with contextlib.suppress(OSError):
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                pid = int(name)
                if pid == os.getpid():
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as handle:
                        command = handle.read()
                except OSError:
                    continue
                if b"--watch" in command and needle in command:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
        self._kill_ps_watchers(needle)

    def _kill_ps_watchers(self, needle):
        env = dict(os.environ)
        env["COLUMNS"] = "4096"
        try:
            completed = subprocess.run(
                ["ps", "-axww", "-o", "pid=,command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return
        for line in (completed.stdout or b"").splitlines():
            pid_text, _sep, command = line.strip().partition(b" ")
            if not pid_text.isdigit() or int(pid_text) == os.getpid():
                continue
            if b"--watch" in command and needle in command:
                with contextlib.suppress(OSError):
                    os.kill(int(pid_text), signal.SIGKILL)

    def _format_problem(self, start, event, owner, count, before, actual, expected, osc, expected_osc):
        lines = [f"{start} {event} {owner} count={count}"]
        names = sorted(set(before) | set(actual) | set(expected))
        for name in names:
            if actual.get(name) != expected.get(name):
                lines.append(f"  {name}: actual {actual.get(name)!r:.180} expected {expected.get(name)!r:.180}")
        if osc != expected_osc:
            lines.append(f"  osc actual {osc!r} expected {expected_osc!r}")
        return "\n".join(lines)


class _Book:
    """The post-fix directory a cell is allowed to leave behind."""

    def __init__(self, files):
        self.files = dict(files)
        self.state = None
        self.floor = 0
        self.spawned = False
        self.logs = []
        self.armed = False
        self._load()

    def _load(self):
        raw = self.files.get("t7.json")
        floor_raw = self.files.get("t7.json.seq")
        try:
            self.floor = int(floor_raw.decode()) if floor_raw else 0
        except ValueError:
            self.floor = 0
        if raw is None:
            self.state = None
            return
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        self.state = parsed if isinstance(parsed, dict) else None

    def apply(self, fn, now):
        raw = self.files.get("t7.json")
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        existed = raw is not None
        loaded = parsed if isinstance(parsed, dict) else hook.fresh_state()
        before = copy.deepcopy(loaded)
        flag = fn(loaded) or ""
        changed = loaded != before
        if not changed and flag in ("", "none", "refresh"):
            if not existed:
                return
            return
        # refresh may replace the busy timestamp and nothing else. Seq stays,
        # and the floor file is not created.
        if flag == "refresh":
            self.files["t7.json"] = json.dumps(loaded).encode()
            self.state = loaded
            return
        try:
            seq = int(loaded.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq < self.floor:
            loaded["seq"] = self.floor
        elif self.floor == 0 and seq == 0:
            loaded["seq"] = int(now)
        self.files["t7.json"] = json.dumps(loaded).encode()
        written = int(loaded.get("seq") or 0)
        if written > self.floor:
            self.floor = written
            self.files["t7.json.seq"] = str(written).encode()
        elif "t7.json.seq" not in self.files and written:
            self.files["t7.json.seq"] = str(written).encode()
            self.floor = written
        self.state = loaded


def _read_pty(master):
    chunks = []
    while True:
        ready, _, _ = select.select([master], [], [], 0.3)
        if not ready:
            break
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


class _WriteTracer:
    """Fail a hook write unless commit_pane is applying it."""

    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def __init__(self):
        self.violations = []

    def __enter__(self):
        self._real_remove = os.remove
        self._real_unlink = os.unlink
        self._real_open = os.open
        self._real_write = os.write
        self._real_replace = os.replace
        self._real_rename = os.rename
        self._real_makedirs = os.makedirs
        self._real_mkdir = os.mkdir
        self._builtin_open = open

        def traced_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                self._note("open", file)
            return self._builtin_open(file, mode, *args, **kwargs)

        self._traced_open = traced_open
        self._bind(
            os,
            (
                ("remove", self._remove),
                ("unlink", self._unlink),
                ("open", self._os_open),
                ("write", self._os_write),
                ("replace", self._replace),
                ("rename", self._rename),
                ("makedirs", self._makedirs),
                ("mkdir", self._mkdir),
            ),
        )
        self._bind(builtins, (("open", traced_open),))
        return self

    def __exit__(self, *_exc):
        self._bind(
            os,
            (
                ("remove", self._real_remove),
                ("unlink", self._real_unlink),
                ("open", self._real_open),
                ("write", self._real_write),
                ("replace", self._real_replace),
                ("rename", self._real_rename),
                ("makedirs", self._real_makedirs),
                ("mkdir", self._real_mkdir),
            ),
        )
        self._bind(builtins, (("open", self._builtin_open),))
        return False

    @staticmethod
    def _bind(target, pairs):
        for name, value in pairs:
            setattr(target, name, value)

    def _note(self, kind, target):
        stack = traceback.extract_stack()
        if not any(frame.filename.endswith("herdr_kiro_state.py") for frame in stack):
            return
        depth = getattr(hook, "_WRITE_DEPTH", 0)
        applying = depth > 0 and any(frame.name == "commit_pane" for frame in stack)
        if applying:
            return
        where = " <- ".join(frame.name for frame in stack[-8:])
        self.violations.append(f"{kind} {target} via {where}")

    def _remove(self, path):
        self._note("remove", path)
        return self._real_remove(path)

    def _unlink(self, path):
        self._note("unlink", path)
        return self._real_unlink(path)

    def _os_open(self, path, flags, *args, **kwargs):
        if flags & self._WRITE_FLAGS and path != "/dev/null":
            self._note("os.open", path)
        return self._real_open(path, flags, *args, **kwargs)

    def _os_write(self, fd, data):
        self._note("os.write", fd)
        return self._real_write(fd, data)

    def _replace(self, src, dst):
        self._note("replace", dst)
        return self._real_replace(src, dst)

    def _rename(self, src, dst):
        self._note("rename", dst)
        return self._real_rename(src, dst)

    def _makedirs(self, path, *args, **kwargs):
        if not os.path.isdir(path):
            self._note("makedirs", path)
        return self._real_makedirs(path, *args, **kwargs)

    def _mkdir(self, path, *args, **kwargs):
        if not os.path.isdir(path):
            self._note("mkdir", path)
        return self._real_mkdir(path, *args, **kwargs)


class ScreenReadPurityTests(unittest.TestCase):
    """Classifiers, watch_loop, and main write only while commit_pane applies."""

    def _quiet(self):
        return "› ask a question or describe a task\n"

    def _reset_explain(self):
        hook._EXPLAIN_ENV = None
        hook._SCREEN_CACHE.clear()
        hook._RULES_DIR = None

    def _state_root(self):
        root = tempfile.mkdtemp(prefix="herdr-kiro-purity-")
        self.addCleanup(shutil.rmtree, root, True)
        state = os.path.join(root, "herdr-kiro")
        os.makedirs(os.path.join(state, "pids"), mode=0o700)
        return root, state

    def _plant(self, state):
        dead = subprocess.Popen(["sleep", "30"])
        dead.kill()
        dead.wait(timeout=5)
        files = {
            str(dead.pid): f"{os.getuid()}\nnot-the-live-start\nstale-comm\n",
            str(os.getpid()): f"{os.getuid()}\n1700000000.999999\nnot-the-comm\n",
        }
        for name, text in files.items():
            with open(os.path.join(state, "pids", name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return files

    def _snapshot(self, state):
        found = {}
        for dirpath, _dirs, names in os.walk(state):
            for name in names:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    found[os.path.relpath(path, state)] = handle.read()
        return found

    def test_classifiers_watch_loop_and_main_do_not_write_outside_commit_pane(self):
        root, state = self._state_root()
        self._plant(state)
        before = self._snapshot(state)
        quiet = self._quiet()
        self._reset_explain()
        env = {
            "XDG_STATE_HOME": root,
            "HERDR_KIRO_LOG": os.path.join(state, "hook.log"),
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HC_TERMINAL_ID": "t7",
            "HERDR_BIN_PATH": os.path.join(root, "missing-herdr"),
        }
        violations = []
        with mock.patch.dict(os.environ, env, clear=False):
            with _WriteTracer() as tracer:
                hook.explain(quiet)
                hook.explain(hook.ScreenSnapshot(None, None))
                hook.screen_is_settled(quiet)
                hook.screen_is_settled(hook.ScreenSnapshot(None, None))
                hook.screen_is_quiet(quiet)
                hook.report_for(quiet, "working")
                hook.report_for(quiet, "idle")
                hook.report_for(hook.ScreenSnapshot(None, None), "working")
                hook.blocker_visible(quiet)
                hook.blocker_visible(hook.ScreenSnapshot(None, None))
                hook.prompt_visible(quiet)
                hook.workflow_banner_from(quiet)
                hook.approval_text(quiet)
            violations.extend(tracer.violations)
            after_classifiers = self._snapshot(state)
            self._reset_explain()
            pane = hook.state_path("t7")
            weak = hook.fresh_state()
            weak["phase"] = "weak"
            weak["aggregate"] = 0
            weak["busy"] = {hook.AGGREGATE: 10.0}
            weak["owners"] = {hook.AGGREGATE: os.getpid()}
            weak["owner_ident"] = {hook.AGGREGATE: "sec:10"}
            weak["generation"] = 3
            weak["seq"] = 4
            with open(pane, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(weak))
            before_watch = self._snapshot(state)

            ticks = {"n": 0}

            def wait(_self, _pids, _timeout, ticks=ticks):
                ticks["n"] += 1
                if ticks["n"] > 1:
                    raise SystemExit
                return False

            def screen(_pane):
                return quiet

            with (
                _WriteTracer() as tracer,
                mock.patch.object(hook.OwnerExitWatch, "wait", wait),
                mock.patch.object(hook, "pane_text", screen),
                mock.patch.object(hook.time, "sleep", lambda _delay: None),
                contextlib.suppress(SystemExit),
            ):
                hook.watch_loop("t7", pane)
            violations.extend(tracer.violations)
            after_watch = self._snapshot(state)
            self._reset_explain()
            # os.getpid() has no /proc start ticks on macOS, so its token is
            # sec: and unproven. Refresh needs a live owner with a fine token.
            # The pin applies only while this process is still running.
            owner = subprocess.Popen(
                ["sleep", "30"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(lambda: owner.poll() is not None or owner.kill())
            start = "1700000000.111111"
            pins = {owner.pid: (start, owner)}
            real_hires = hook.hires_start

            def hires(asked, real_hires=real_hires, pins=pins):
                return pinned_start(real_hires, pins, asked)

            proven = hook.fresh_state()
            proven["phase"] = "proven"
            proven["aggregate"] = 0
            proven["busy"] = {hook.AGGREGATE: 10.0}
            proven["owners"] = {hook.AGGREGATE: owner.pid}
            proven["owner_ident"] = {hook.AGGREGATE: start}
            proven["proven_pid"] = owner.pid
            proven["proven_ident"] = start
            proven["generation"] = 3
            proven["seq"] = 4
            with open(pane, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(proven))
            before_hook = self._snapshot(state)
            with (
                _WriteTracer() as tracer,
                mock.patch.object(hook, "hires_start", side_effect=hires),
                mock.patch.object(sys, "argv", ["herdr-kiro", "UserPromptSubmit"]),
                mock.patch.object(sys, "stdin", io.StringIO("{}")),
                mock.patch.object(hook, "pane_text", screen),
                mock.patch.object(hook, "kiro_lineage", lambda: (owner.pid, start, 0, "")),
            ):
                self.assertEqual(hook.main(), 0)
            violations.extend(tracer.violations)
            after_hook = self._snapshot(state)
        problems = []
        if after_classifiers != before:
            problems.append(f"classifiers changed the state directory: {sorted(set(before) | set(after_classifiers))}")
        if after_watch != before_watch:
            problems.append("watch none tick changed the state directory")
        refresh_names = sorted(set(before_hook) | set(after_hook))
        changed = [name for name in refresh_names if before_hook.get(name) != after_hook.get(name)]
        if changed != ["t7.json"]:
            problems.append(f"refresh changed {changed}")
        else:
            saved = json.loads(after_hook["t7.json"])
            original = json.loads(before_hook["t7.json"])
            if saved["busy"][hook.AGGREGATE] == original["busy"][hook.AGGREGATE]:
                problems.append("refresh did not move the busy timestamp")
            else:
                saved["busy"] = original["busy"]
                if saved != original:
                    problems.append("refresh changed more than the busy timestamp")
        if violations:
            problems.append(f"{len(violations)} writes outside commit_pane:\n" + "\n".join(violations[:20]))
        if problems:
            self.fail("\n".join(problems))


class NoneReadWriteTests(unittest.TestCase):
    """Failed App reads and unconfirmed holds write nothing.

    A None pane_text is not a prompt, a banner, aggregate_zero, or refresh.
    A later None tick does not paint a held blocker or banner. A fault that
    returns none does not unlink a pid file.
    """

    _BANNER = "▶ watch-step running 0/0 · ctrl+x expand · ctrl+g monitor\n"
    _BLOCKED = "esc to close · ↑↓ to navigate · ↵ to select\n"
    _PINNED_OWNER = "1700000000.111111"

    def _snapshot(self, root):
        found = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    found[os.path.relpath(path, root)] = handle.read()
        return found

    def _owner(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        return master, proc

    def _prepare(self, proc, count):
        root = tempfile.mkdtemp(prefix="herdr-kiro-none-")
        self.addCleanup(shutil.rmtree, root, True)
        state = os.path.join(root, "herdr-kiro")
        os.makedirs(os.path.join(state, "pids"), mode=0o700)
        # macOS has no /proc start ticks, so this sleep is sec: or empty.
        # The pin applies only while the process is still running.
        real_hires = hook.hires_start
        token = real_hires(proc.pid)
        if not hook.fine_start(token):
            token = self._PINNED_OWNER
            pins = {proc.pid: (token, proc)}

            def hires(asked, real_hires=real_hires, pins=pins):
                return pinned_start(real_hires, pins, asked)

            patch = mock.patch.object(hook, "hires_start", side_effect=hires)
            patch.start()
            self.addCleanup(patch.stop)
        self.assertTrue(hook.fine_start(token))
        pane = hook.fresh_state()
        pane["seq"] = 4
        pane["aggregate"] = count
        pane["busy"] = {hook.AGGREGATE: 1000.0, "old": 900.0}
        pane["grace_until"] = 3.5
        pane["generation"] = 7
        pane["phase"] = "proven"
        pane["owners"] = {hook.AGGREGATE: proc.pid}
        pane["owner_ident"] = {hook.AGGREGATE: token}
        pane["proven_pid"] = proc.pid
        pane["proven_ident"] = token
        pane["watcher"] = os.getpid()
        pane["watcher_started"] = hook.hires_start(os.getpid())
        path = os.path.join(state, "t7.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(pane))
        with open(path + ".seq", "w", encoding="utf-8") as handle:
            handle.write("50")
        stale = f"{os.getuid()}\nnot-the-live-start\nstale-comm\n".encode()
        with open(os.path.join(state, "pids", "424242"), "wb") as handle:
            handle.write(stale)
        own = f"{os.getuid()}\n{pane['watcher_started']}\npython\n".encode()
        with open(os.path.join(state, "pids", str(os.getpid())), "wb") as handle:
            handle.write(own)
        return root, state, path, token

    def _env(self, root):
        return {
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HC_TERMINAL_ID": "t7",
            "XDG_STATE_HOME": root,
            "HERDR_KIRO_LOG": os.path.join(root, "herdr-kiro", "hook.log"),
            "HERDR_BIN_PATH": os.path.join(root, "missing-herdr"),
        }

    def _watch(self, root, path, screens):
        pending = list(screens)
        previous_hup = signal.getsignal(signal.SIGHUP)
        previous_term = signal.getsignal(signal.SIGTERM)

        def screen(_pane, pending=pending):
            if not pending:
                raise SystemExit("done")
            return pending.pop(0)

        def wait(_self, _pids, _timeout, pending=pending):
            if not pending:
                raise SystemExit("done")
            return False

        try:
            with (
                mock.patch.dict(os.environ, self._env(root), clear=False),
                mock.patch.object(hook, "pane_text", screen),
                mock.patch.object(hook.OwnerExitWatch, "wait", wait),
                mock.patch.object(hook.time, "sleep", lambda _delay: None),
            ):
                os.environ.pop("HERDR_SESSION", None)
                hook.begin_tick()
                try:
                    hook.watch_loop("t7", path)
                except SystemExit:
                    return
        finally:
            signal.signal(signal.SIGHUP, previous_hup)
            signal.signal(signal.SIGTERM, previous_term)

    def _diff(self, before, after):
        names = sorted(set(before) | set(after))
        lines = []
        for name in names:
            if before.get(name) != after.get(name):
                lines.append(f"{name}: {before.get(name)!r:.120} -> {after.get(name)!r:.120}")
        return "\n".join(lines)

    def test_none_read_at_count_one_does_not_paint_working(self):
        master, proc = self._owner()
        root, state, path, _token = self._prepare(proc, 1)
        before = self._snapshot(state)
        with _WriteTracer() as tracer:
            self._watch(root, path, [None])
        after = self._snapshot(state)
        osc = _read_pty(master)
        stray = [item for item in tracer.violations if "_open_held" not in item and "_hold_live_owner" not in item]
        self.assertEqual(stray, [])
        self.assertEqual(osc, b"", osc)
        self.assertEqual(after, before, self._diff(before, after))

    def test_none_read_at_count_zero_is_not_refresh(self):
        master, proc = self._owner()
        root, state, path, _token = self._prepare(proc, 0)
        before = self._snapshot(state)
        with _WriteTracer() as tracer:
            self._watch(root, path, [None])
        after = self._snapshot(state)
        osc = _read_pty(master)
        stray = [item for item in tracer.violations if "_open_held" not in item and "_hold_live_owner" not in item]
        self.assertEqual(stray, [])
        self.assertEqual(osc, b"", osc)
        self.assertEqual(after, before, self._diff(before, after))

    def test_later_none_tick_does_not_paint_held_blocked(self):
        master, proc = self._owner()
        root, _state, path, _token = self._prepare(proc, 1)
        self._watch(root, path, [self._BLOCKED, None])
        osc = _read_pty(master)
        self.assertEqual(osc.count(b"state=blocked"), 1, osc)
        self.assertNotIn(b"state=working", osc)

    def test_later_none_tick_does_not_paint_held_banner(self):
        master, proc = self._owner()
        root, _state, path, _token = self._prepare(proc, 1)
        self._watch(root, path, [self._BANNER, None])
        osc = _read_pty(master)
        self.assertEqual(osc.count(b"state=working"), 1, osc)
        self.assertNotIn(b"state=blocked", osc)

    def test_unconfirmed_prompt_does_not_touch_aggregate_or_paint_blocked(self):
        master, proc = self._owner()
        root, state, _path, token = self._prepare(proc, 1)
        before = self._snapshot(state)
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        _held_master, held_slave = os.openpty()
        self.addCleanup(os.close, _held_master)
        self.addCleanup(os.close, held_slave)
        hook._pane_fd = os.dup(held_slave)
        hook._held_ident = token
        hook._held_pid = 0

        def screen(_pane):
            return self._BLOCKED

        with (
            mock.patch.dict(os.environ, self._env(root), clear=False),
            mock.patch.object(sys, "argv", ["herdr-kiro", "UserPromptSubmit"]),
            mock.patch.object(sys, "stdin", io.StringIO("{}")),
            mock.patch.object(hook, "pane_text", screen),
            mock.patch.object(hook, "kiro_lineage", lambda: (proc.pid, token, 0, "")),
        ):
            os.environ.pop("HERDR_SESSION", None)
            hook.begin_tick()
            self.assertEqual(hook.main(), 0)
        after = self._snapshot(state)
        osc = _read_pty(master)
        self.assertEqual(after, before, self._diff(before, after))
        self.assertEqual(osc, b"", osc)

    def test_fault_none_does_not_unlink_the_pidfile(self):
        _master, proc = self._owner()
        root, state, path, _token = self._prepare(proc, 1)
        pid_name = f"pids/{os.getpid()}"
        stale_name = "pids/424242"
        before = self._snapshot(state)
        with mock.patch.dict(os.environ, self._env(root), clear=False):
            write, ok = hook.commit_pane(
                hook.fresh_state(),
                "watcher_fault",
                path=path,
                pane="t7",
                fault=RuntimeError("boom"),
            )
        after = self._snapshot(state)
        self.assertEqual((write, ok), ("none", False))
        self.assertEqual(after.get(pid_name), before[pid_name])
        self.assertEqual(after.get(stale_name), before[stale_name])

    def _hold(self, token, held_pid):
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        _master, slave = os.openpty()
        self.addCleanup(os.close, _master)
        self.addCleanup(os.close, slave)
        hook._pane_fd = os.dup(slave)
        hook._held_ident = token
        hook._held_pid = held_pid

    def _main(self, root, proc, token, event, payload="{}"):
        with (
            mock.patch.dict(os.environ, self._env(root), clear=False),
            mock.patch.object(sys, "argv", ["herdr-kiro", event]),
            mock.patch.object(sys, "stdin", io.StringIO(payload)),
            mock.patch.object(hook, "pane_text", lambda _pane: "still writing the reply\n"),
            mock.patch.object(hook, "kiro_lineage", lambda: (proc.pid, token, 0, "")),
            _WriteTracer() as tracer,
        ):
            os.environ.pop("HERDR_SESSION", None)
            hook.begin_tick()
            self.assertEqual(hook.main(), 0)
        return tracer

    def test_unconfirmed_partial_stop_writes_nothing(self):
        _master, proc = self._owner()
        other = subprocess.Popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: other.poll() is not None or other.kill())
        for event in ("Stop", "SessionEnd"):
            for held_pid in (0, other.pid):
                with self.subTest(event=event, held_pid=held_pid):
                    root, state, _path, token = self._prepare(proc, 2)
                    before = self._snapshot(state)
                    self._hold(token, held_pid)
                    tracer = self._main(root, proc, token, event)
                    after = self._snapshot(state)
                    self.assertEqual(tracer.violations, [])
                    self.assertEqual(after, before, self._diff(before, after))

    def test_unconfirmed_aggregate_zero_does_not_refresh(self):
        _master, proc = self._owner()
        for event in ("Stop", "UserPromptSubmit"):
            with self.subTest(event=event):
                root, state, _path, token = self._prepare(proc, 0)
                before = self._snapshot(state)
                self._hold(token, 0)
                tracer = self._main(root, proc, token, event)
                after = self._snapshot(state)
                self.assertEqual(tracer.violations, [])
                self.assertEqual(after, before, self._diff(before, after))

    def test_unconfirmed_named_prompt_does_not_record_or_refresh(self):
        _master, proc = self._owner()
        root, state, _path, token = self._prepare(proc, 0)
        before = self._snapshot(state)
        self._hold(token, 0)
        tracer = self._main(
            root,
            proc,
            token,
            "UserPromptSubmit",
            json.dumps({"session_id": "sess"}),
        )
        after = self._snapshot(state)
        self.assertEqual(tracer.violations, [])
        self.assertEqual(after, before, self._diff(before, after))

    def test_proven_named_prompt_refreshes_only_the_busy_timestamp(self):
        _master, proc = self._owner()
        root, state, _path, token = self._prepare(proc, 0)
        hook._close_held_fd()
        self.addCleanup(hook._close_held_fd)
        before = self._snapshot(state)
        tracer = self._main(
            root,
            proc,
            token,
            "UserPromptSubmit",
            json.dumps({"session_id": "sess"}),
        )
        after = self._snapshot(state)
        self.assertEqual(tracer.violations, [])
        names = sorted(set(before) | set(after))
        changed = [name for name in names if before.get(name) != after.get(name)]
        self.assertEqual(changed, ["t7.json"], self._diff(before, after))
        saved = json.loads(after["t7.json"])
        original = json.loads(before["t7.json"])
        self.assertNotEqual(saved["busy"][hook.AGGREGATE], original["busy"][hook.AGGREGATE])
        self.assertNotIn("sess", saved["busy"])
        self.assertNotIn("sess", saved["owners"])
        saved["busy"][hook.AGGREGATE] = original["busy"][hook.AGGREGATE]
        self.assertEqual(saved, original)

    def test_fault_none_returns_before_any_io(self):
        _master, proc = self._owner()
        root, state, path, _token = self._prepare(proc, 1)
        before = self._snapshot(state)
        with (
            mock.patch.dict(os.environ, self._env(root), clear=False),
            _WriteTracer() as tracer,
        ):
            write, ok = hook.commit_pane(
                hook.fresh_state(),
                "watcher_fault",
                path=path,
                pane="t7",
                fault=RuntimeError("boom"),
            )
        after = self._snapshot(state)
        self.assertEqual((write, ok), ("none", False))
        self.assertEqual(tracer.violations, [])
        self.assertEqual(after, before, self._diff(before, after))

    def _fine_token(self, proc):
        real = hook.hires_start
        token = real(proc.pid)
        if hook.fine_start(token):
            return token
        token = self._PINNED_OWNER
        pins = {proc.pid: (token, proc)}

        def hires(asked, real_hires=real, pins=pins):
            return pinned_start(real_hires, pins, asked)

        patch = mock.patch.object(hook, "hires_start", side_effect=hires)
        patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(hook.fine_start(token))
        return token

    def _plant_phase(self, proc, phase, token, floor, watcher):
        root = tempfile.mkdtemp(prefix="herdr-kiro-phase-")
        self.addCleanup(shutil.rmtree, root, True)
        state = os.path.join(root, "herdr-kiro")
        os.makedirs(os.path.join(state, "pids"), mode=0o700)
        pane = hook.fresh_state()
        pane["seq"] = 4
        pane["aggregate"] = 2
        pane["busy"] = {hook.AGGREGATE: 1000.0}
        pane["grace_until"] = 3.5
        pane["generation"] = 7
        pane["phase"] = phase
        pane["owners"] = {hook.AGGREGATE: proc.pid}
        pane["owner_ident"] = {hook.AGGREGATE: token}
        if phase != "weak":
            pane["proven_pid"] = proc.pid
            pane["proven_ident"] = token
        pane["watcher"] = watcher
        if watcher:
            pane["watcher_started"] = hook.hires_start(watcher)
        path = os.path.join(state, "t7.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(pane))
        if floor == "ahead":
            with open(path + ".seq", "w", encoding="utf-8") as handle:
                handle.write("50")
        stale = f"{os.getuid()}\nnot-the-live-start\nstale-comm\n".encode()
        with open(os.path.join(state, "pids", "424242"), "wb") as handle:
            handle.write(stale)
        if watcher:
            own = f"{os.getuid()}\n{pane['watcher_started']}\npython\n".encode()
            with open(os.path.join(state, "pids", str(watcher)), "wb") as handle:
                handle.write(own)
        hook.begin_tick()
        self.assertEqual(hook.owner_phase(pane), phase)
        return root, state

    def _quiet_main(self, root, proc, token, event):
        spawned = []
        real_popen = subprocess.Popen

        def popen(args, *pos, **kwargs):
            command = args if isinstance(args, (list, tuple)) else [str(args)]
            if any(part == "--watch" for part in command):
                spawned.append(list(command))
                proc_stub = mock.Mock()
                proc_stub.pid = 515151
                return proc_stub
            return real_popen(args, *pos, **kwargs)

        with mock.patch.object(hook.subprocess, "Popen", popen):
            tracer = self._main(root, proc, token, event)
        return tracer, spawned

    def test_unconfirmed_hold_writes_nothing_for_idle_clear_weak_and_exited(self):
        _master, proc = self._owner()
        fine = self._fine_token(proc)
        phases = {"idle_clear": fine, "weak": "sec:10", "exited": fine}
        events = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionStart")
        cases = [(phase, event, floor, 0) for phase in phases for event in events for floor in ("ahead", "absent")]
        cases.extend((phase, "UserPromptSubmit", "absent", os.getpid()) for phase in phases)
        for phase, event, floor, watcher in cases:
            with self.subTest(phase=phase, event=event, floor=floor, watcher=watcher):
                root, state = self._plant_phase(proc, phase, phases[phase], floor, watcher)
                before = self._snapshot(state)
                self._hold(phases[phase], 0)
                tracer, spawned = self._quiet_main(root, proc, phases[phase], event)
                after = self._snapshot(state)
                problems = []
                if after != before:
                    problems.append(self._diff(before, after))
                if spawned:
                    problems.append("watcher started")
                if tracer.violations:
                    problems.append("\n".join(tracer.violations[:8]))
                self.assertEqual(problems, [], "\n".join(problems))

    def test_refuse_watch_returns_none_before_any_io(self):
        root = tempfile.mkdtemp(prefix="herdr-kiro-refuse-")
        self.addCleanup(shutil.rmtree, root, True)
        state = os.path.join(root, "herdr-kiro")
        os.makedirs(state, mode=0o700)
        before = self._snapshot(state)
        with (
            mock.patch.dict(os.environ, self._env(root), clear=False),
            mock.patch.object(sys, "argv", ["herdr-kiro", "--watch", "t7", os.path.join(root, "nope.json")]),
            mock.patch.object(hook, "_run_watch", side_effect=AssertionError("watch")),
            _WriteTracer() as tracer,
        ):
            os.environ.pop("HERDR_SESSION", None)
            code = hook.main()
        after = self._snapshot(state)
        self.assertEqual(code, 1)
        self.assertEqual(tracer.violations, [])
        self.assertEqual(after, before, self._diff(before, after))
        write, ok = hook.commit_pane(hook.fresh_state(), "refuse_watch", pane="t7", path=os.path.join(state, "t7.json"))
        self.assertEqual((write, ok), ("none", False))
        self.assertEqual(self._snapshot(state), before, self._diff(before, self._snapshot(state)))

    def test_watch_result_runs_the_watcher_and_is_not_none(self):
        ran = []

        def spy(pane, path):
            ran.append((pane, path))

        with mock.patch.object(hook, "_run_watch", spy):
            write, ok = hook.commit_pane(hook.fresh_state(), "watch", path="pane.json", pane="t7", watch=True)
        self.assertEqual(ran, [("t7", "pane.json")])
        self.assertEqual((write, ok), ("watch", True))


class KiroThreeOneLineageTests(unittest.TestCase):
    """Kiro CLI 3.1 runs each hook under node <- bun <- kiro-cli.

    There is no kiro-cli-chat. Where /proc start ticks are absent, as on
    macOS, the outer kiro-cli start is sec: plus a whole-second lstart.
    """

    _NODE = 990003
    _BUN = 990002
    _ZSH = 990001
    _PROMPT = "› ask a question or describe a task\n"

    def _kiro(self):
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        chain = {
            self._NODE: {"ppid": self._BUN, "lstart": "1794361689", "comm": "node"},
            self._BUN: {"ppid": proc.pid, "lstart": "1794361689", "comm": "bun"},
            proc.pid: {"ppid": self._ZSH, "lstart": "1794361688", "comm": "kiro-cli"},
            self._ZSH: {"ppid": 1, "lstart": "1794361688", "comm": "zsh"},
        }
        root = tempfile.mkdtemp(prefix="herdr-kiro-31-")
        self.addCleanup(shutil.rmtree, root, True)
        return proc, chain, root

    def _main(self, root, chain, event):
        payload = json.dumps({"cwd": root, "hook_event_name": event, "session_id": "sess_31"})
        env = {
            "HERDR_ENV": "",
            "HERDR_PANE_ID": "",
            "HC_TERMINAL_ID": "t5",
            "XDG_STATE_HOME": root,
            "HERDR_KIRO_LOG": os.path.join(root, "hook.log"),
            "HERDR_BIN_PATH": os.path.join(root, "missing-herdr"),
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(sys, "argv", ["herdr-kiro", event]),
            mock.patch.object(sys, "stdin", io.StringIO(payload)),
            mock.patch.object(hook.os, "getppid", return_value=self._NODE),
            mock.patch.object(hook, "process_snapshot", lambda pid: chain.get(int(pid))),
            mock.patch.object(hook, "_linux_start_ticks", return_value=None),
            mock.patch.object(hook, "ensure_watcher", lambda *_args: None),
            mock.patch.object(hook, "pane_text", lambda _pane: self._PROMPT),
            mock.patch.object(hook, "screen_view", lambda _text: hook._unknown_screen()),
            mock.patch.object(hook, "herdr", lambda *_args: True),
        ):
            hook.begin_tick()
            self.assertEqual(hook.main(), 0)

    def _saved(self, root):
        with open(os.path.join(root, "herdr-kiro", "t5.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_lineage_has_an_outer_and_no_chat_child(self):
        proc, chain, _root = self._kiro()
        with (
            mock.patch.object(hook.os, "getppid", return_value=self._NODE),
            mock.patch.object(hook, "process_snapshot", lambda pid: chain.get(int(pid))),
        ):
            self.assertEqual(hook.kiro_lineage(), (proc.pid, "1794361688", 0, ""))

    def test_stop_ends_the_turn_when_the_outer_start_is_whole_seconds(self):
        _proc, chain, root = self._kiro()
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self._main(root, chain, event)
        self.assertIn("sess_31", self._saved(root)["busy"])
        self._main(root, chain, "Stop")
        saved = self._saved(root)
        self.assertNotIn("sess_31", saved["busy"], saved)
        with open(os.path.join(root, "hook.log"), encoding="utf-8") as handle:
            self.assertIn("event=Stop", handle.read())

    def test_compaction_events_do_not_touch_the_pane(self):
        _proc, chain, root = self._kiro()
        for event in ("SessionStart", "UserPromptSubmit"):
            self._main(root, chain, event)
        before = self._saved(root)
        for event in ("PreCompact", "PostCompact"):
            self._main(root, chain, event)
        self.assertEqual(self._saved(root), before)


if __name__ == "__main__":
    unittest.main()
