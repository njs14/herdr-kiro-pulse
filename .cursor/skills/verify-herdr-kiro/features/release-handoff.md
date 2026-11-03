# Release handoff

After the prompt has been idle, the hook releases herdr's hook authority. The hook log contains `release-agent`. Once that handoff settles, get and explain both report idle and `completion_seq` is null.

## Sub-features

- `release-logged` records `release-agent` in `hook.log`.
- `release-settles` returns to idle after the unknown gap.

## How to get to it (user POV)

- Finish a turn and leave the prompt up. The hook reports idle, waits, then releases the pane back to screen detection.
- On a Mac, read `herdr agent get` again a few seconds after the turn ends, not only on the first idle sample.

## Driving it with verify-herdr-kiro

Preconditions:

- The finished-turn recipe has run in this same launch, through `hook Stop`, and `wait idle` has already saved its JSON.
- The evidence directory for this launch still exists.

- **Let the delay pass.** Wait eight seconds after the idle wait returns. The hook releases about two seconds after idle, and herdr can then report `unknown` for about three seconds more. Six seconds can still land on that unknown sample.
- **Read the settled pane.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py status --save release.json`. The saved JSON has `agent_status` `idle`, `explain_state` `idle`, `explain_rule` `live_prompt_idle`, and `completion_seq` null.
- **Read the log.** Open `$evidence/hook.log` from the launch JSON. It contains a `release-agent` line for this pane and `watcher exit idle`.

## Gotchas

- `status` in the three seconds after `release-agent` often reads `unknown` while explain still says idle. Wait until the status stops changing. Do not treat that sample as the settled handoff.
- The two second delay is measured from when the prompt became quiet, not from the `watcher idle` log line. Release can land later than two seconds after that line.
- Releasing while the hook still holds authority does not publish the idle screen first. The following screen publish is what ends the unknown gap.
- Cleanup kills the watcher. Copy or read `hook.log` before cleanup. Cleanup leaves the evidence directory in place.
