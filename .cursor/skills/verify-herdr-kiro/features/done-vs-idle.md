# Finished turn

After a Kiro turn ends, the pane is idle. `herdr agent get` and `herdr agent explain` both report idle, including when that pane's workspace is not the focused one. Explain names `live_prompt_idle`. `completion_seq` is absent. Get does not stay on `done`.

## Sub-features

- `turn-stop` sends Stop after the prompt is back.
- `background-idle` keeps get idle when another workspace is focused.
- `no-completion` leaves `completion_seq` null.

## How to get to it (user POV)

- Let a turn finish. The hook receives Stop, the prompt returns, and the pane is idle whether or not you are looking at that workspace.
- On a Mac, finish a real turn in a background workspace and compare `herdr agent get` with `herdr agent explain` after the status stops changing.

## Driving it with verify-herdr-kiro

Preconditions:

- Doctor still reports agent `kiro`.
- The pane is `w1:p1` from this launch.

- **Leave the pane.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py background`. Stdout names a workspace other than `w1`.
- **Start a turn.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint working` and `python3 .cursor/skills/verify-herdr-kiro/drive.py hook UserPromptSubmit`. `python3 .cursor/skills/verify-herdr-kiro/drive.py status` reports `agent_status` `working`.
- **End the turn.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint idle` and `python3 .cursor/skills/verify-herdr-kiro/drive.py hook Stop`.
- **Read the settled status.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait idle live_prompt_idle 12`. The saved JSON has `agent_status` `idle`, `explain_state` `idle`, `explain_rule` `live_prompt_idle`, and `completion_seq` null.

## Gotchas

- A direct working-to-idle report on an unfocused workspace is herdr's unseen completion, and get returns `done` while explain stays `idle`. This hook clears through `unknown` first so the idle report is not that completion. If get is `done`, the hook regressed.
- Sampling during the release window can read `unknown` for about three seconds. That window is the release handoff, not this recipe. `wait` returns on the idle report that happens before release.
- The Mac run has to use a real Stop from Kiro. Injecting `hook Stop` is the stand-in.
