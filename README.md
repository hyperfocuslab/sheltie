# Sheltie server

Mac-side server for the **Sheltie** iOS app. Sheltie watches Claude Code agents running under [herdr-core](https://herdr.dev) (`hc`) and lets you answer them from your iPhone: permission prompts, questions and plan approvals show up as notifications with Allow / Always allow / Deny buttons, so you can reply from the lock screen without opening the app.

Not affiliated with herdr or Anthropic.

## Requirements

- macOS with `python3` (3.9+)
- herdr-core (`hc`) running your Claude Code agents
- [Tailscale](https://tailscale.com) on the Mac and the iPhone. The server listens on `127.0.0.1` and is exposed only inside your tailnet with `tailscale serve`. It never uses Funnel.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/hyperfocuslab/sheltie/main/install.sh | sh
```

The installer copies the server to `~/.local/share/herdr-remote/`, creates a token in `~/.config/herdr-remote/` (mode 0600), registers a launchd agent, runs `tailscale serve --bg --https=8790 http://127.0.0.1:8790`, checks `/api/health`, and prints a QR code. Scan it with the Sheltie app.

Options: `--dry-run` prints what it would do. `--uninstall` removes the launchd agent and the `tailscale serve` entry and keeps your config.

Show the pairing QR again:

```sh
python3 ~/.local/share/herdr-remote/herdr_remote.py pair
```

## What it can do

The server calls `hc` with fixed argument lists. It has no endpoint that runs arbitrary shell commands. The phone can:

- list terminals and read their screens
- answer permission, question and plan menus (the menu is re-read right before any key is sent; a changed menu returns 409)
- send text and a fixed set of keys
- read the Claude Code session transcript (`~/.claude/projects/*.jsonl`, read-only)
- start `claude` in a folder under your home directory

Every request needs the bearer token.

## Tests

```sh
cd server && python3 -m unittest -q tests.test_herdr_remote tests.test_install
```

## License

MIT
