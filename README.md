# 🦞 ClawX

**Give your Claude Code a soul — persistent identity, memory, and autonomy.**

ClawX is a thin PTY wrapper + soul framework for [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code). A set of tiny config files that give Claude Code persistent identity, memory, heartbeat, and scheduled tasks — all within the official CLI.

![ClawX Demo](demo.png)

## Quick Start

### With [uv](https://docs.astral.sh/uv/) (recommended — zero setup)

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# uv reads PEP 723 metadata in clawx.py and auto-installs deps
uv run clawx.py
```

uv creates an isolated venv, installs `apscheduler`, and runs ClawX — all in one command. No `pip install`, no virtualenv juggling. First run takes a few seconds; subsequent runs are cached and start in <200ms.

Don't have uv yet? Install it once:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### With plain Python

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# Optional: for scheduled tasks
pip install apscheduler

# Start — looks exactly like `claude` but with superpowers
python clawx.py
```

On first run, the agent will introduce itself and ask who you are. Just talk naturally.

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                        ClawX                                │
│                                                             │
│   ┌─────────────┐    ┌──────────────────────────────────┐   │
│   │  clawx.py   │───>│  Claude Code CLI (PTY)           │   │
│   │  ─────────  │    │  ┌────────────────────────────┐  │   │
│   │  Scheduler  │    │  │ CLAUDE.md (bootstrap)      │  │   │
│   │  FIFO inject│    │  │   └→ BOOTSTRAP.md (1st run)│  │   │
│   │  Auto-restart    │  │   └→ AGENTS.md             │  │   │
│   │  Transcript │    │  │       └→ SOUL.md (who I am)│  │   │
│   └─────────────┘    │  │       └→ USER.md (who you) │  │   │
│                      │  │       └→ IDENTITY.md       │  │   │
│   ┌─────────────┐    │  │       └→ MEMORY.md         │  │   │
│   │ mono.fifo   │───>│  │       └→ memory/*.md       │  │   │
│   │ (injection) │    │  │   └→ HEARTBEAT.md          │  │   │
│   └─────────────┘    │  └────────────────────────────┘  │   │
│                      └──────────────────────────────────┘   │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  Channels: Telegram / Discord / ...                 │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### The Boot Sequence

```
First run:
  CLAUDE.md → BOOTSTRAP.md → Conversation → fills IDENTITY.md + USER.md → deletes BOOTSTRAP.md

Every session after:
  CLAUDE.md → AGENTS.md → SOUL.md + USER.md + IDENTITY.md + memory/ → HEARTBEAT.md → alive
```

1. **First run** — `BOOTSTRAP.md` guides a conversation where you and the agent figure out its name, personality, and vibe together. It fills in `IDENTITY.md` and `USER.md`, then deletes itself.
2. **Every session** — Claude reads `AGENTS.md`, which loads its soul (`SOUL.md`), your info (`USER.md`), identity (`IDENTITY.md`), and recent memory. The agent wakes up knowing who it is, who you are, and what happened.
3. **Heartbeat** — Periodic checks (disk space, crypto prices, calendar, etc.) run automatically via `HEARTBEAT.md`.
4. **Scheduled tasks** — Cron jobs inject prompts at set times (morning reports, reminders, etc.).

### The PTY Wrapper

ClawX runs Claude Code in a pseudo-terminal. You get the **exact same interactive UI** — colors, progress bars, animations — plus:
- **FIFO injection** — send prompts from any terminal: `echo "hello" > mono.fifo`
- **Scheduled injection** — apscheduler fires prompts on cron schedules
- **Auto-restart** — if Claude crashes, ClawX brings it back
- **Transcript logging** — full session saved to `logs/`

## Project Structure

```
ClawX/
├── clawx.py              # PTY wrapper + scheduler
├── config.json           # Launch & schedule config
│
├── CLAUDE.md             # Bootstrap entry point
├── BOOTSTRAP.md          # First-run ritual (self-deleting)
├── AGENTS.md             # Agent behavior rules & memory system
├── SOUL.md               # Agent personality & values
├── IDENTITY.md           # Agent identity card (name, emoji, vibe)
├── USER.md               # About your human
├── TOOLS.md              # Environment-specific notes
│
├── HEARTBEAT.md          # Periodic check items
├── heartbeat-config.json # Heartbeat interval & quiet hours
├── MEMORY.md             # Long-term memory index
└── memory/               # Daily memory logs
```

### CLI Commands

```bash
python clawx.py                    # Start (PTY passthrough)
python clawx.py inject "message"   # Inject into running session
python clawx.py prompt "message"   # One-shot (separate process)
python clawx.py stop               # Stop running session
```

### Injecting Messages

While ClawX is running, from another terminal:

```bash
# Via FIFO (simplest)
echo "run morning report" > mono.fifo

# Via CLI
python clawx.py inject "run morning report"
```

## Configuration

Everything ClawX needs lives in one file: `config.json`. Three sections — `claude` (how to launch Claude), `session` (auto-restart behavior), `schedule` (cron jobs).

```json
{
  "claude": {
    "command": "claude",
    "project_dir": "./",
    "model": "opus",
    "permission_mode": null,
    "resume_last": false,
    "mcp_config": null,
    "extra_args": ["--channels", "plugin:telegram@claude-plugins-official"]
  },
  "session": {
    "auto_restart": true,
    "max_restart_attempts": 3,
    "restart_delay_seconds": 5,
    "health_check_interval": 300
  },
  "schedule": {
    "heartbeat": {
      "enabled": true,
      "cron": "*/30 * * * *",
      "prompt": "Read HEARTBEAT.md if it exists. Follow it strictly."
    }
  }
}
```

### `claude` — how to launch Claude Code

| Field | Description |
|-------|-------------|
| `command` | Claude binary (`claude`, or absolute path like `/home/user/.local/bin/claude`). ClawX auto-resolves bare names via `$PATH` + fallbacks (`~/.local/bin`, `/usr/local/bin`). |
| `project_dir` | Working directory passed via `--add-dir`. Use `"./"` for the ClawX folder, or an absolute path to point at another project. |
| `model` | `opus`, `sonnet`, or `haiku` |
| `permission_mode` | `null` = skip permissions (default), `"default"` = normal mode, `"plan"` = plan mode |
| `resume_last` | If `true`, launches with `--continue` to resume the most recent session (memory carries over across restarts). |
| `mcp_config` | Path to an MCP config file, or `null` to skip. |
| `extra_args` | Any extra CLI args appended verbatim — e.g. `--channels plugin:telegram@claude-plugins-official` for Telegram. |

### `session` — auto-restart & health checks

| Field | Description |
|-------|-------------|
| `auto_restart` | If `true`, ClawX respawns Claude when it dies. |
| `max_restart_attempts` | Stop trying after this many failed restarts. |
| `restart_delay_seconds` | Wait this long before each restart. |
| `health_check_interval` | Seconds between liveness checks. Logged as "Health OK | uptime=… | restarts=…". |

### `schedule` — cron jobs (apscheduler)

Each entry runs on a cron schedule and injects its `prompt` into the live Claude session via the FIFO. Standard 5-field cron: `minute hour day month day_of_week`.

```json
"schedule": {
  "heartbeat": {
    "enabled": true,
    "cron": "*/30 * * * *",
    "prompt": "Read HEARTBEAT.md if it exists. Follow it strictly."
  },
  "morning_report": {
    "enabled": true,
    "cron": "0 8 * * *",
    "prompt": "Run the morning report."
  }
}
```

Set `enabled: false` to disable a job without deleting it. ClawX uses APScheduler — long-running, no 7-day expiry.

### Telegram

Add `--channels plugin:telegram@claude-plugins-official` to `extra_args` (already included in the default config). See `CLAUDE.md` for the bot/token setup.

## Setup Options

### A. Fresh start — use this repo directly

Clone, run `python clawx.py`, talk to your agent. `BOOTSTRAP.md` will guide a first-run conversation to set up identity and personality.

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX
pip install apscheduler  # optional, for scheduled tasks
python clawx.py
```

