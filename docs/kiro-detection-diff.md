# Kiro detection override against herdr 2026.09.19.1

Upstream file: `distribution/agent-detection/kiro.toml` in herdr, Apache-2.0.

```
version = "2026.09.19.1"
min_engine_version = 2
updated_at = "2026-09-19T00:00:00Z"
```

Last verified against herdr 0.9.5 bundled `manifest_version` `2026.09.19.1` (matches `BUILT_AGAINST` in `prototype/herdr_kiro_doctor.py`). CI fails when that bundled version drifts ahead of `BUILT_AGAINST`; rebase this override, bump `BUILT_AGAINST`, and refresh this doc when that happens.

Herdr 0.9.5 does not merge a local `agent-detection/kiro.toml` with the remote manifest. The file replaces it. `prototype/kiro-detection.toml` is that upstream file plus the edits below. While it is installed, a newer remote Kiro manifest is not applied. Uninstall restores the previous file.

## Changed rules

| id | upstream | this copy |
| --- | --- | --- |
| `live_prompt_idle` | `visible_idle = true`, priority 1100 | `visible_idle = false`, priority 1100 |
| `tool_approval` | priority 1050 | priority 1410 |
| `tool_approval_edit` | priority 1045 | priority 1400 |
| `crew_approval` | priority 1040 | priority 1390 |
| `question_panel` | priority 1030 | priority 1380 |

`live_working_footer` (950), `osc_title_working` (900), and `osc_progress_working` (890) are unchanged. The regex text of the stock rules is unchanged. Priorities on the blocked rules moved above the banner so a `▶ name running` line cannot hide a chooser.

`visible_idle = false` makes herdr hold a working-to-idle change for a few hundred milliseconds. A one-frame idle prompt under the banner is not published on that frame. Plain chat reaches idle a little later than the stock rule.

## Added rules

| id | state | priority | region |
| --- | --- | --- | --- |
| `workspace_trust` | blocked | 1440 | `bottom_non_empty_lines(48)` |
| `workflow_requires_approval` | blocked | 1430 | `bottom_non_empty_lines(24)` |
| `workflow_chooser_blocked` | blocked | 1420 | `bottom_non_empty_lines(12)` |
| `workflow_question_esc_cancel` | blocked | 1385 | `bottom_non_empty_lines(12)` |
| `workflow_trust_all` | blocked | 1370 | `bottom_non_empty_lines(16)` |
| `workflow_approval_more_options` | blocked | 1360 | `bottom_non_empty_lines(12)` |
| `workflow_banner_working` | working | 1200 | `whole_recent` |

`workflow_requires_approval` matches when the bottom of the screen contains `requires approval` and both `Allow once` and `Deny once` are rows. A newline inside `requires` still matches, which is what a 54-column wrap does to that row. A leading box border, text before a `│` column, and a trailing scrollbar glyph are part of the row. A sentence that mentions the words does not match. A chooser sitting about 60 lines above the prompt does not match, because the region is the bottom 24 non-empty lines.

`workspace_trust` matches `Trust this workspace?`, then a selected `Trust, continue` row and a `Don't trust, exit` row. At most 20 lines sit between the title and the selected row. The selected row is a short indent and `❯`. A `>` quote or a 4-space indent does not match. Wrapped revoke and footer lines can sit after the rows, and the rows end the screen. A workflow banner suppresses the rule when it is the last line, and also when the status bar and the prompt sit below it. Quoted option lines under a banner do not match.

`crew_approval` still requires the subagent phrases. `configure individually (agent monitor)` may split across a newline in a 40-column pane.

`workflow_banner_working` matches a line that starts with `▶`, then a token, then `running`, and also contains `ctrl+x`. `ctrl+g` may be wrapped or clipped. A model line such as `Status: ▶ deploy running` has no `ctrl+x`.
