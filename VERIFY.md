# Verify the herdr Kiro hook

The bundle includes `.git`. `git rev-parse HEAD` is the commit this tree was packed from.

Do not print `KIRO_API_KEY`. Do not write it into logs.

## What passing looks like

`python3 -m unittest tests.test_grace tests.test_install tests.test_detection` exits 0.

`tests/run_py39.sh` runs those tests on Python 3.9. Stock macOS `/usr/bin/python3` is 3.9, and the hook uses `from __future__ import annotations` so `int | None` is not evaluated at import.

`python3 harness/run_suite.py` is the live check. A passing case has no `error` key, and:

- every sample taken while the workflow banner is up is `working`
- the first of four idle samples in a row is within 3.5 seconds of the later of the last banner sample and the last Stop
- `logs/ps-<name>.txt` says no `herdr_kiro_state.py` watcher is left
- `logs/stops-<name>.txt` has the Stop lines for that run

`tests/test_detection.py` feeds screens to `herdr agent explain`. A workflow banner is working. The same banner above the chooser footer is blocked. Phrases such as "always allow" and "esc to close" in ordinary text stay idle.

## Install and uninstall

```bash
./install.sh
```

This writes `$HOME/.kiro/hooks/herdr-kiro.json` with `json.dumps` and a shell-quoted command. An existing hook file is copied to `herdr-kiro.json.bak.<timestamp>`.

It also installs `~/.config/herdr/agent-detection/kiro.toml`. Herdr 0.9.5 loads that path as a local manifest override. No rebuild. The file replaces the cached remote Kiro rules, keeps them, and adds two rules: a workflow banner at priority 1200, and the chooser footer at priority 1300 so an approval still beats the banner. The idle-prompt rule in this copy does not set `visible_idle`, so a one-frame prompt under the banner is held instead of published. `herdr --session <name> server reload-agent-manifests` loads it into a running server.

On macOS the same binary looks up config under `~/.config/herdr` (or `$XDG_CONFIG_HOME/herdr`). Install the published herdr 0.9.5 build, run `./install.sh`, then reload manifests. There is no source patch.

```bash
./uninstall.sh
```

This removes the hook file and restores the newest `herdr-kiro.json.bak.*` if one exists, then deletes the backups. It stops only watchers recorded for this user in the state directory `pids` files. It does not read `/proc`. It leaves `*.seq` so the next run does not restart the sequence at 1. A fresh state file with no floor starts at the current unix time instead of 1. It removes the Kiro detection override when that file contains `workflow_banner_working`, and restores a backup of that file the same way.

## Stripped session ids

Kiro 2.37.0 starts each hook in a new process. A parent pid is different on every event, so it cannot join a Start to its Stop. When `session_id` is missing, every event for the pane shares one aggregate slot. One Stop, the banner clearing, or `kiro-cli` exiting ends that slot, then the 3 second grace runs. Busy count cannot grow with the event count.

`harness/strip_session_id.py` is the live probe. It strips `session_id` and runs the real hook as a child. On this VM, 19 stripped events had 19 different immediate parents and one shared `kiro-cli`. Max busy was 1. The last Stop left busy at 0 and started grace. The watcher then exited.

## Results from this VM

Kiro CLI 2.37.0, herdr 0.9.5, engine v3. Polls are 0.4 seconds apart.

| run | banner samples | non-working while banner up | idle after the run | watcher left |
| --- | --- | --- | --- | --- |
| sequential | 66 | 0 | 0.137s | no |
| parallel | 34 | 0 | 0.222s | no |
| repeat | 31 | 0 | 0.294s | no |
| watch, `/workflow run watch-step` | 24 | 0 | 0.148s | no |
| solo | 28 | 0 | 0.283s | no |
| watch, model run-workflow tool | 25 | 0 | 0.399s | no |

An earlier pass of parallel and of the model-started watch each caught one idle poll. Both were rerun and those reruns are the rows above. The slash-command watch did not need a rerun.