### B. Add ClawX to an existing project (has its own soul files)

If you already have a project with `SOUL.md`, `IDENTITY.md`, `MEMORY.md`, etc. (e.g. an OpenClaw workspace), you just need the wrapper and config:

```bash
# Copy only the wrapper + config into your project
cp clawx.py config.json /path/to/your-project/

# Edit config.json — set project_dir to "." (or leave as default)
# Adjust model, permission_mode, extra_args as needed

# If your project doesn't have CLAUDE.md yet, copy it too
cp CLAUDE.md /path/to/your-project/

# Start
cd /path/to/your-project
python clawx.py
```

Your existing soul files (`SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`, `memory/`, etc.) will be picked up automatically — no need to copy them again.

**Important:** Do NOT copy `BOOTSTRAP.md` into a project that already has identity files — it's only for first-run setup and would overwrite your existing identity.

### C. Bring an existing project into ClawX

If you want to use the ClawX repo as your workspace and bring in your existing soul files:

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# Remove BOOTSTRAP.md — you already have an identity
rm BOOTSTRAP.md

# Copy your soul files over (overwrite the templates)
cp /path/to/your-project/SOUL.md .
cp /path/to/your-project/IDENTITY.md .
cp /path/to/your-project/USER.md .
cp /path/to/your-project/MEMORY.md .
cp -r /path/to/your-project/memory/ ./memory/

# Copy any other files you need (AGENTS.md, TOOLS.md, HEARTBEAT.md, etc.)
# Then start
python clawx.py
```

### D. Point ClawX at a remote project directory

ClawX can live separately and point to your project via `config.json`:

```bash
# In config.json, set project_dir to your project's path
{
  "claude": {
    "project_dir": "/home/user/my-project",
    ...
  }
}
```

Claude will use `--add-dir` to load that directory. ClawX stays in its own folder, your project stays in its own folder.

## Philosophy

Traditional AI assistants are stateless — every conversation starts from zero. ClawX gives Claude Code:

- **Identity** — a name, personality, and values that persist
- **Memory** — daily logs + curated long-term memory
- **Autonomy** — heartbeats, scheduled tasks, proactive behavior
- **Relationships** — remembers who you are and how you work together

All built on tiny markdown files. No database, no cloud service, no subscription. Just files and the official Claude Code CLI.

## Development

### Running tests

```bash
pip install pytest apscheduler
python3 tests/test_clawx.py
```

19 tests covering smoke, FIFO injection, schedule registration, build_command flags, transcript logging, cleanup, and path resolution. All run in ~14 seconds against a mock claude binary.

### Pre-commit hook

Install once after cloning to auto-run tests before every commit:

```bash
bash scripts/install-hooks.sh
```

GitHub Actions also runs the suite on every push and PR — see `.github/workflows/test.yml`.

## License

MIT
