# herdr-kiro verification map

This directory is the maintained source for verifying the Kiro hook in a herdr pane. Read the index before driving, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch with `python3 .cursor/skills/verify-herdr-kiro/drive.py launch` from the repo root.
- Set `VERIFY_RUN` to a scratch directory this run owns. The default is `/tmp/herdr-kiro-verify`.
- Set `HERDR_BIN` when the herdr 0.9.5 binary is not on `PATH`.
- Run `python3 .cursor/skills/verify-herdr-kiro/drive.py doctor` and require version `0.9.5`, agent `kiro`, and a cwd under `VERIFY_RUN`.
- Never drive a herdr session this launch did not start.

## Driving conventions

- Start every recipe from a fresh launch unless its preconditions say otherwise.
- Treat every command as literal.
- Paint screens only in the CI stand-in. A live Mac run uses real `kiro-cli` output instead of `paint`, and Kiro's own hook process instead of `hook`.
- Restore the idle screen after a recipe that paints something else, or launch again.
- Do not remove proof JSON during cleanup.

## Proof and skip reporting

- Capture the command and the saved JSON, not only the last status word.
- JSON proof includes `agent_status`, `explain_state`, `explain_rule`, and `completion_seq`.
- Record the feature id with the artifact name.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with verify-herdr-kiro` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

## Features

- [Idle prompt](./idle.md) covers the settled prompt before a turn.
- [Working banner](./working.md) covers a running workflow banner.
- [Tool approval](./tool-approval.md) covers the Allow once chooser.
- [Trust dialog](./trust-dialog.md) covers Trust this workspace.
- [Finished turn](./done-vs-idle.md) covers get and explain after Stop, including a background workspace.
- [Release handoff](./release-handoff.md) covers the delayed release after idle.
