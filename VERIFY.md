# Verify the herdr Kiro hook

The bundle includes `.git`. `git rev-parse HEAD` is the tree this directory was packed from.

Do not print `KIRO_API_KEY`. Do not write it into logs.

## What passing looks like

`python3 -m unittest tests.test_grace tests.test_install` exits 0.

`python3 harness/run_suite.py` is the live check. For `sequential`, `parallel`, `repeat`, and `solo` a passing summary has no `error` key, and:

- a workflow banner appears (`running` with `ctrl+g` or a triangle)
- from the first hook event through the last Stop, every sample is `working`
- the banner may paint for less than a second before that first event
- the first of four idle samples in a row is at least 2.5 seconds after the last Stop and at most 3.5 seconds after it
- `logs/ps-<name>.txt` says no `herdr_kiro_state.py` watcher is left
- `logs/stops-<name>.txt` has the Stop lines for that run, with timestamps, so the last Stop can be checked against the poll log

`watch` (`/workflow run watch-step`) does not pass. Kiro runs that command watch without emitting a hook, so the banner stays idle for about 10 seconds. See the results table below.

## Install and uninstall

```bash
./install.sh
```

This writes `$HOME/.kiro/hooks/herdr-kiro.json`. The command is built with `json.dumps`, and the path is shell-quoted. If a hook file is already there, it is copied to `herdr-kiro.json.bak.<timestamp>` first. Restart Kiro after installing.

```bash
./uninstall.sh
```

This deletes the hook file, sends `SIGTERM` to `herdr_kiro_state.py --watch` processes, and removes `$XDG_STATE_HOME/herdr-kiro` (or `~/.local/state/herdr-kiro`) plus `/tmp/herdr-kiro-*`.

State files and `hook.log` are mode `0600`. The directory is mode `0700`. The log is cut back after 200KB. `<state>.seq` stores the highest sequence. A state file rewritten with a lower `seq` does not move the next report backwards.

## Dependencies

herdr 0.9.5, the published release binary. Kiro CLI 2.37.0 with V3 enabled both ways:

```bash
kiro-cli settings chat.agentEngine v3
kiro-cli settings chat.enableWorkflows true
kiro-cli settings chat.workflowLaunch when-asked
kiro-cli chat --v3 --trust-all-tools --trust-workspace
```

Put `KIRO_API_KEY` in the environment. Check with `kiro-cli whoami`. Do not log the key.

## Live harness

```bash
tmux new-session -d -s herdr-server -- herdr --session kiro-repro server
herdr --session kiro-repro workspace create --cwd "$PWD/fixtures/wf-repro" --label wf-repro
export HERDR_SESSION=kiro-repro
export HERDR_PANE=w1:p1
python3 harness/run_suite.py
```

The suite does not install the hook. Run `./install.sh` first. Poll logs are `logs/poll-*.jsonl`.

## Results from this VM

Kiro CLI 2.37.0, herdr 0.9.5, engine v3. Poll interval 0.4 seconds.

| run | banner samples | idle during the run | idle after last Stop | watcher left |
| --- | --- | --- | --- | --- |
| sequential | 66 | 0 | 3.437s | no |
| parallel | 37 | 0 | 3.430s | no |
| repeat | 34 | 0 | 3.374s | no |
| solo | 29 | 0 | 3.257s | no |
| watch (`/workflow run`) | 24, all idle | banner idle 10.19s before any hook | n/a | no |
| watch started by the model tool | 25 | 1 sample | 3.13s | no |

The slash-command watch is the remaining failure. `shell.workflow.watch.command.poll` runs the command and does not emit `SessionStart`, `UserPromptSubmit`, or a tool hook. The pane stays idle until a later model turn. A watch the model starts with the run-workflow tool stays working except for one poll that landed between working reports while the footer repainted.
