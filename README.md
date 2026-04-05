# ClaudexClaw

A Claude Code supervisor daemon — manages, monitors, and schedules long-running Claude Code sessions.

## Quick Start

```bash
# Install dependencies (required for scheduling)
pip install apscheduler

# Start daemon (auto-launches a Claude CLI session)
python clawx.py

# One-shot command (no daemon needed)
python clawx.py prompt "run morning report"

# Check status
python clawx.py status

# Stop
python clawx.py stop
```

## Architecture

```
ClaudexClaw (supervisor)
├── Lifecycle management: start / monitor / auto-restart Claude CLI
├── Scheduler: cron-based, session-independent (apscheduler)
├── Command injection: send prompts to a running session
└── Logging: all session output saved to logs/

Claude CLI (worker)
├── CLAUDE.md bootstrap
├── MCP plugins (Telegram, etc.)
└── Daily tasks & heartbeats
```

## Configuration: config.json

- `claude`: CLI path, project directory, model, permission mode, extra args (e.g. `--channels`)
- `session`: auto-restart strategy, health check interval
- `schedule`: cron jobs (morning reports, heartbeats, etc.)
- `logging`: log directory, size limits, rotation

## How It Works

ClaudexClaw launches Claude Code in `--print` mode with stream-JSON I/O, keeping a persistent interactive session alive. It monitors health, auto-restarts on crash, and injects scheduled prompts via stdin.

Unlike lightweight wrappers that spawn a new process per message, ClaudexClaw maintains a **long-running agent** with full context continuity.

## TODO

- [ ] IPC socket: allow `clawx.py send` to communicate with the running daemon
- [ ] Web dashboard: simple status page
- [ ] Context management: detect context window nearing limit, graceful restart
- [ ] Multi-session support: manage multiple agents simultaneously
- [ ] Windows service / systemd unit
