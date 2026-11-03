# Tool approval

The Allow once chooser blocks the pane. Get and explain both report blocked, and explain names `workflow_requires_approval`.

## Sub-features

- `approval-chooser` shows `requires approval`, `Allow once`, and `Deny once`.
- `approval-agrees` makes get and explain both blocked.

## How to get to it (user POV)

- Let Kiro ask to use a tool. The throwaway permissions rule is tool `*` permission `ask`, so a tool call asks instead of running.
- On a Mac, do not pass `--trust-all-tools`. Confirm the chooser, or cancel it, yourself.

## Driving it with verify-herdr-kiro

Preconditions:

- Doctor still reports agent `kiro`.
- The permissions file under the scratch workspace contains tool `*` permission `ask`. The stand-in does not read that file.

- **Show the chooser.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint approval`.
- **Read the status.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait blocked workflow_requires_approval 8`. The saved JSON has `agent_status` `blocked`, `explain_state` `blocked`, and `explain_rule` `workflow_requires_approval`.

## Gotchas

- `--trust-all-tools` skips this dialog. The Mac command must not include it.
- The stand-in paints the chooser. It does not execute a tool and it does not consult `permissions.yaml`. A Mac run that never reaches a tool call has not verified this feature.
- Two blocked waits in one run overwrite nothing if you keep the distinct `wait-blocked-*.json` names. Do not rename them onto one file.
