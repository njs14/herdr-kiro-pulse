# herdr Kiro workflow state

Kiro workflow steps run in their own sessions. The parent pane keeps the idle prompt on the last lines, under a banner like `▶ name running 0/2 · ctrl+g monitor`. herdr's Kiro idle rule matches that prompt, so the pane looks idle while the workflow is still running.

This hook counts busy Kiro sessions in the pane and reports the pane working. It reports idle after a 0.5 second grace with no busy session and no workflow banner, then it releases authority so screen detection takes over. A visible blocker is reported blocked instead, and working reports stop until that blocker leaves the screen.

## Why the grace was 0.2 seconds

The hook slept for 3 seconds and then reported idle. The sleep did run for 3.000 seconds. herdr had already flipped the pane to idle about 0.18 seconds after the last Stop.

That flip happens when a foreground turn repaints from the `Kiro is working` footer back to the idle prompt. A working report that is already on screen survives a quiet idle prompt. The same report does not survive that repaint. herdr also keeps the source's sequence number, so a later report only lands if its sequence is higher. The fix re-sends working, with a fresh sequence, until the grace ends.

Measured on herdr 0.9.5 with Kiro CLI 2.37.0 in V3 mode. The poll log is `logs/poll-gap-before.jsonl`.

## Install

Version 0.12.0. License `WTFPL`. Requires `herdr` 0.9.5 or newer and `kiro-cli` on `PATH`, and Python 3.9 or newer. macOS and Linux.

This tree is a herdr plugin. The manifest is `herdr-plugin.toml` (`id = "herdr-kiro"`).

```bash
herdr plugin link /path/to/this/repo
herdr plugin action invoke herdr-kiro.install
```

`herdr plugin install owner/repo` clones a GitHub repository. It does not install a local checkout, and this package is not published. Use `plugin link` here. `plugin link` does not run the manifest's build command, and a startup command does not run until the next herdr server start, so invoke `herdr-kiro.install` once after linking. That action runs `install.sh`.

```bash
./install.sh
```

`./install.sh` is the same work the plugin action runs. Use it when you are not linking the plugin. `./doctor.sh` prints the hook version, `herdr` and `kiro-cli` versions, `chat.agentEngine`, `chat.enableWorkflows`, whether the detection override and the hook file are installed, and how many watcher pid files exist. It does not print secrets. The plugin action is `herdr plugin action invoke herdr-kiro.doctor`.

### What the plugin system does not do

Herdr has no manifest field for Kiro's hook directory or for `agent-detection`. `plugin install` and `plugin link` only register the package. The Kiro hook `~/.kiro/hooks/herdr-kiro.json` and the override `~/.config/herdr/agent-detection/kiro.toml` are written by `install.sh`. The build command and the `install` action run that script. Startup runs `startup.sh`, which runs `install.sh` only when the lifecycle marker says `installed`.

- `plugin link` does not run `[[build]]`. Startup does not run on link, enable, or config reload. It runs when a server starts, and only if the lifecycle marker says `installed`.
- A startup failure does not stop the herdr server. The result is in `herdr plugin log`.
- Nothing in the plugin sets `chat.agentEngine` or `chat.enableWorkflows`.
- Nothing restarts a running `kiro-cli`. Kiro reads hooks when its process starts.
- The local `kiro.toml` replaces the remote Kiro manifest. Herdr 0.9.5 does not merge the two. The diff against upstream `2026.09.19.1` is `docs/kiro-detection-diff.md`.
- Windows is not a declared platform.
- herdr 0.9.5 has no uninstall or unlink lifecycle command. `herdr plugin uninstall` and `herdr plugin unlink` only unregister the plugin. They leave the hook and `kiro.toml` in place. `./uninstall.sh` removes a file only when this package still owns it. A restored user file stays. If no herdr server is running, unlink is recorded as `pending_unlink`. That happens when `plugin list` still shows the plugin, when it exits non-zero, when the error text is `server_not_running`, or when the API socket is missing. The next `startup.sh` finishes the unlink once `plugin list` succeeds and the plugin is gone. Until then, doctor exits non-zero and says the plugin is still registered. doctor does not unlink it and does not delete files. `~/.config/herdr/.plugins.lock` is herdr's own lock. Leaving it behind on uninstall is acceptable.

The script writes `~/.kiro/hooks/herdr-kiro.json`. The command is passed through `json.dumps`, and the path is shell-quoted, so a checkout path can contain spaces, quotes, and backslashes. The first time it replaces a user file it copies that file to `herdr-kiro.json.original` and records a sha256 of the bytes it wrote in `herdr-kiro.json.herdr-kiro-owned`. A later install does not replace `.original`. If the installed file was edited, that copy is saved under a new `herdr-kiro.json.user-modified.<timestamp>` name before the new bytes are written. A file is treated as ours only when its bytes match the payload being installed. A marker that says the file is owned but has no hash does not overwrite a hand edit. The first time, install keeps one `.user-modified.<timestamp>` copy and leaves the hand edit in place. A later install does not add another copy of the same bytes and does not print `Installed` for that file. If no herdr server is running, install still exits 0 and reload happens the next time herdr starts. If `HERDR_SESSION` names a server that is not running, install exits 2 and says that session is not a running herdr server. A failed reload against a running server exits nonzero. Restart Kiro after installing it. Start Kiro in V3 mode with workflows enabled:

```bash
kiro-cli settings chat.agentEngine v3
kiro-cli settings chat.enableWorkflows true
kiro-cli chat --v3
```

