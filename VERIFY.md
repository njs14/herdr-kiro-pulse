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

This writes `$HOME/.kiro/hooks/herdr-kiro.json` with `json.dumps` and a shell-quoted command. The first user file it replaces is saved as `*.original`. The sha256 of the bytes written is stored in the `*.herdr-kiro-owned` marker. An older marker with no hash does not overwrite a hand edit. Install saves a timestamped copy and leaves the edited file in place.

It also installs `~/.config/herdr/agent-detection/kiro.toml`. Herdr 0.9.5 loads that path as a local manifest override and does not merge it with the remote file. No rebuild, and no narrower override. The copy is the Kiro rules from version `2026.09.19.1` plus the rules in `docs/kiro-detection-diff.md`. Every blocked rule in the copy, including the question panel, the trust-all dialog, and the `enter to see more options` footer, is above 1200 so a banner cannot hide a blocker. The idle-prompt rule sets `visible_idle = false`. herdr then holds a working-to-idle change for a few hundred milliseconds. That is slower for plain chat, and it is what stops a one-frame idle prompt under the banner from publishing. While this file is installed, newer remote Kiro rules are not applied. Uninstall restores the previous file. `herdr --session <name> server reload-agent-manifests` loads it into a running server.

`herdr plugin link` plus `herdr plugin action invoke herdr-kiro.install` runs the same script. `./doctor.sh` is the status command.

`./install.sh` writes a sidecar `*.herdr-kiro-owned` next to each file it owns, including the sha256 of the bytes it wrote. It copies a pre-existing user file to `*.original` once, and a second install does not replace that copy. A user-edited installed file is copied to a new `*.user-modified.<timestamp>` name before install overwrites it. A missing marker preserves a hand edit as `.original` unless the bytes are exactly the installer payload. `./uninstall.sh` deletes a file only when the stored hash still matches. A changed file is renamed to a new timestamped `*.user-modified` name, and `*.original` moves back when that name is free. Uninstall reloads with `herdr --session "$HERDR_SESSION"` when that variable is set. No running server is a successful install with a note. A failed reload against a running server exits nonzero.

Workflows have to be on for these runs:

```bash
kiro-cli settings chat.enableWorkflows true
```

On macOS the same binary looks up config under `~/.config/herdr` (or `$XDG_CONFIG_HOME/herdr`). Install the published herdr 0.9.5 build, run `./install.sh`, then reload manifests. There is no source patch.

```bash
./uninstall.sh
```

This removes the hook file when the stored hash still matches, and renames a user-edited copy to `herdr-kiro.json.user-modified`. It stops only watchers recorded for this user in the state directory `pids` files. It does not read `/proc`. It leaves `*.seq` so the next run does not restart the sequence at 1. A fresh state file with no floor starts at the current unix time instead of 1. It removes the Kiro detection override when that file contains `workflow_banner_working`, and restores `*.original` the same way.

## Stripped session ids

Kiro 2.37.0 starts each hook in a new process. A parent pid is different on every event, so it cannot join a Start to its Stop. When `session_id` is missing, every event for the pane shares one aggregate slot. The slot ends when the SessionStart count returns to zero, when the idle prompt has stayed up with no banner and no tool for 2 seconds, or when the outer `kiro-cli` process exits. The grace after that is 0.5 seconds. Busy count cannot grow with the event count. A second SessionStart does not end the slot by itself.

`harness/strip_session_id.py` is the live probe. It strips `session_id` and runs the real hook as a child. On this VM, 19 stripped events had 19 different immediate parents and one shared `kiro-cli`. Max busy was 1. The last Stop left busy at 0 and started grace. The watcher then exited.

## Results from this VM

Kiro CLI 2.37.0, herdr 0.9.5, engine v3. Polls are 0.4 seconds apart. Grace is 0.5 seconds. A stuck slot clears after 2 seconds of a settled screen.

| run | banner samples | non-working while banner up | idle after the run | watcher left |
| --- | --- | --- | --- | --- |
| sequential | 63 | 0 | 0.103s | no |
| parallel | 35 | 0 | 0.035s | no |
| repeat | 33 | 0 | 0.424s | no |
| watch, `/workflow run watch-step` | 24 | 0 | 0.209s | no |
| solo | 29 | 0 | 0.517s | no |
| watch, model run-workflow tool | 25 | 0 | 0.353s | no |

The model-started watch had one idle poll during the banner on the first two passes. The row above is the third pass. A 0.4 second poll can still land on a one-frame idle prompt under the banner.

The approval widget was blocked on 8 of 8 polls in each of 3 runs, and the watcher was gone after each. A double Ctrl-C during `two-step` removed the watcher in 0.971 seconds. No `report-agent` followed `watcher exit agent gone`, and the pid file was removed.

A live `Write File requires approval` / `fs_write → note.txt` / `Allow once` chooser was blocked on 6 of 6 polls. `herdr agent explain` on that screen and on the same chooser under a running banner is `workflow_requires_approval`. The subagent widget stayed blocked. Double Ctrl-C removed the watcher in 2.6 seconds, after repeated `agent_not_found` readings rather than the first one.

## v8 on this VM

Python 3.12 with herdr: 71 tests, 0 failures. Python 3.9 with herdr: 71 tests, 0 failures. The same suites with `herdr` removed from `PATH`: 71 ran, 16 skipped, 0 failures, on both interpreters.

