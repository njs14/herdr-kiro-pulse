"""The local kiro.toml override marks a workflow banner working and a chooser blocked."""
import json
import os
import shutil
import subprocess
import tempfile
import unicodedata
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, "prototype", "kiro-detection.toml")
WIDTHS = (40, 54, 70, 80, 100, 133, 200)


def _column_width(char):
    # Box drawing is East Asian Ambiguous. A western terminal column is 1.
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def wrap_terminal(text, width):
    """Character wrap the way a terminal folds a line, not word wrap."""
    rows = []
    for line in text.splitlines():
        if not line:
            rows.append("")
            continue
        current = []
        column = 0
        for char in line:
            wide = _column_width(char)
            if column + wide > width and current:
                rows.append("".join(current))
                current = [char]
                column = wide
            else:
                current.append(char)
                column += wide
        rows.append("".join(current))
    if text.endswith("\n"):
        return "\n".join(rows) + "\n"
    return "\n".join(rows)


class DetectionTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("herdr") is None:
            self.skipTest("herdr is not on PATH")
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

    def test_workspace_trust_dialog_is_blocked(self):
        capture = os.path.join(ROOT, "logs", "workspace-trust.txt")
        with open(capture, encoding="utf-8") as handle:
            text = handle.read()
        state, rule = self.explain(text)
        self.assertEqual((state, rule), ("blocked", "workspace_trust"))

    def test_chooser_right_border_stays_blocked(self):
        state, rule = self.explain(
            "│ Write File requires approval\n"
            "│ ❯ Allow once  │\n"
            "│   Deny once  │\n"
        )
        self.assertEqual((state, rule), ("blocked", "workflow_requires_approval"))

    def test_chooser_scrollbar_glyph_stays_blocked(self):
        state, rule = self.explain(
            "│ Write File requires approval\n"
            "│ ❯ Allow once █\n"
            "│   Deny once █\n"
        )
        self.assertEqual((state, rule), ("blocked", "workflow_requires_approval"))

    def test_chooser_on_the_steps_row_stays_blocked(self):
        state, rule = self.explain(
            "Write File requires approval\n"
            "❯ └── ⚠ write needs approval │ ❯ Allow once\n"
            "                              │   Deny once\n"
        )
        self.assertEqual((state, rule), ("blocked", "workflow_requires_approval"))

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

    def test_monitor_border_chooser_beats_the_banner(self):
        state, rule = self.explain(
            "│ write-probe - running\n"
            "│ Write File requires approval\n"
            "│ fs_write -> note.txt\n"
            "│ ❯ Allow once\n"
            "│   Deny once\n"
            "│   Create allow rule\n"
        )
        self.assertEqual((state, rule), ("blocked", "workflow_requires_approval"))

    def test_workspace_trust_prose_does_not_block(self):
        state, rule = self.explain(
            "▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "The dialog says Trust, continue or Don't trust, exit.\n"
            "› ask a question or describe a task\n"
        )
        self.assertNotEqual(rule, "workspace_trust")
        self.assertEqual(state, "working")

    def test_bordered_prose_about_allow_once_does_not_block(self):
        state, rule = self.explain(
            "│ The notes say this step requires approval and Allow once is the default.\n"
            "│ Deny once would stop the write.\n"
            "› ask a question or describe a task\n"
        )
        self.assertNotEqual(rule, "workflow_requires_approval")

    def test_requires_approval_prose_does_not_block(self):
        state, rule = self.explain(
            "The step requires approval and the notes say Allow once is the default.\n"
            "› ask a question or describe a task\n"
        )
        self.assertNotEqual(rule, "workflow_requires_approval")
        self.assertEqual(state, "idle")

    def test_chooser_scrolled_off_the_bottom_does_not_block(self):
        filler = "\n".join(["older log line %s" % index for index in range(70)])
        state, rule = self.explain(
            "│ Write File requires approval\n"
            "│ ❯ Allow once\n"
            "│   Deny once\n"
            + filler
            + "\n› ask a question or describe a task\n"
        )
        self.assertNotEqual(rule, "workflow_requires_approval")

    def test_requires_approval_chooser_beats_the_banner(self):
        state, rule = self.explain(
            "▶ note running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            " Write File requires approval\n"
            " fs_write → note.txt\n"
            "\n"
            " ❯ Allow once\n"
            "   Deny once\n"
            "   Create allow rule to approve future similar actions\n"
            "   Create deny rule to block future similar actions\n"
        )
        self.assertEqual(state, "blocked")
        self.assertEqual(rule, "workflow_requires_approval")
        shell_state, shell_rule = self.explain(
            "▶ two-step running 1/2 · ctrl+x expand · ctrl+g monitor\n"
            " shell requires approval\n"
            " ❯ Allow once\n"
            "   Deny once\n"
        )
        self.assertEqual((shell_state, shell_rule), ("blocked", "workflow_requires_approval"))

    def test_edit_approval_beats_the_banner(self):
        state, rule = self.explain(
            "▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "────────\n"
            "shell requires approval · modify request\n"
            "> printf probe\n"
            "────────\n"
            "esc to close\n"
        )
        self.assertEqual(state, "blocked")
        self.assertEqual(rule, "tool_approval_edit")

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

    def _capture(self, name):
        path = os.path.join(ROOT, "logs", name)
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_workspace_trust_width_matrix(self):
        raw = self._capture("workspace-trust.txt")
        for width in WIDTHS:
            state, rule = self.explain(wrap_terminal(raw, width))
            self.assertEqual((width, state, rule), (width, "blocked", "workspace_trust"))

    def test_workspace_trust_prose_width_matrix(self):
        prose = (
            "▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "The dialog says Trust, continue or Don't trust, exit. "
            "Revoke later by removing workspaceTrusted from the permissions file "
            "after you have read this long note about the workspace.\n"
            "› ask a question or describe a task\n"
        )
        for width in WIDTHS:
            state, rule = self.explain(wrap_terminal(prose, width))
            self.assertNotEqual((width, rule), (width, "workspace_trust"))
            self.assertEqual((width, state), (width, "working"))

    def test_workspace_trust_banner_last_is_not_the_dialog(self):
        screen = (
            "Trust this workspace?\n"
            "❯ Trust, continue\n"
            "  Don't trust, exit\n"
            "esc to exit · ↑↓ to navigate · ↵ to select\n"
            "▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n"
        )
        state, rule = self.explain(screen)
        self.assertNotEqual(rule, "workspace_trust")
        self.assertEqual(state, "working")

    def test_quoted_trust_prose_during_a_running_workflow_stays_working(self):
        # Kiro V3 keeps the status bar and the prompt under the banner.
        # The quoted rows use the dialog marker, with a gap after the title,
        # and the prompt is the end of the screen.
        gap = "\n".join(
            "The notes quote the trust dialog, line %s." % index for index in range(6)
        )
        screen = (
            "Trust this workspace?\n"
            + gap
            + "\n"
            " ❯ Trust, continue\n"
            "   Don't trust, exit\n"
            "▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "Default · auto · ◔ 3% · /workspace/fixtures/wf-repro · (main)\n"
            "\n"
            "›  ask a question or describe a task ↵\n"
        )
        state, rule = self.explain(screen)
        self.assertNotEqual(rule, "workspace_trust")
        self.assertEqual(state, "working")

    def test_trust_rows_that_do_not_end_the_screen_do_not_match(self):
        filler = "\n".join("later log %s" % index for index in range(20))
        screen = (
            "Trust this workspace?\n"
            " ❯ Trust, continue\n"
            "   Don't trust, exit\n"
            + filler
            + "\n› ask a question or describe a task\n"
        )
        state, rule = self.explain(screen)
        self.assertNotEqual(rule, "workspace_trust")
        self.assertEqual(state, "idle")

    def test_trust_gap_over_the_cap_does_not_match(self):
        gap = "\n".join("quoted line %s" % index for index in range(30))
        screen = (
            "Trust this workspace?\n"
            + gap
            + "\n ❯ Trust, continue\n"
            "   Don't trust, exit\n"
        )
        _state, rule = self.explain(screen)
        self.assertNotEqual(rule, "workspace_trust")

    def test_indented_and_quoted_trust_rows_do_not_match(self):
        indented = (
            "    Trust this workspace?\n"
            "    ❯ Trust, continue\n"
            "    Don't trust, exit\n"
        )
        quoted = (
            "Trust this workspace?\n"
            "> Trust, continue\n"
            "Don't trust, exit\n"
        )
        for screen in (indented, quoted):
            _state, rule = self.explain(screen)
            self.assertNotEqual(rule, "workspace_trust")

    def test_workspace_trust_rows_without_a_marker_do_not_match(self):
        screen = (
            "Trust this workspace?\n"
            "Trust, continue\n"
            "Don't trust, exit\n"
            "esc to exit · ↑↓ to navigate · ↵ to select\n"
        )
        _state, rule = self.explain(screen)
        self.assertNotEqual(rule, "workspace_trust")

    def test_chooser_width_matrix(self):
        raw = self._capture("monitor-v10.txt")
        prose = (
            "▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "The notes say this step requires approval and Allow once is the default. "
            "Deny once would stop the write if you picked it from the paragraph.\n"
            "› ask a question or describe a task\n"
        )
        for width in WIDTHS:
            state, rule = self.explain(wrap_terminal(raw, width))
            self.assertEqual(
                (width, state, rule),
                (width, "blocked", "workflow_requires_approval"),
            )
            prose_state, prose_rule = self.explain(wrap_terminal(prose, width))
            self.assertNotEqual((width, prose_rule), (width, "workflow_requires_approval"))
            self.assertEqual((width, prose_state), (width, "working"))

    def test_subagent_widget_width_matrix(self):
        raw = self._capture("widget-v10.txt")
        prose = (
            "▶ approve-probe running 0/1 · ctrl+x expand · ctrl+g monitor\n"
            "The model mentioned a tool approval and said the notes can wait. "
            "Nothing on this screen is the subagent chooser.\n"
            "› ask a question or describe a task\n"
        )
        for width in WIDTHS:
            state, rule = self.explain(wrap_terminal(raw, width))
            self.assertEqual((width, state, rule), (width, "blocked", "crew_approval"))
            prose_state, prose_rule = self.explain(wrap_terminal(prose, width))
            self.assertNotEqual((width, prose_rule), (width, "crew_approval"))
            self.assertEqual((width, prose_state), (width, "working"))


if __name__ == "__main__":
    unittest.main()
