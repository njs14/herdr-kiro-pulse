"""Grace rules for the Kiro hook.

Herdr drops a working report when a foreground turn repaints onto the idle
prompt, about 0.2s after Stop, and it keeps the old sequence. Re-sending
working after that drop is what holds the pane for the grace period.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prototype"))

import herdr_kiro_state as hook


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
        self.assertEqual(effective(reports, drop_at, 2.0), "working")
        self.assertEqual(effective(reports, drop_at, 3.4), "working")
        self.assertEqual(effective(reports, drop_at, 4.2), "idle")
        idle_at = next(stamped for stamped, kind in reports if kind == "idle")
        self.assertGreaterEqual(idle_at, 4.0)

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
            hook.next_action(len(state["busy"]), state["grace_until"], False, 6.9),
            "working",
        )
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 7.1),
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
            hook.next_action(len(quiet["busy"]), quiet["grace_until"], False, 22.9),
            "working",
        )
        self.assertEqual(
            hook.next_action(len(quiet["busy"]), quiet["grace_until"], False, 23.1),
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
        self.assertNotEqual(key, "unknown")
        self.assertEqual(key, hook.session_of({"cwd": "/work", "hook_event_name": "Stop"}, ppid=77))
        other = hook.session_of({"cwd": "/work"}, ppid=78)
        self.assertNotEqual(key, other)
        state = hook.fresh_state()
        state = hook.apply_event(state, "SessionStart", key, 1)
        state = hook.apply_event(state, "SessionStart", key, 1.1)
        state = hook.apply_event(state, "UserPromptSubmit", key, 1.2)
        self.assertEqual(len(state["busy"]), 2)
        state = hook.apply_event(state, "Stop", key, 3)
        self.assertEqual(len(state["busy"]), 1)
        self.assertEqual(
            hook.next_action(len(state["busy"]), state["grace_until"], False, 3.2),
            "working",
        )
        state = hook.apply_event(state, "Stop", key, 4)
        self.assertEqual(state["busy"], {})
        self.assertEqual(state["grace_until"], 4 + hook.SETTLE_SECS)

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
        hook.update(path, lambda state: None)
        reads = {"n": 0}

        def read(_pane):
            reads["n"] += 1
            return "› ask a question or describe a task\n"

        def sleep(_delay):
            if reads["n"] > 2:
                raise AssertionError("watcher kept polling after idle")

        def report(*_args):
            return True

        with (
            mock.patch.object(hook, "pane_text", read),
            mock.patch.object(hook, "send_report", report),
            mock.patch.object(hook.time, "sleep", sleep),
        ):
            hook.watch_loop("w1:p1", path)
        self.assertEqual(reads["n"], 1)
        self.assertEqual(json.load(open(path, encoding="utf-8"))["watcher"], 0)

        spawned = {}

        class Proc:
            pid = 4321

        def popen(*args, **kwargs):
            spawned["args"] = args
            return Proc()

        def mark_busy(state):
            state["busy"]["sess"] = time.time()

        hook.update(path, mark_busy)
        with mock.patch.object(hook.subprocess, "Popen", popen):
            hook.ensure_watcher("w1:p1", path)
        self.assertIn("args", spawned)
        self.assertEqual(json.load(open(path, encoding="utf-8"))["watcher"], 4321)

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
        hook.update(path, lambda state: None)
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
