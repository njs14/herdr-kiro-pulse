# Bug report

Draft only. This file is not filed.

## Is this a reproducible bug?

- [x] I confirm this is a reproducible bug, not a feature request, idea, question, contribution proposal, or direction check.
- [x] I reproduced this bug on the version and environment reported below using the exact steps provided.

The screen is an approval chooser. Herdr already has a blocked state for approval choosers. This report is that this one is not classified as blocked. That is the bug. It is not a request for a new kind of status.

## Current behavior

Kiro's workflow monitor can show a file-write approval while the step is running. `herdr agent explain` on that screen, with the built-in Kiro manifest `2026.09.19.1` and no local `agent-detection/kiro.toml`, returns state `idle` and no matched rule.

The capture below is the pane text `herdr pane read` returned. It is not retyped. An earlier draft of this note dropped the mouse line and squeezed spaces. This one does not.

In the session that produced it, the monitor was opened twice and both opens showed this chooser. That is 2 of 2 opens in that session. An earlier check saw the chooser on 1 of 3 opens. A later check of the same build saw it on 3 of 9 or more opens. The other opens showed the step list or an empty monitor. The rate is not "every open."

```
 WORKFLOW(S)                                                                                              ● MOUSE ON
 │ ▶ write-probe - running │ 0/1 │ 1m 54s │ Tab agent monitor
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Steps                                 │ Output
 ❯ └── ⚠ write  needs approval  low    │
                                       │ WORKFLOW OUTPUT [write]
                                       │     1+  note                                                               ▲
                                       │  esc to cancel                                                             █
                                       │ Write File requires approval
                                       │ fs_write → note.txt
                                       │
                                       │ ❯ Allow once
                                       │   Deny once
                                       │   Create allow rule to approve future similar actions
                                       │   Create deny rule to block future similar actions

 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ctrl+x stop
```

## Expected behavior

The pane should be blocked while that approval is on screen.

## Reproduction

1. herdr 0.9.5, Kiro CLI 2.37.0, `kiro-cli chat --v3`, `chat.enableWorkflows` true, trust-all off.
2. Move any local `~/.config/herdr/agent-detection/kiro.toml` aside and run `herdr server reload-agent-manifests`.
3. Save this as `.kiro/workflows/write-probe.workflow.json` and run it from that workspace:

```json
{
  "name": "write-probe",
  "description": "One write step that should raise a tool approval while trust-all is off.",
  "effortLevel": "low",
  "steps": [
    {
      "type": "step",
      "id": "write",
      "agent": "wf-coder",
      "prompt": "Create note.txt containing the single word note, using the file write tool and no shell command. After it is written, call send_message with severity success and the single word done."
    }
  ]
}
```
4. In that workspace run `/workflow run write-probe`.
5. When the subagent approval widget is up, press ctrl+g.
6. If the monitor shows the file-write chooser, run `herdr agent explain` on that pane with the built-in Kiro manifest. If it shows only the step list, the chooser is not on screen and this report does not apply to that open.

## Impact

While the approval is up, detection does not call the pane blocked.

## Environment

- Herdr version: 0.9.5
- Update channel (stable or preview): `herdr --version` prints 0.9.5 and does not print a channel
- Operating system: Linux
- Terminal: herdr pane
- Shell, if relevant:
- Relevant config, if any: Kiro CLI 2.37.0, V3, workflows enabled. Built-in Kiro manifest `2026.09.19.1`. No local agent-detection override for the explain call above.
