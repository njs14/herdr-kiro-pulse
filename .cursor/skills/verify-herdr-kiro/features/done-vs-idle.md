# Finished turn

After a Kiro turn ends on a workspace that is not the focused one, and the agent has already been working once, `herdr agent get` returns `done` and `herdr agent explain` returns idle. Explain names `live_prompt_idle`. `completion_seq` matches `state_change_seq`. `herdr agent focus <pane>` marks that pane seen: get becomes idle on the same seq, and explain stays idle.

## Sub-features

- `turn-stop` sends Stop after the prompt is back.
- `unseen-done` is get `done` and explain idle while another workspace is focused.
- `seen-idle` is both idle after `herdr agent focus` on that pane.

## How to get to it (user POV)

- Let a turn finish in a workspace you are not looking at. The hook receives Stop and the prompt returns. Get says done. Explain says the idle prompt.
- Focus that pane with `herdr agent focus <pane_id>`. Get becomes idle on the same seq. Explain stays on the idle rule. `completion_seq` stays.

## Driving it with verify-herdr-kiro

Preconditions:

- Doctor still reports agent `kiro`.
- The pane is `w1:p1` from this launch.

- **Leave the pane.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py background`. Stdout names a workspace other than `w1`.
- **Settle the process.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint working`, `python3 .cursor/skills/verify-herdr-kiro/drive.py hook UserPromptSubmit`, `python3 .cursor/skills/verify-herdr-kiro/drive.py paint idle`, and `python3 .cursor/skills/verify-herdr-kiro/drive.py hook Stop`. Wait about a second. `python3 .cursor/skills/verify-herdr-kiro/drive.py status` may report `agent_status` `idle` with `completion_seq` null. That first idle is herdr's process-acquisition guard. It is not the finished-turn sample.
- **Start the measured turn.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint working` and `python3 .cursor/skills/verify-herdr-kiro/drive.py hook UserPromptSubmit`. `python3 .cursor/skills/verify-herdr-kiro/drive.py status` reports `agent_status` `working`.
- **End the measured turn.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint idle` and `python3 .cursor/skills/verify-herdr-kiro/drive.py hook Stop`.
- **Read the unseen status.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait done idle live_prompt_idle 12`. The saved JSON has `agent_status` `done`, `explain_state` `idle`, `explain_rule` `live_prompt_idle`, and `completion_seq` equal to `state_change_seq`.
- **Hold that sample for the release check.** When this launch also runs the release-handoff recipe, do that 12 second read and the log check before the next step.
- **Mark the pane seen.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py seen`. That runs `herdr agent focus` on `w1:p1`. Stdout `agent_status` is `idle`, and `completion_seq` is unchanged.
- **Read the seen status.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait idle live_prompt_idle 8`. The saved JSON has `agent_status` `idle`, `explain_state` `idle`, `explain_rule` `live_prompt_idle`, and the same `completion_seq` as the unseen read.

## Gotchas

- `herdr pane focus --direction` moves to a neighbor. It does not mark this pane seen. A direction with no neighbor leaves get on `done`.
- `herdr agent get` and `herdr agent explain` do not mark the pane seen. Another read leaves `done` in place.
- The first idle after the process is detected is not a completion. Herdr suppresses it. Read a later turn.
- A pane that never published `working` or `blocked` does not become `done`. Explain can already say idle, and `completion_seq` stays absent. That is the Mac Codex error turn. An error title after a working title is `done`.
- The active workspace suppresses this completion when no client has reported the terminal unfocused. Get stays idle. This recipe focuses another workspace first.
- `release-agent` while `kiro-cli` is still running drops `done`. With the idle prompt already up, get goes unknown and then idle, and `completion_seq` disappears. With the working footer still up, get goes back to working, and the later idle screen settles idle. The finished turn keeps the idle report. Owner exit still releases.
