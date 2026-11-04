<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="herdr-kiro. A workflow pane stays working until the run ends. Three states: idle on the prompt, working under a running banner, blocked on Allow once.">
</p>

# herdr-kiro

A [herdr](https://herdr.dev) plugin that reports a [Kiro CLI](https://kiro.dev/docs/cli) V3 session to the Herdr App, and still to the Herdr TUI.

The App reads status only from OSC 7501 on the program's own output. `hc` is the herdr-core CLI. It can print a screen and paste input. It cannot set status. Inside an App terminal, core sets `HC_TERMINAL_ID`, and this hook writes one report to that pane's tty:

- a running turn is `state=working:app=kiro`
- a chooser or the workspace-trust dialog is `state=blocked:kind=permission:app=kiro`
- a question panel is `state=blocked:kind=question:app=kiro`
- a finished turn is `state=done:app=kiro`

`herdr-app ls` shows `blocked` as needing you. It shows `done` while the turn is unseen, and idle once `herdr.seen` is that turn. The painted Agents section is a separate hold: a focused row that was under Done stays under Done after that list status is idle, and selecting another tab in the same space moves the row to Idle. A finished turn stays `state=done` on the wire.

The Herdr TUI path is unchanged. `HERDR_ENV=1` and `HERDR_PANE_ID` still call `herdr pane report-agent` with `working`, `idle`, `blocked`, or `unknown`. A finished turn is an `idle` report. herdr 0.9.5 shows that unseen idle as done. The hook does not call `release-agent` while `kiro-cli` is running.

Stock Kiro detection reads the idle prompt and calls the pane idle, including when `▶ name running` is still painted above that prompt. On the TUI, this plugin reports the pane from the workflow and from those dialogs. herdr 0.9.5 does not merge a local detection file with the remote one, so installing this replaces upstream Kiro rules until you uninstall.

## Demo

One pane, twelve seconds. The prompt looks idle, the banner is still a running workflow, then `Allow once` blocks the pane.

![Twelve seconds: the prompt is idle, the banner is still running, then Allow once blocks the pane](demo/herdr-kiro-demo.gif)

The GIF above (960×540, 12 seconds) is [demo/herdr-kiro-demo.gif](demo/herdr-kiro-demo.gif). The source render is [demo/herdr-kiro-demo.mp4](demo/herdr-kiro-demo.mp4) (1920×1080, 30 fps). The three states, in order:

1. The idle prompt and no banner: the pane is idle.
2. `▶ two-step running` is on screen: the pane is working.
3. `Allow once` and `Deny once` are on screen: the pane is blocked, banner or not.

## Install

Requires herdr 0.9.5 or newer, `kiro-cli` on `PATH`, and Python 3.9 or newer. Platforms in `herdr-plugin.toml` are Linux and macOS.

```bash
herdr plugin install njs14/herdr-kiro-pulse
```

The manifest `build` step runs `install.sh`. It writes the Kiro hook to `~/.kiro/hooks/herdr-kiro.json` and the detection override to `~/.config/herdr/agent-detection/kiro.toml`, backs up any file of yours it would replace, and reloads agent manifests. It uses no sudo and no network. The `startup` hook re-applies it on each herdr start unless you uninstalled.

Manual install from a clone:

```bash
./install.sh
```

With herdr installed and no server running, that exits 0. Stdout:

```text
Installed ~/.kiro/hooks/herdr-kiro.json
Installed ~/.config/herdr/agent-detection/kiro.toml
Restart Kiro CLI so it loads the hook. Start it with: kiro-cli chat --v3
```

Stderr when nothing is listening: `note: no herdr server is running; reload happens on the next herdr start`. If `HERDR_SESSION` names a session that is not running, the script exits 2.

Register the same script as a plugin action:

```bash
herdr plugin link /path/to/this/repo --enabled
herdr plugin action invoke herdr-kiro.install
```

`plugin link` does not run `install.sh`. The action does. Linking also does not run the manifest `build` command.

Kiro reads hooks when its process starts. After install:

```bash
kiro-cli chat --v3
```

The hook was tested with `chat.agentEngine` set to `v3` and `chat.enableWorkflows` set to `true`. Nothing in this package writes those settings, and nothing restarts a `kiro-cli` that is already running. Outside an App terminal (`HC_TERMINAL_ID`) and outside a TUI pane (`HERDR_ENV=1` and `HERDR_PANE_ID`) the hook exits without reporting.

```bash
./doctor.sh
./uninstall.sh
```

`./doctor.sh` prints versions and whether the installed files still match the hashes this package wrote. It does not unlink the plugin and it does not delete files. `./uninstall.sh` removes the hook and `kiro.toml` only when this package still owns them, puts a saved `.original` back when one exists, and writes `uninstalled` in the lifecycle file. If the server is down it appends `pending_unlink=herdr-kiro`. The next `startup.sh` clears that line once `herdr plugin list` succeeds and the plugin is gone.

Rebuild the demo with Node.js 22, FFmpeg, and the `hyperframes` package pinned in `package.json`:

```bash
npm install
cd demo
npx hyperframes check
npx hyperframes render -o herdr-kiro-demo.mp4
ffmpeg -y -i herdr-kiro-demo.mp4 -vf "fps=12,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse" -loop 0 herdr-kiro-demo.gif
```

## Why not the stock Kiro rules

The stock idle rule matches `› ask a question or describe a task` on the bottom lines. A workflow leaves that prompt under the banner, so herdr shows idle for the rest of the run.

`prototype/kiro-detection.toml` is herdr's Kiro manifest `2026.09.19.1` plus rules that sit above that prompt:

- A line that starts with `▶`, then a name, then `running`, and contains `ctrl+x`, is working. `ctrl+g` may be on the next line.
- `Allow once` / `Deny once`, the subagent widget, and `Trust this workspace?` are blocked, and they beat the banner.
- The hook releases the pane when the recorded `kiro-cli` or `kiro-cli-chat` process has exited. `Stop` and `SessionEnd` end a turn or a session. They are not process exit.

Stay on the stock file if you want herdr to keep applying newer remote Kiro rules. Switch to this if the idle pane during a workflow is the failure you are fixing. The rule-by-rule list is [docs/kiro-detection-diff.md](docs/kiro-detection-diff.md).

## Status

Version 0.1.0, from `herdr-plugin.toml`. Actively changed. Not a stable API, and not published.

Unit tests on this tree: 168 passed, 1 error, on Python 3.15.2, with herdr 0.9.5. The command is `python3 -m unittest tests.test_grace tests.test_install tests.test_detection tests.test_doctor tests.test_redaction tests.test_osc`. `tests.test_osc` writes OSC 7501 to a pty and reads the bytes back, and it keeps a finished turn at `state=done`. It does not launch the Herdr App. `tests/run_py39.sh` is the 3.9 run. CI also runs `uvx ruff check`, `uvx ruff format --check`, and `uvx ty check`. The rule set is in `pyproject.toml`. Live notes from herdr 0.9.5 and Kiro CLI 2.37.0 are in [VERIFY.md](VERIFY.md). A live App check is a Kiro session inside an App terminal, and it is not this suite. Release notes are in [CHANGELOG.md](CHANGELOG.md).

`python3 harness/run_suite.py` drives workflows in a herdr pane. It needs a session, a Kiro pane, and `KIRO_API_KEY` in that environment. Do not print the key.

## Licence

[Do What the Fuck You Want to Public License, version 2](LICENSE). Copyright (C) 2026 njs14.

Upstream herdr is Apache-2.0. This package does not copy herdr source. The detection file is a local override of the published `kiro.toml` for version `2026.09.19.1`.