The workflow monitor was opened twice on `write-probe` while `fs_write` was waiting. Both captures show `│ ❯ Allow once` and `│   Deny once` next to `write-probe - running`. Status samples were blocked on all 8 polls the first time and all 6 the second time. `herdr agent explain` with this repo's rules returned `workflow_requires_approval`. The same capture with upstream `kiro.toml` `2026.09.19.1` and no local override returned `idle` and no matched rule. That explain is the draft in `docs/herdr-bug-workflow-monitor-fs-write.md`. It is not filed.

The subagent widget under `▶ write-probe running` was blocked on 8 of 8 polls. The trust-all confirmation on pane start was blocked.

A double Ctrl-C that exited `kiro-cli` while a watcher was in its loop logged `watcher exit owner exit` 0.078 seconds after the exiting Ctrl-C. No `report-agent` followed that line.

Kiro CLI 2.37.0's documented hook list includes `Stop` and does not list `SessionEnd`. The hook loader still accepts `SessionEnd` and describes it as loaded-session runtime teardown. The installed hook listens for it. The watcher does not treat that hook as process exit. Process exit is `pidfd` on this Linux VM. `select.kqueue` is not available here, so the macOS `EVFILT_PROC` / `NOTE_EXIT` path was not executed on this machine.

| run | banner samples | non-working while banner up | idle after the run | watcher left |
| --- | --- | --- | --- | --- |
| sequential | 65 | 0 | 0.188s | no |
| parallel | 33 | 0 | 0.395s | no |
| repeat | 34 | 0 | 0.371s | no |
| watch | 25 | 0 | 0.468s | no |
| solo | 32 | 0 | 0.121s | no |
| watch-tool | 25 | 0 | 0.628s | no |

`herdr plugin link` of this tree registered `herdr-kiro` 0.8.0. `herdr plugin install` was not used. It only clones GitHub, and this package is not published.

## v9 on this VM

Python 3.12 and 3.9 with herdr: 91 tests, 0 failures. The same suites with `herdr` removed from `PATH`: 91 ran, 19 skipped, 0 failures, on both interpreters.

`herdr plugin link` accepted `herdr-plugin.toml` 0.9.0, including `startup.sh`. `herdr plugin action invoke herdr-kiro.install` wrote the lifecycle marker `installed` / `via=plugin`. doctor exited 0. `herdr plugin unlink` left both files in place. doctor then exited 1 and named them. `./uninstall.sh` removed the files this install owned and wrote `uninstalled`. On this machine an older `.original` was restored, because that file was here before this install. A home with no `.original` keeps nothing but the lifecycle marker. `startup.sh` with that marker printed that it was leaving the files alone and did not recreate the hook.

A single Ctrl-C that only cancels the turn leaves `kiro-cli` running. The watcher then takes the idle path. Measured here, `watcher exit idle` was 2.30 seconds after the prompt event. That is the same path as the 2.38 to 2.40 second result. The 0.078 second figure from v8 was a later Ctrl-C that actually exited the process. Repeating that here, three Ctrl-C presses that ended `kiro-cli` logged `watcher exit owner exit` 0.092 seconds after the last press and 0.547 seconds after the first. A `SessionEnd` hook during that exit spawned a second watcher, which also logged `watcher exit owner exit`.

The workspace-trust dialog on `/tmp/kiro-trust-probe` was blocked on 6 of 6 status samples. `herdr agent explain` returned `workspace_trust`. The capture is `logs/workspace-trust.txt`.

| run | banner samples | non-working while banner up | idle after the run | watcher left |
| --- | --- | --- | --- | --- |
| sequential | 59 | 0 | 0.199s | no |
| parallel | 32 | 0 | 0.499s | no |
| repeat | 33 | 0 | 0.462s | no |
| watch | 24 | 0 | 0.384s | no |
| solo | 26 | 0 | 0.388s | no |
| watch-tool | 25 | 0 | 0.281s | no |

The macOS `kqueue` path was unit-tested with a fake `select.kqueue`. It was not executed against a macOS kernel.

## v10 on this VM

Python 3.12 and 3.9 with herdr installed: 97 tests, 0 failures. Without herdr: 97 ran, 21 skipped, 0 failures, on both interpreters. The round-trip tests ignore `~/.config/herdr/.plugins.lock` because that file is herdr's lock.

One Ctrl-C during a live turn left watcher pid 403344 running, both `kiro-cli` and `kiro-cli-chat` alive, and no `owner exit` line. `herdr agent get` stayed `working` from that watcher. A later Ctrl-C sequence that actually exited `kiro-cli` logged `watcher exit owner exit` 0.067 seconds after the last press. SIGKILL of `kiro-cli-chat` logged `watcher exit owner exit` 0.459 seconds later. `sleep 45` was `working` on 50 of 51 samples. The last sample was idle after the command finished. The subagent widget was blocked on 4 of 4 polls. The workflow-monitor `fs_write` chooser was blocked on 5 of 5 polls. `TMPDIR=/tmp/v10-custom-tmpdir` received the explain rules directory, and cleanup removed it. doctor with `pending_unlink` unregistered `herdr-kiro` while a server was up.
