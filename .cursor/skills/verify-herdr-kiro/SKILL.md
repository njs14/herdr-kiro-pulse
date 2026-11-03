---
name: verify-herdr-kiro
description: Drive the herdr-kiro hook in an isolated herdr 0.9.5 session and compare agent get with agent explain. Use to prove idle, working, tool approval, workspace trust, a finished turn, or the release handoff.
---

# Verify herdr-kiro

The user-facing surface is a herdr pane whose foreground process is `kiro-cli`, plus the Kiro hook in `prototype/herdr_kiro_state.py`. `herdr agent get` is the status herdr shows. `herdr agent explain` is the screen rule. After a turn those two must agree.

This skill's driver is `.cursor/skills/verify-herdr-kiro/drive.py`. Run it from the repo root. Set `HERDR_BIN` to a herdr 0.9.5 binary when `herdr` is not on `PATH`. Set `VERIFY_RUN` to a scratch directory (default `/tmp/herdr-kiro-verify`). One run owns that directory. Do not drive a herdr the user already has open.

## Launch

```bash
python3 .cursor/skills/verify-herdr-kiro/drive.py launch
```

Launch compiles `painter.c` to `$VERIFY_RUN/bin/kiro-cli`, writes a throwaway agent at `$VERIFY_RUN/home/.kiro/agents/verify-herdr.md`, and writes `$VERIFY_RUN/workspace/.kiro/permissions.yaml` with one rule: tool `*` permission `ask`. It copies `prototype/kiro-detection.toml` into that home's herdr agent-detection directory, starts `herdr --session verify server` in the scratch home, creates a workspace if the server has none, and runs the painter in the pane. Ready means stdout is JSON with `"ready": true` and a pane id.

The painter is the screen stand-in. It is not Kiro. A live Mac run does not compile it and does not call `paint` or `hook`.

## Doctor

```bash
python3 .cursor/skills/verify-herdr-kiro/drive.py doctor
```

Doctor is read-only. It requires the server pid from this launch to be alive, `herdr --version` to contain `0.9.5`, workspace list to succeed, and `agent get` on the recorded pane to report agent `kiro` with a cwd under `VERIFY_RUN`. Run it before the first drive and again after a failed drive.

## Drive

```bash
python3 .cursor/skills/verify-herdr-kiro/drive.py paint idle|working|approval|trust
python3 .cursor/skills/verify-herdr-kiro/drive.py hook SessionStart|UserPromptSubmit|PreToolUse|PostToolUse|Stop|SessionEnd
python3 .cursor/skills/verify-herdr-kiro/drive.py wait STATUS RULE [seconds]
python3 .cursor/skills/verify-herdr-kiro/drive.py status --save NAME.json
python3 .cursor/skills/verify-herdr-kiro/drive.py background
python3 .cursor/skills/verify-herdr-kiro/drive.py focus-primary
```

`paint` writes one mode into the painter fifo. The pane then shows that screen and nothing else. `hook` runs the real hook on stdin JSON `{"session_id":"verify-session"}` with `HERDR_ENV=1` and this run's pane id. `wait` polls until `agent get` and `agent explain` both report `STATUS` and the matched rule is `RULE`. `background` creates a second workspace and focuses it, leaving the Kiro pane in the background. That is the unseen-completion case.

## Evidence

`wait` and `status --save` write JSON under `$VERIFY_EVIDENCE/<run-id>/` (default `/tmp/herdr-kiro-verify-evidence/<run-id>/`). Each file has `agent_status`, `state_change_seq`, `completion_seq`, `explain_state`, and `explain_rule`. The herdr server log and `hook.log` are in that same directory. Proof is the command plus the saved JSON, not the final number alone. A finished turn is proved only when a second read, at least 12 seconds after the first idle sample, still has `completion_seq` null and both sides say idle. `done` on that read is a failure. The hook log for that turn has no `release-agent` line while `kiro-cli` is still running. Do not copy these files into the repo.

## Cleanup

```bash
python3 .cursor/skills/verify-herdr-kiro/drive.py cleanup
```

Cleanup sends `SIGTERM` to the watcher pid recorded in this run's state file and to the server process group started by launch, then deletes `VERIFY_RUN`. It does not delete the evidence directory. After cleanup, the `wait-*.json` file from the drive must still be readable.

## CI and a live Mac

CI can run, with herdr 0.9.5 installed and no Kiro key:

- `python3 -m unittest tests.test_grace tests.test_install tests.test_detection tests.test_doctor tests.test_redaction`
- `python3 .cursor/skills/verify-herdr-kiro/drive.py` for idle, working, tool approval, workspace trust, finished turn, and release handoff

These three are CI checks. They do not need herdr or a Kiro key:

- `uvx ruff check`
- `uvx ruff format --check`
- `uvx ty check`

A live Mac run is still required for a real `kiro-cli`. It uses the same throwaway home and permissions file, starts `kiro-cli chat --v3 --trust-workspace` in the pane, and does not pass `--trust-all-tools`. The `ask` rule is what produces a real tool-approval dialog. `--trust-workspace` skips the trust dialog, so the trust drive on a Mac is a fresh workspace that has not been trusted yet, before that flag is used.

Stand-in steps a Mac run will not do:

- compile `painter.c` and exec it as `kiro-cli`
- paint terminal text instead of a Kiro turn
- inject hook JSON from `drive.py hook` instead of Kiro spawning the hook
- skip Kiro login and the model network

## Helpers

`drive.py` is the only helper. It is executable. `painter.c` is the screen stand-in source; launch compiles it with `cc`.
