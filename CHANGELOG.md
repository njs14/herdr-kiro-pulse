# Changelog

## 0.10.0

Uninstall no longer treats herdr's plugin lock or a restored user file as ours. A unlink that cannot run until herdr has a server is finished by startup or doctor. The watcher leaves only when the recorded kiro-cli or kiro-cli-chat process has exited. The workspace-trust rule matches the two option rows, not a quotation of them.

## 0.9.0

License is the WTFPL, version 2, copyright njs14. `herdr plugin uninstall` and `herdr plugin unlink` still do not run a plugin command, because herdr 0.9.5 has no uninstall lifecycle. `./uninstall.sh` removes the hook and the detection override, writes an uninstalled marker, and unlinks the plugin when it is registered. Startup runs install only when that marker says installed. doctor exits non-zero for a hand edit, a dead pid file, a stopped server, a newer upstream Kiro manifest, an engine that is not v3, workflows that are off, or files left behind after unlink. The watcher stops instead of polling when the owner is dead and another watcher holds the slot. A reparented `kiro-cli-chat` counts as owner exit. The workspace-trust dialog is blocked.

## 0.8.0

The file-write chooser in the workflow monitor stays blocked, including when the monitor draws a box border before `Allow once`. The match is limited to the bottom of the screen and requires both choice rows, so a paragraph that mentions those words does not match. The watcher releases as soon as the Kiro owner process exits (`pidfd` on Linux, `kqueue` on macOS), and a single `agent_not_found` while that process is still alive does not end the run. Kiro 2.37.0's `SessionEnd` hook is installed as the session-teardown signal. `Stop` remains the per-turn signal. Install succeeds when no herdr server is running. A mistyped `HERDR_SESSION` is reported on its own and exits nonzero. A marker with no content hash does not overwrite a hand edit. The package is a herdr plugin (`herdr-plugin.toml`) and `./install.sh` remains the command that writes the Kiro hook and the detection override. `./doctor.sh` prints status. See `docs/kiro-detection-diff.md` for the override against herdr's `kiro.toml` `2026.09.19.1`.

## 0.7.0

The requires-approval chooser is a blocked rule. One `agent_not_found` no longer clears every slot. Failed explain results are not cached. `ps` uses the C locale.

## 0.6.0

The watcher exits when herdr reports that the agent is gone, and it stops reporting working over a dead pane's leftover banner.
