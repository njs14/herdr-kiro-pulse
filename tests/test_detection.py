"""The local kiro.toml override marks a workflow banner working and a chooser blocked."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, "prototype", "kiro-detection.toml")


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        config = os.path.join(self.base, "herdr", "agent-detection")
        os.makedirs(config)
        shutil.copy(RULES, os.path.join(config, "kiro.toml"))
        self.env = dict(os.environ)
        self.env["XDG_CONFIG_HOME"] = self.base

    def explain(self, text):
        path = os.path.join(self.base, "screen.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        completed = subprocess.run(
            ["herdr", "agent", "explain", "--file", path, "--agent", "kiro", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = json.loads(completed.stdout)
        return data.get("state"), (data.get("matched_rule") or {}).get("id")

    def test_slash_watch_banner_is_working(self):
        state, rule = self.explain(
            "▶ watch-step running 0/0 · ctrl+x expand · ctrl+g monitor\n"
            "\n"
            "› ask a question or describe a task\n"
        )
        self.assertEqual(state, "working")
        self.assertEqual(rule, "workflow_banner_working")

    def test_banner_with_chooser_stays_blocked(self):
        state, rule = self.explain(
            "▶ two-step running 1/2 · ctrl+x expand · ctrl+g monitor\n"
            "❯ Allow once\n"
            "  Deny once\n"
            "esc to close · ↑↓ to navigate · ↵ to select · Tab to edit\n"
        )
        self.assertEqual(state, "blocked")
        self.assertEqual(rule, "workflow_chooser_blocked")

    def test_model_phrases_do_not_hide_the_prompt(self):
        state, _rule = self.explain(
            "I will always allow this.\n"
            "You can press esc to close the panel.\n"
            "› ask a question or describe a task\n"
        )
        self.assertEqual(state, "idle")

    def test_model_status_line_is_not_the_banner(self):
        state, rule = self.explain(
            "Status: ▶ deploy running\n"
            "› ask a question or describe a task\n"
        )
        self.assertEqual(state, "idle")
        self.assertNotEqual(rule, "workflow_banner_working")

    def test_question_panel_beats_the_banner(self):
        state, _rule = self.explain(
            "▶ two-step running 1/2 · ctrl+x expand · ctrl+g monitor\n"
            "Pick one\n"
            "esc to cancel · ↑↓ to navigate · enter to submit\n"
        )
        self.assertEqual(state, "blocked")

    def test_trust_all_beats_the_banner(self):
        state, rule = self.explain(
            "▶ solo running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "❯ No, exit\n"
            "  Yes, I accept\n"
            "esc to cancel · ↑↓ to navigate · ↵ to select\n"
        )
        self.assertEqual(state, "blocked")
        self.assertEqual(rule, "workflow_trust_all")

    def test_more_options_footer_beats_the_banner(self):
        state, rule = self.explain(
            "▶ two-step running 1/2 · ctrl+x expand · ctrl+g monitor\n"
            "❯ Allow\n"
            "esc to close · enter to see more options\n"
        )
        self.assertEqual(state, "blocked")
        self.assertEqual(rule, "workflow_approval_more_options")

    def test_subagent_widget_beats_the_banner(self):
        state, rule = self.explain(
            " ▶ approve-probe running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            " ⚠ 1 tool approval pending\n"
            " ❯ (a) Approve all pending\n"
            "   (f) Approve all pending and auto-approve future subagent requests\n"
            "   (c) Configure individually (agent monitor)\n"
            "   (x) Exit (cancel subagents)\n"
        )
        self.assertEqual(state, "blocked")
        self.assertEqual(rule, "crew_approval")

    def test_truncated_banner_is_working(self):
        full = "▶ parallel-steps running 0/2 · ctrl+x expand · ctrl+g monitor"
        state, rule = self.explain(full[:54] + "\n› ask a question or describe a task\n")
        self.assertEqual((state, rule), ("working", "workflow_banner_working"))

    def test_repaint_gap_stays_working_while_the_banner_is_up(self):
        # The idle prompt is on the bottom lines. The banner is still on screen.
        state, rule = self.explain(
            "▶ parallel-steps running 1/2 · ctrl+x expand · ctrl+g monitor\n"
            "Kiro is working\n"
            "› ask a question or describe a task\n"
        )
        self.assertEqual((state, rule), ("working", "workflow_banner_working"))


if __name__ == "__main__":
    unittest.main()
