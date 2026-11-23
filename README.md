# Sheltie server

Server for the **Sheltie** iOS app. It runs on a Mac or a Windows PC. Sheltie lets you answer your coding agents from your iPhone: permission prompts, questions and plan approvals show up as notifications with Allow / Always allow / Deny buttons, so you can reply from the lock screen without opening the app.

It works two ways:

- **In any terminal (no herdr needed).** The installer adds Sheltie hooks to Claude Code and Codex. Run `claude` or `codex` as usual in Terminal, iTerm, VS Code or Windows Terminal, and approvals, questions and plan approvals reach your phone.
- **Under [herdr-core](https://herdr.dev) (`hc`).** You also get the live terminal screen, messages and keys from the phone, starting new agents, files and attachments, and the other agents (Gemini CLI, Qwen Code, OpenCode, Grok Build, GitHub Copilot CLI, Cursor Agent, Amp).

**Get the iPhone app:** [Sheltie on the App Store](https://apps.apple.com/app/id6837271201)

Not affiliated with herdr or Anthropic.

## Requirements

- The [Sheltie app](https://apps.apple.com/app/id6837271201) on your iPhone (iOS 18+)
- macOS with `python3` (3.9+), or Windows 10/11 (x64) with Python 3.9+ from [python.org](https://www.python.org/downloads/)
- Claude Code or Codex. herdr-core (`hc`) is optional, see above
- [Tailscale](https://tailscale.com) on the computer and the iPhone. The server listens on `127.0.0.1` and is exposed only inside your tailnet with `tailscale serve`. It never uses Funnel.

## Install

### macOS

```sh
curl -fsSL https://raw.githubusercontent.com/hyperfocuslab/sheltie/main/install.sh | sh
```

The installer copies the server to `~/.local/share/sheltie/`, creates a token in `~/.config/sheltie/` (mode 0600), registers a launchd agent, runs `tailscale serve --bg --https=8790 http://127.0.0.1:8790`, checks `/api/health`, and prints a QR code. Scan it with the Sheltie app.

Options: `--dry-run` prints what it would do. `--uninstall` removes the launchd agent, the `tailscale serve` entry and the Sheltie hooks, and keeps your config. `--no-hooks` skips the Claude Code and Codex hooks.

Show the pairing QR again:

```sh
python3 ~/.local/share/sheltie/sheltie.py pair
```

### Windows

Run this in PowerShell as your normal user. To use herdr-core, install it first (`irm https://releases.herdr.dev/core/install.ps1 | iex`).

```powershell
irm https://raw.githubusercontent.com/hyperfocuslab/sheltie/main/install.ps1 | iex
```

The installer copies the server to `%LOCALAPPDATA%\sheltie\`, creates a token in `%USERPROFILE%\.config\sheltie\`, registers a Task Scheduler task (`Sheltie`, runs at logon without a console window), runs the same `tailscale serve` command, checks `/api/health`, and prints a QR code.

To uninstall, set `$env:SHELTIE_UNINSTALL=1` before the same command. With a local copy, run `powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall` (or `-DryRun`).

Show the pairing QR again:

```powershell
python "$env:LOCALAPPDATA\sheltie\sheltie.py" pair
```

## Hooks

The installer adds a hook block to `~/.claude/settings.json` and `~/.codex/hooks.json`, backs up the original, and leaves your other hooks alone. Uninstalling removes only Sheltie's block.

- Restart sessions that were already running. Hooks are read when a session starts.
- Codex asks you to trust new hooks once: run `/hooks` in Codex and choose Trust all.
- Codex has no Always allow, so the phone shows Allow and Deny only.
- If you don't answer within about 9 minutes, the prompt falls back to the terminal.
- Hook sessions are answer-only: the live screen, messages and keys need herdr-core.

## What it can do

The server calls `hc` with fixed argument lists and answers the hooks above. It has no endpoint that runs arbitrary shell commands. The phone can:

- list terminals and read their screens
- answer permission, question and plan menus (the menu is re-read right before any key is sent; a changed menu returns 409)
- send text and a fixed set of keys
- read the Claude Code session transcript (`~/.claude/projects/*.jsonl`, read-only)
- start `claude` in a folder under your home directory

Every request needs the bearer token.

## Tests

```sh
cd server && python3 -m unittest discover -s tests
```

## License

MIT
