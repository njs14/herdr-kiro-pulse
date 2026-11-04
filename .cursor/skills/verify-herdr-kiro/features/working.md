# Working banner

A running workflow banner means the pane is working. Get and explain both report working, and explain names `workflow_banner_working`.

## Sub-features

- `banner-visible` shows a `▶ name running` line that includes `ctrl+x`.
- `banner-agrees` makes get and explain both working.

## How to get to it (user POV)

- Start a Kiro workflow so the pane draws `▶ two-step running` with `ctrl+x` on that line.
- On a Mac, send a prompt that starts a workflow and wait for that banner. Do not trust the idle prompt underneath it.

## Driving it with verify-herdr-kiro

Preconditions:

- Doctor still reports agent `kiro` or `kiro-cli`.
- Launch's idle wait has already passed, or doctor has seen agent `kiro` or `kiro-cli`.

- **Show the banner.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint working`.
- **Read the status.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait working workflow_banner_working 8`. The saved JSON has `agent_status` `working`, `explain_state` `working`, and `explain_rule` `workflow_banner_working`.

## Gotchas

- A line that mentions a banner but has no `ctrl+x` is not this rule. The painter's working screen includes `ctrl+x`.
- The idle prompt must not remain on the visible screen. `paint` clears the pane before drawing the banner.
- On a Mac, a workflow that never draws the banner cannot use this recipe. Report that, and do not paint.
