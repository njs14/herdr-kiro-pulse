# herdr Kiro workflow state

Kiro workflow steps run in their own sessions. The parent pane keeps the idle prompt on the last lines, under a banner like `▶ name running 0/2 · ctrl+g monitor`. herdr's Kiro idle rule matches that prompt, so the pane looks idle while the workflow is still running.

This hook counts busy Kiro sessions in the pane and reports the pane working. It reports idle only after a 3 second grace with no busy session and no workflow banner, then it releases authority so screen detection takes over.

## Why the grace was 0.2 seconds

The hook slept for 3 seconds and then reported idle. The sleep did run for 3.000 seconds. herdr had already flipped the pane to idle about 0.18 seconds after the last Stop.

That flip happens when a foreground turn repaints from the `Kiro is working` footer back to the idle prompt. A working report that is already on screen survives a quiet idle prompt. The same report does not survive that repaint. herdr also keeps the source's sequence number, so a later report only lands if its sequence is higher. The fix re-sends working, with a fresh sequence, until the grace ends.

Measured on herdr 0.9.5 with Kiro CLI 2.37.0 in V3 mode. The poll log is `logs/poll-gap-before.jsonl`.

## Install

Requires `herdr` and `kiro-cli` on `PATH`.

```bash
./install.sh
```

The script writes `~/.kiro/hooks/herdr-kiro.json`. The command is passed through `json.dumps`, and the path is shell-quoted, so a checkout path can contain spaces, quotes, and backslashes. An existing hook file is copied to `herdr-kiro.json.bak.<timestamp>` first. Restart Kiro after installing it. Start Kiro in V3 mode:

```bash
kiro-cli settings chat.agentEngine v3
kiro-cli chat --v3
```

`chat.agentEngine v3` is the saved default. `--v3` selects the V3 engine for that process. This repo was tested with Kiro CLI 2.37.0. The official installer on 1 Nov 2026 did not offer a separate 3.0.0 package. The V3 welcome screen in 2.37.0 still says CLI 3.0 launches at the end of October.

Inside a herdr pane the hook sees `HERDR_ENV` and `HERDR_PANE_ID`. Outside herdr it exits without doing anything.

State and the hook log live in `$XDG_STATE_HOME/herdr-kiro` or `~/.local/state/herdr-kiro`, mode `0700` for the directory and `0600` for the files. The log is trimmed after 200KB. A separate `.seq` file keeps the highest sequence. Rewriting the state file with a lower `seq` does not move the next report backwards, which is what made herdr ignore the hook after a reset.

```bash
./uninstall.sh
```

That removes `~/.kiro/hooks/herdr-kiro.json`, stops `herdr_kiro_state.py --watch` processes, and deletes the state directory files plus the old `/tmp/herdr-kiro-*` files.

## Check

```bash
python3 -m unittest tests/test_grace.py
python3 harness/run_suite.py
```

The suite needs a herdr session named `kiro-repro`, a Kiro pane, and `KIRO_API_KEY` in that pane's environment. It runs sequential, parallel, repeat, and watch workflows, plus one workflow Kiro starts from a normal prompt (`solo`). Poll logs land in `logs/`. A run passes when the pane stays working from the first banner sample until the last Stop, then stays idle, and the first of four idle samples in a row is within 3.5 seconds of that Stop and not before 2.5 seconds. After each run the suite checks that no `herdr_kiro_state.py --watch` process is left. Stop lines for the run are copied to `logs/stops-<name>.txt`.

A session that never sends Stop is dropped after 30 minutes. A visible `running` workflow banner keeps the pane working when a watcher is already running and no step session is busy. A command watch started with `/workflow run` does not emit a hook while the banner is up, so that banner stays idle. A watch the model starts with the run-workflow tool is held, aside from a single poll that can land in the footer repaint.
