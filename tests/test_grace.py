"""Grace rules for the Kiro hook.

Herdr drops a working report when a foreground turn repaints onto the idle
prompt, about 0.2s after Stop, and it keeps the old sequence. Re-sending
working after that drop is what holds the pane for the grace period.
"""
import json
import os
import shlex
import subprocess
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
        self.assertTrue(
            hook.workflow_banner_from(
                "▶ watch-step running 0/0 · ctrl+x expand · ctrl+g monitor\n"
            )
        )
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
        hook.update(path, lambda state: None)
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
            with mock.patch.object(hook, "pane_text", read), mock.patch.object(
                hook, "send_report", report
            ), mock.patch.object(hook, "release_authority", report), mock.patch.object(
                hook, "agent_lookup", lambda _pane: "idle"
            ), mock.patch.object(hook.time, "sleep", sleep):
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
        hook.update(path, lambda state: None)
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
            "import sys; sys.path.insert(0, %s); import herdr_kiro_state as hook; "
            "print(hook.session_of({}))"
        ) % repr(root)
        quoted = shlex.quote(code)

        def run():
            completed = subprocess.run(
                ["sh", "-c", "python3 -c " + quoted],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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

        with mock.patch.object(hook, "process_snapshot", snap):
            self.assertFalse(hook.alive(50, started="THEN"))
            self.assertTrue(hook.alive(50, started="NOW"))

    def test_release_authority_reports_success(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda state: None)
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

    def test_watcher_waits_before_a_missing_agent_ends_the_run(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "pane.json")
        hook.update(path, lambda state: None)
        clock = {"t": 1000.0}

        def sleep(_delay):
            clock["t"] += 0.5
            if clock["t"] > 1008:
                raise AssertionError("watcher did not exit")

        def now():
            return clock["t"]

        with mock.patch.object(hook, "pane_text", lambda _pane: "hello\n"), mock.patch.object(
            hook, "agent_lookup", lambda _pane: "gone"
        ), mock.patch.object(hook, "send_report", lambda *_args, **_kwargs: True), mock.patch.object(
            hook, "herdr", lambda *_args: True
        ), mock.patch.object(hook, "screen_view", lambda _text: {"state": "idle", "id": None, "ok": True}), mock.patch.object(
            hook.time, "sleep", sleep
        ), mock.patch.object(hook.time, "time", now):
            hook.watch_loop("w1:p9", path)
        self.assertGreaterEqual(clock["t"] - 1000.0, hook.GONE_CONFIRM_SECS)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["watcher"], 0)

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

        with mock.patch.object(hook, "alive", lambda *_args, **_kwargs: True), mock.patch.object(
            hook.subprocess, "Popen", popen
        ), mock.patch.object(
            hook, "process_snapshot", lambda _pid: {"ppid": 1, "lstart": "9", "comm": "python"}
        ):
            hook.ensure_watcher("w1:p1", path)
        self.assertTrue(spawned.get("yes"))

    def test_owner_death_is_visible_after_normalize(self):
        state = hook.apply_event(
            hook.fresh_state(), "UserPromptSubmit", "sess", 1.0, owner=424242, owner_started="old"
        )

        def gone(_pid):
            return None

        with mock.patch.object(hook, "process_snapshot", gone):
            later = hook.normalize(state, 10)
        self.assertEqual(later["busy"], {})
        self.assertTrue(later["owners_died"])
        self.assertTrue(hook.should_stop_for_dead_owner(later, False))
        self.assertFalse(hook.should_stop_for_dead_owner(later, True))

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

        def run(cmd, **kwargs):
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

        with mock.patch.object(hook.os, "getppid", return_value=10), mock.patch.object(
            hook, "process_snapshot", snap
        ):
            self.assertEqual(hook.kiro_ancestor(), (9, "outer"))


if __name__ == "__main__":
    unittest.main()
