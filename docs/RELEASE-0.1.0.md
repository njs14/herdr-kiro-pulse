# herdr-kiro 0.1.0

Release notes for tag `0.1.0`. Keep the GitHub Release body in sync with this file.

herdr-kiro reports a Kiro CLI V3 session to the Herdr App with OSC 7501 on the pane tty, and still reports the Herdr TUI with `herdr pane report-agent`.

## Supported CLIs

- Kiro CLI 3.0 and 3.1
- Interactive `kiro-cli chat` starts on V3
- A saved `chat.agentEngine` of `v2` still selects V2
- On 3.1 each hook runs under `node` and `bun` below the outer `kiro-cli`; there is no `kiro-cli-chat`

## App and TUI status

- Running turn: `state=working`
- Chooser or workspace-trust dialog: `state=blocked` with `kind=permission`
- Question panel: `state=blocked` with `kind=question`
- Finished turn: `state=done` on the App, `idle` on the Herdr TUI
- The OSC word stays `done` after the pane is seen
- The hook writes `app=kiro-cli` (a status whose app is `kiro` is still this program)

## macOS / hosts without `/proc` start times

- The hook does not record an owner when the outer start is only whole seconds
- Stop ends the turn and clears the plugin pane state
- In an App terminal the hook still paints OSC by resolving the live outer `kiro-cli` from the hook parent chain at paint time; that pid is never stored as an owner
- App terminals do not call `herdr report-agent`, `herdr release-agent`, or `herdr agent get`
- A mid-turn kill of a turn this version recorded can leave that session busy until the pane state file is deleted

## Other

- `PreCompact` / `PostCompact` are not registered (Kiro sends no Stop after `/compact`)
- Uninstall restores your previous file modes
- Requires herdr 0.9.5 or newer
