# Bug report

Draft only. This file is not filed.

## Is this a reproducible bug?

- [x] I confirm this is a reproducible bug, not a feature request, idea, question, contribution proposal, or direction check.
- [x] I reproduced this bug on the version and environment reported below using the exact steps provided.

## Current behavior

Kiro's workflow monitor is open on a running step. The step is waiting on a file-write approval. The pane shows `Write File requires approval`, `fs_write → note.txt`, `Allow once`, and `Deny once`.

`herdr agent explain --file <capture> --agent kiro --json`, with herdr's built-in Kiro manifest `2026.09.19.1` and no local `agent-detection/kiro.toml`, returns state `idle` and no matched rule. The same capture was taken twice, by closing the monitor and opening it again.

Pane capture:

```
 WORKFLOW(S)
 │ ▶ write-probe - running │ 0/1 │ 1m 54s │ Tab agent monitor
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Steps                                 │ Output
 ❯ └── ⚠ write  needs approval  low    │
                                       │ WORKFLOW OUTPUT [write]
                                       │     1+  note
                                       │  esc to cancel
                                       │ Write File requires approval
                                       │ fs_write → note.txt
                                       │
                                       │ ❯ Allow once
                                       │   Deny once
                                       │   Create allow rule to approve future similar actions
                                       │   Create deny rule to block future similar actions
 ctrl+x stop
```

## Expected behavior

The pane should be blocked while that approval is on screen.

## Reproduction

1. herdr 0.9.5, Kiro CLI 2.37.0, `kiro-cli chat --v3`, `chat.enableWorkflows` true, trust-all off.
2. Move any local `~/.config/herdr/agent-detection/kiro.toml` aside and run `herdr server reload-agent-manifests`.
3. In the Kiro workspace, run `/workflow run write-probe` with a workflow step that writes a file.
4. When the subagent approval widget is up, press ctrl+g to open the workflow monitor.
5. Run `herdr agent explain` on the visible pane with the built-in Kiro manifest.

## Impact

A workflow that is waiting on approval still looks idle to detection, so the pane is not shown as blocked.

## Environment

- Herdr version: 0.9.5
- Update channel (stable or preview): `herdr --version` prints 0.9.5 and does not print a channel
- Operating system: Linux
- Terminal: herdr pane
- Shell, if relevant:
- Relevant config, if any: Kiro CLI 2.37.0, V3, workflows enabled. Built-in Kiro manifest `2026.09.19.1`. No local agent-detection override for the explain call above.
