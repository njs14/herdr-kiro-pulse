# Idle prompt

The idle prompt is the settled Kiro pane. `herdr agent get` and `herdr agent explain` both report idle, and explain names the rule `live_prompt_idle`.

## Sub-features

- `idle-prompt` shows the ask-a-question line and no banner.
- `idle-agrees` makes get and explain both idle.

## How to get to it (user POV)

- Start `kiro-cli` in a herdr pane and wait until the prompt is sitting with no banner and no chooser.
- On a Mac, start `kiro-cli chat --v3 --trust-workspace` in the throwaway workspace after the trust dialog is gone.

## Driving it with verify-herdr-kiro

Preconditions:

- `drive.py launch` has printed `"ready": true`.
- `drive.py doctor` has printed `"doctor": "ok"`.
- The painter is still on its initial idle screen. Do not paint first.

- **Wait for detection.** Herdr does not publish the first screen during the startup grace. Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait idle live_prompt_idle 12`. The saved JSON has `agent_status` `idle`, `explain_state` `idle`, `explain_rule` `live_prompt_idle`, and `completion_seq` null.

## Gotchas

- Reading status in the first few seconds returns `unknown` or `agent_not_found`. That is the startup grace, not a failed idle check.
- `visible_idle` is off in the shipped detection file. A later working-to-idle screen publish can sit for a fraction of a second. This recipe starts on idle, so that hold does not apply.
- The Mac run must not use the painter. If `kiro-cli` is missing, say so and do not paint a substitute on that machine.
