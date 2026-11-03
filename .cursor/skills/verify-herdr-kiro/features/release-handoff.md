# Release handoff

A finished turn does not release the pane. The hook reports idle and leaves that report in place while `kiro-cli` is still running. On a background workspace, get stays `done` and explain stays idle across the old release window. The hook log for that turn has no `release-agent` line. After the pane is seen, get stays idle.

## Sub-features

- `no-release-after-idle` leaves hook authority in place after the prompt is quiet.
- `stable-done` is still `done` 12 seconds later, while the workspace stays in the background.

## How to get to it (user POV)

- Finish a turn and leave the prompt up on a workspace you are not viewing. The hook reports idle and does not hand the pane back.
- On a Mac, read `herdr agent get` again about 12 seconds after Stop, before you focus the pane. It is still `done`. Explain is still `live_prompt_idle`. The hook log has no `release-agent` line.

## Driving it with verify-herdr-kiro

Preconditions:

- The finished-turn recipe has run in this same launch through `wait done idle live_prompt_idle`, and `seen` has not run yet.
- The evidence directory for this launch still exists.

- **Let the old release window pass.** Wait 12 seconds after the unseen wait returns. Two seconds used to be the release delay.
- **Read the pane again.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py status --save release.json`. The saved JSON has `agent_status` `done`, `explain_state` `idle`, `explain_rule` `live_prompt_idle`, and the same `completion_seq` as the unseen wait.
- **Read the log.** Open `$evidence/hook.log` from the launch JSON. It has no `release-agent` line for this idle turn. `watcher exit idle` may be present. A `release-agent` line while the painter or `kiro-cli` is still the foreground process fails the check.
- **Mark seen and read again.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py seen`, then `python3 .cursor/skills/verify-herdr-kiro/drive.py status --save seen.json`. `agent_status` is `idle`. `explain_state` is `idle` and `explain_rule` is `live_prompt_idle`. `completion_seq` is unchanged.

## Gotchas

- `herdr agent focus` and `herdr workspace focus` on that workspace turn `done` into `idle` without a new completion. Run them after the 12 second read.
- Owner exit still calls `release-agent`. That line belongs to a dead Kiro process, not to a quiet prompt.
- Cleanup kills the watcher. Copy or read `hook.log` before cleanup. Cleanup leaves the evidence directory in place.
