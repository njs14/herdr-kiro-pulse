# Verify the herdr Kiro hook

Do not print `KIRO_API_KEY`. Do not write it into logs. `harness/run_suite.py` writes under `logs/`, and that directory is gitignored.

Kiro CLI 3.0 is the supported CLI. Interactive `kiro-cli chat` starts on V3. A saved `chat.agentEngine` of `v2` still selects V2. A headless run that omits `--trust-all-tools` stays on V2, so `harness/run_suite.py` passes that flag. The approval check is interactive and does not pass it. `hc --json get` reports `app` `kiro-cli`. The hook writes that app. A status whose app is `kiro` is still this program.

## What passing looks like

`python3 -m unittest tests.test_grace tests.test_install tests.test_detection tests.test_doctor tests.test_redaction tests.test_osc` exits 0. `tests.test_osc` proves the OSC 7501 bytes on a pty, including that a finished turn stays `state=done` and that a focused Agents row stays under Done until it is left. It does not launch the Herdr App. A live App check is a Kiro session inside an App terminal. `hc --json get` status `app` is `kiro-cli` or `kiro`. The painted sidebar check selects another tab. It uses `echo` or `true` when a tool approval is required. Do not pass `--trust-all-tools`.

`tests/run_py39.sh` runs those tests on Python 3.9. The hook uses `from __future__ import annotations`, so `int | None` is not evaluated at import.

Cases that call `herdr` fail when `herdr` is not on `PATH`. CI installs herdr 0.9.5. CI also runs `uvx ruff check`, `uvx ruff format --check`, and `uvx ty check`.

`python3 harness/run_suite.py` is the live workflow check. It needs a session, a Kiro pane, and `KIRO_API_KEY` in that environment. A passing case has no `error` key, and:

- every sample taken while the workflow banner is up is `working`
- the first of four idle samples in a row is within 3.5 seconds of the later of the last banner sample and the last Stop
- `logs/ps-<name>.txt` says no `herdr_kiro_state.py` watcher is left
- `logs/stops-<name>.txt` has the Stop lines for that run

`tests/test_detection.py` feeds screens to `herdr agent explain`. A workflow banner is working. The same banner above the chooser footer is blocked. Phrases such as "always allow" and "esc to close" in ordinary text stay idle. The workflow-monitor file-write chooser, with `requires approval` and both `Allow once` and `Deny once`, is `workflow_requires_approval`.

## Install and uninstall

```bash
./install.sh
```

With herdr on `PATH` and no server running, that exits 0. Stdout is `Installed` plus the absolute path of the hook, `Installed` plus the absolute path of the detection file, and `Restart Kiro CLI so it loads the hook. Start it with: kiro-cli chat`. The default paths are `$HOME/.kiro/hooks/herdr-kiro.json` and `$HOME/.config/herdr/agent-detection/kiro.toml`. `$KIRO_HOOKS_DIR` and `$XDG_CONFIG_HOME` replace those defaults when set. Stderr is `note: no herdr server is running; reload happens on the next herdr start`. If `herdr` is not on `PATH`, stderr is `herdr is not on PATH, skipped manifest reload` and the script still exits 0. If `HERDR_SESSION` names a session that is not running, the script exits 2.

The hook file is `json.dumps` with a shell-quoted command. The first user file it replaces is saved as `*.original`. The sha256 of the bytes written is stored in the `*.herdr-kiro-owned` marker. An older marker with no hash does not overwrite a hand edit. Install saves a timestamped copy and leaves the edited file in place when the marker has no hash. When the marker hash does not match, install copies the edited file to `*.user-modified.<timestamp>` and then writes the package file.

It also installs `~/.config/herdr/agent-detection/kiro.toml`. Herdr 0.9.5 loads that path as a local manifest override and does not merge it with the remote file. The copy is the Kiro rules from version `2026.09.19.1` plus the rules in `docs/kiro-detection-diff.md`. Every blocked rule in the copy, including the question panel, the trust-all dialog, and the `enter to see more options` footer, is above 1200 so a banner cannot hide a blocker. The idle-prompt rule sets `visible_idle = false`. herdr then holds a working-to-idle change for a few hundred milliseconds. While this file is installed, newer remote Kiro rules are not applied. Uninstall restores the previous file. `herdr --session <name> server reload-agent-manifests` loads it into a running server.

`herdr plugin link` plus `herdr plugin action invoke herdr-kiro.install` runs the same script. `./doctor.sh` is the status command. Install also runs `prototype/herdr_kiro_doctor.py --warn-upstream`. That prints on stderr when the cached remote Kiro manifest is newer than `2026.09.19.1`, and install still exits 0.

`./uninstall.sh` deletes a file only when the stored hash still matches. A changed file is renamed to a new timestamped `*.user-modified` name, and `*.original` moves back when that name is free. Uninstall reloads with `herdr --session "$HERDR_SESSION"` when that variable is set. No running server is a successful install with a note. A failed reload against a running server exits nonzero. A file with no marker, or a marker with no hash, is left unchanged. doctor classifies files with the same check. Doctor removes a dead watcher pid file. `./uninstall.sh` signals a watcher when the pid file records this uid, the `hires_start` token, and the comm basename, and the live process still matches. Where `/proc` start ticks are absent, including on Linux, `hires_start` stores `sec:` plus the whole-second lstart. Uninstall does not signal a watcher recorded with that unproven form, or with a bare whole-second lstart. Uninstall leaves `*.seq` so the next run does not restart the sequence at 1. A fresh state file with no floor starts at the current unix time instead of 1.

Workflows have to be on for the live suite:

```bash
kiro-cli settings chat.enableWorkflows true
```

Nothing in this package writes that setting.

## Stripped session ids

Kiro starts each hook in a new process. A parent pid is different on every event, so it cannot join a Start to its Stop. When `session_id` is missing, every event for the pane shares one aggregate slot. The slot ends when the SessionStart count returns to zero, when the idle prompt has stayed up with no banner and no tool for 2 seconds, or when the outer `kiro-cli` process exits. The 0.5 second grace follows only the SessionStart count returning to zero. A named Stop, a SessionEnd, a Stop whose count is already zero, and a workflow banner do not start that grace. That idle clear waits `IDLE_CLEAR_SECS` and sets `grace_until` to 0. `IDLE_DELAY` is also 2 seconds and only gates the watcher exit; that idle exit does not release the pane. An outer exit does not wait the 0.5 seconds. Busy count cannot grow with the event count. A second SessionStart does not end the slot by itself.

`harness/strip_session_id.py` is the live probe. It strips `session_id` and runs the real hook as a child. Point `HERDR_KIRO_REAL_HOOK` at `prototype/herdr_kiro_state.py`.
