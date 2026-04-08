# 🦀 ClawX

**Give your Claude Code a soul — persistent identity, memory, and autonomy.**

ClawX is a thin PTY wrapper + soul framework for [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code). A set of tiny config files that give Claude Code persistent identity, memory, heartbeat, and scheduled tasks — all within the official CLI.

## Why ClawX?

We originally used [OpenClaw](https://github.com/nichochar/openclaw) — a great project for giving AI agents persistent identity and memory. But OpenClaw moved to a subscription model, making it inaccessible for many users.

So we built ClawX: a lightweight, self-hosted alternative that achieves the same goal — persistent AI identity, memory, and autonomy — using nothing but the official Claude Code CLI and a handful of markdown files. No subscription, no cloud dependency, no vendor lock-in. Just `python clawx.py` and you're running.

![ClawX Demo](demo.png)

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

## Quick Start

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# Optional: for scheduled tasks
pip install apscheduler

# Start — looks exactly like `claude` but with superpowers
python clawx.py
```

On first run, the agent will introduce itself and ask who you are. Just talk naturally.

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

### config.json

```json
{
  "claude": {
    "command": "claude",
    "project_dir": "./",
    "model": "opus",
    "permission_mode": null,
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

| Field | Description |
|-------|-------------|
| `model` | Claude model (`opus`, `sonnet`, `haiku`) |
| `permission_mode` | `null` = skip permissions (default), `"default"` = normal mode |
| `extra_args` | Additional CLI args (e.g. `--channels` for Telegram) |
| `schedule` | Cron jobs that inject prompts on schedule |

### Telegram Integration

Add `--channels plugin:telegram@claude-plugins-official` to `extra_args` (included by default). See `CLAUDE.md` for full setup.

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

## License

MIT
