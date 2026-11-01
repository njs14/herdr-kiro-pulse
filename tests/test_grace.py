"""Grace rules for the Kiro hook.

Herdr drops a working report when a foreground turn repaints onto the idle
prompt, about 0.2s after Stop, and it keeps the old sequence. Re-sending
working after that drop is what holds the pane for the grace period.
"""
import os
import sys
import unittest

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

    def test_approval_text_is_recognized(self):
        self.assertTrue(hook.approval_text("esc to close\nAlways allow"))
        self.assertFalse(hook.approval_text("ask a question or describe a task"))

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


if __name__ == "__main__":
    unittest.main()
