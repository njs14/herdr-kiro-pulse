# Release handoff

A finished turn does not release the pane. The hook reports idle and leaves that report in place while `kiro-cli` is still running. `herdr agent get` stays idle, including on a background workspace, and `herdr agent explain` stays idle. `completion_seq` stays null. The hook log for that turn has no `release-agent` line.

## Sub-features

- `no-release-after-idle` leaves hook authority in place after the prompt is quiet.
- `settled-idle` is still idle 12 seconds later, not `done`.

## How to get to it (user POV)

- Finish a turn and leave the prompt up. The hook reports idle and does not hand the pane back to screen detection.
- On a Mac, read `herdr agent get` again at about 12 seconds after Stop, not only on the first idle sample. It stays idle. Explain stays `live_prompt_idle`.

## Driving it with verify-herdr-kiro

Preconditions:

- The finished-turn recipe has run in this same launch, through `hook Stop`, and `wait idle` has already saved its JSON.
- The evidence directory for this launch still exists.

- **Let the old release window pass.** Wait 12 seconds after the idle wait returns. Two seconds used to be the release delay, and a live check at 10 seconds still saw `done`.
- **Read the settled pane.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py status --save release.json`. The saved JSON has `agent_status` `idle`, not `done`. `explain_state` is `idle`, `explain_rule` is `live_prompt_idle`, and `completion_seq` is null.
- **Read the log.** Open `$evidence/hook.log` from the launch JSON. It has no `release-agent` line for this idle turn. `watcher exit idle` may be present. A `release-agent` line while the painter or `kiro-cli` is still the foreground process fails the check.

## Gotchas

- The first idle sample can pass and the pane can still become `done` after `release-agent`. Wait the 12 seconds and read get again. `done` with explain `live_prompt_idle` is the failure.
- Owner exit still calls `release-agent`. That line belongs to a dead Kiro process, not to a quiet prompt.
- Cleanup kills the watcher. Copy or read `hook.log` before cleanup. Cleanup leaves the evidence directory in place.
