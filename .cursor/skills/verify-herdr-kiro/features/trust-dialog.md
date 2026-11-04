# Trust dialog

The workspace trust dialog blocks the pane. Get and explain both report blocked, and explain names `workspace_trust`.

## Sub-features

- `trust-dialog` shows `Trust this workspace?`, `Trust, continue`, and `Don't trust, exit`.
- `trust-agrees` makes get and explain both blocked.

## How to get to it (user POV)

- Open `kiro-cli` in a workspace that has not been trusted. Kiro draws the trust dialog before the prompt.
- On a Mac, omit `--trust-workspace` for this drive only. Later drives that need the prompt pass `--trust-workspace` after the dialog is accepted.

## Driving it with verify-herdr-kiro

Preconditions:

- Doctor still reports agent `kiro` or `kiro-cli`.
- No workflow banner is on screen. A banner under the dialog is working, not trust.

- **Show the dialog.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py paint trust`.
- **Read the status.** Run `python3 .cursor/skills/verify-herdr-kiro/drive.py wait blocked workspace_trust 8`. The saved JSON has `agent_status` `blocked`, `explain_state` `blocked`, and `explain_rule` `workspace_trust`.

## Gotchas

- `--trust-workspace` skips the dialog. Using it and then painting is the stand-in, not the Mac path.
- Quoted trust prose under a running banner stays working. The painter's trust screen has no banner.
- The stand-in does not write `workspaceTrusted` into a permissions file. Accepting or rejecting the real dialog is a Mac action.