`chat.agentEngine v3` is the saved default. `--v3` selects the V3 engine for that process. This repo was tested with Kiro CLI 2.37.0. The official installer on 1 Nov 2026 did not offer a separate 3.0.0 package. The V3 welcome screen in 2.37.0 still says CLI 3.0 launches at the end of October.

Inside a herdr pane the hook sees `HERDR_ENV` and `HERDR_PANE_ID`. Outside herdr it exits without doing anything.

State and the hook log live in `$XDG_STATE_HOME/herdr-kiro` or `~/.local/state/herdr-kiro`, mode `0700` for the directory and `0600` for the files. The log is trimmed after 200KB. A separate `.seq` file keeps the highest sequence. Rewriting the state file with a lower `seq` does not move the next report backwards, which is what made herdr ignore the hook after a reset.

```bash
./uninstall.sh
# or: herdr plugin action invoke herdr-kiro.uninstall
```

That removes a hook file this repo still owns and moves `.original` back. A file whose bytes no longer match the stored hash is renamed to a new `.user-modified.<timestamp>` file instead of being deleted. Uninstall does not signal a watcher whose start time was never recorded. It stops only the watchers recorded for this user under the state directory's `pids` files. It leaves `*.seq` in place so the next install does not restart the sequence at 1. It also removes the Kiro detection override when that file is the one this repo installed. When `HERDR_SESSION` is set, the reload goes to that session.

## Check

```bash
./doctor.sh
python3 -m unittest tests.test_grace tests.test_install tests.test_detection tests.test_doctor
tests/run_py39.sh
python3 harness/run_suite.py
```

GitHub Actions workflow `.github/workflows/test.yml` runs the unit tests on Ubuntu and macOS, on Python 3.9 and 3.12. It installs the herdr 0.9.5 release binary after checking its sha256, so the detection tests run there. The workflow does not run until this repository is published.

The suite needs a herdr session named `kiro-repro`, a Kiro pane, and `KIRO_API_KEY` in that pane's environment. It runs sequential, parallel, repeat, and watch workflows, plus one workflow Kiro starts from a normal prompt (`solo`). Poll logs land in `logs/`. A run passes when the pane stays working from the first banner sample until the last Stop, then stays idle, and the first of four idle samples in a row is within 3.5 seconds of the later of the last banner sample and the last Stop. After each run the suite checks that no `herdr_kiro_state.py --watch` process is left. Stop lines for the run are copied to `logs/stops-<name>.txt`.

A named session that never sends Stop is dropped after 30 minutes, or sooner when its Kiro process has exited. A hook payload with no session id shares one slot for the pane. Each Kiro hook is a new process, so a parent pid cannot identify the session. That slot ends on Stop, when the idle prompt has been up with no banner and no tool for 2 seconds, or when the outer `kiro-cli` exits, and then the 0.5 second grace runs. It does not accumulate one busy entry per event. `install.sh` also writes `~/.config/herdr/agent-detection/kiro.toml`. That file is a local detection override. Herdr 0.9.5 loads it without a rebuild. A visible `▶ name running … ctrl+x` banner is working, at a higher priority than the idle prompt. `ctrl+g` may be on the next line or clipped at a narrow width. The approval chooser footer and the subagent widget (`tool approval pending` with Approve all pending) are a higher priority still, so a banner above either one stays blocked. The hook reports `blocked` while that text is on screen and does not send `working` over it. `herdr server reload-agent-manifests` picks the file up in a running server. A failed reload is printed.

herdr 0.9.5 does not merge a local file with the remote manifest. The file in this repo is a full copy of the Kiro rules from `2026.09.19.1`, plus the banner and blocker rules. The rule-by-rule diff is `docs/kiro-detection-diff.md`. While it is installed, herdr will not apply newer remote Kiro rules. There is no narrower override in this version. Uninstall restores the previous file, and the next manifest refresh can fetch remote rules again.

Kiro CLI 2.37.0 documents `Stop` as the hook that runs when an agent execution completes. It also accepts `SessionEnd`, and the hook loader describes that trigger as loaded-session runtime teardown, not as another per-turn stop. The installed hook listens for both. `Stop` ends the turn. `SessionEnd` ends that session's slot the same way. Neither one is a process-exit notification. The watcher also waits on the outer `kiro-cli` pid with `kqueue` `EVFILT_PROC`/`NOTE_EXIT` on macOS and `pidfd` on Linux, and falls back to `kill -0`. When that pid exits, the watcher releases immediately. One `agent_not_found` while the pid is still alive does not end the run.

A draft bug report for the workflow-monitor file-write chooser is `docs/herdr-bug-workflow-monitor-fs-write.md`. It is not filed.

The license is the WTFPL, version 2, in `LICENSE`, copyright njs14. Upstream herdr is Apache-2.0. This package does not copy herdr source. The detection file is a local override derived from the published `kiro.toml` for version `2026.09.19.1`. install and doctor warn when the cached remote version is newer than that, and they print `local_override_shadowing_remote` in that warning. A matching version does not warn, even though the override shadows the remote file.

The idle-prompt rule in that copy sets `visible_idle` to false. herdr then holds a working-to-idle change for a few hundred milliseconds instead of publishing it on the first frame. That hold is what keeps a one-frame idle prompt under the banner from flipping the pane. Plain chat therefore reaches idle a little later than the stock rule. Turning the flag back on makes that one frame publish immediately, which a 0.4 second poll can catch.
