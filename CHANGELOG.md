# Changelog

## Unreleased

The hook reports a Kiro CLI V3 session to the Herdr App with OSC 7501 on the pane tty. `hc` is herdr-core and has no status-set command. A finished turn is `state=done` on the App and still `idle` on the Herdr TUI. The TUI report is not followed by `release-agent` while kiro-cli is running. Question-panel rules use `kind=question`. Other blockers use `kind=permission`.

## 0.13.2

A sidecar is trusted only when `lstat` shows a regular file owned by this uid that is not group- or world-writable, the protected file is a regular file owned by this uid, and the read uses `O_NOFOLLOW`. A symlink or another user's sidecar does not make uninstall delete the file. Pane ids that start with `-` are rejected. startup refuses to chmod a symlinked state directory. The watcher test accepts the macOS `sec:<epoch>` start token. Two sample logs no longer use a `/workspace` path prefix.

## 0.13.1

Uninstall deletes a temp file only when its sidecar marker names this uid, and it no longer treats `HERDR_KIRO_LEGACY_DIR` as a sweep root. A hook or detection file is removed only when the marker sha256 matches. A different hash is renamed to `*.user-modified.<timestamp>` and `*.original` is restored. No marker, or a marker with no hash, leaves the file unchanged. doctor uses that same check. Install creates each file with `O_CREAT|O_EXCL|O_NOFOLLOW` mode 0600 and refuses a symlink destination. Pane ids must match `^[A-Za-z0-9._:-]{1,64}$` with no `..`, and `--watch` opens a path only when its realpath is that pane's file inside the state directory. Suite output is passed through `demo_live.scrub`, and the run fails if `KIRO_API_KEY` remains. Explain screen text is written in the 0700 state directory. startup sets that directory to mode 0700. Watcher pid files record a sub-second start token and the comm basename. Rules directories are removed with an fd walk that does not follow symlinks. Sample captures and the prototype hook no longer embed an absolute home or workspace path.

## 0.13.0

Quoted trust-dialog prose stays working while a workflow is running. The workspace-trust rule no longer treats a banner as absent just because the status bar and the prompt sit below it. The title and the option rows are at most 20 lines apart, the rows end the screen, and a `>` quote or a 4-space indent is not the selection marker. The real dialog still matches when the footer or the revoke line wraps.

## 0.12.0

The workspace-trust rule matches the dialog title and the two option rows when the revoke hint or the footer wraps. A workflow banner as the last line is not that dialog. The approval chooser and the subagent widget still match when a phrase wraps at a narrow width. A watcher whose slot is held by a dead pid exits on that check. Uninstall records `pending_unlink` when `plugin list` exits non-zero or the herdr socket is missing, not only when the error text is `server_not_running`. Legacy cleanup looks in both `$TMPDIR` and `/tmp`. Rules directories use the same uid and marker proof as the sweep.

## 0.11.0

The temp sweep only removes a `herdr-kiro-rules-<pid>-<token>` directory that contains this uid's owner marker. A user directory such as `herdr-kiro-notes` is left alone. The workspace-trust rule matches the captured dialog, not two quoted option lines under a workflow banner. A watcher whose owner has exited leaves even when another pid holds the slot.

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
