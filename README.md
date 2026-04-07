# 🦀 ClawX

A thin PTY wrapper around Claude Code CLI — same UI, with superpowers.

ClawX runs Claude Code in a pseudo-terminal so you get the **exact same interactive experience**, plus:
- **Message injection** via FIFO pipe — send prompts from any terminal
- **Scheduled tasks** via cron (apscheduler) — heartbeats, morning reports, etc.
- **Auto-restart** on crash
- **Transcript logging** — everything saved to file

## What's Inside

```
ClawX/
├── clawx.py              # PTY wrapper + scheduler
├── config.json           # Launch & schedule config
├── CLAUDE.md             # Bootstrap — the entry point that boots everything
├── AGENTS.md             # Agent behavior rules & memory system
├── SOUL.md               # Agent personality & values
├── USER.md               # About your human (fill this in)
├── HEARTBEAT.md          # Periodic check items
├── MEMORY.md             # Long-term memory index
├── memory/               # Daily memory logs
├── README.md             # English docs
└── README_zh.md          # 中文文件
```

## How It Works

ClawX spawns Claude Code in a PTY (pseudo-terminal), so Claude thinks it's running in a real terminal. Everything renders exactly as if you ran `claude` directly — colors, progress bars, tool animations, all of it.

On top of that, ClawX can **inject text** into Claude's input at any time:

```
┌──────────────────────────────────────────┐
│  Your Terminal                           │
│  ┌────────────────────────────────────┐  │
│  │  ClawX (PTY wrapper)              │  │
│  │  ┌──────────────────────────────┐  │  │
│  │  │  Claude Code CLI             │  │  │
│  │  │  (interactive, full UI)      │  │  │
│  │  └──────────────────────────────┘  │  │
│  │       ↑ inject via FIFO / cron     │  │
│  └────────────────────────────────────┘  │
└──────────────────────────────────────────┘
```

When Claude Code starts, it reads `CLAUDE.md` first. This file bootstraps the entire agent system:

1. `CLAUDE.md` → tells Claude to read `AGENTS.md`
2. `AGENTS.md` → tells Claude to read `SOUL.md`, `USER.md`, and memory files
3. The agent wakes up with full context: who it is, who you are, and what happened recently
4. Heartbeat starts, scheduled tasks run, the agent is alive

## Quick Start

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# Edit USER.md with your info
# Edit config.json (set project_dir, model, etc.)

# Optional: install for scheduled tasks
pip install apscheduler

# Start — looks exactly like running `claude` directly
python clawx.py
```

On startup you'll see:

```
=======================================================
  🦀 ClawX — Claude Code PTY Wrapper
=======================================================
  Command:  claude --add-dir /path/to --model opus ...
  Project:  /home/you/your-project
  FIFO:     /home/you/your-project/mono.fifo
  Log:      /home/you/your-project/logs/

  Inject from another terminal:
    echo "your message" > mono.fifo
    python3 clawx.py inject "your message"

  Scheduled jobs:
    ⏰ heartbeat: */30 * * * * — Read HEARTBEAT.md...
=======================================================
```

Then Claude's full interactive UI takes over.

## Injecting Messages

From another terminal while ClawX is running:

```bash
# Via FIFO (simplest)
echo "run morning report" > mono.fifo

# Via CLI
python clawx.py inject "run morning report"
```

The text appears in Claude's input and gets submitted automatically — as if you typed it.

## Configuration: config.json

```json
{
  "claude": {
    "command": "claude",
    "project_dir": "./",
    "model": "opus",
    "permission_mode": null,
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

- **`permission_mode`**: Set to `null` for `--dangerously-skip-permissions` (default), or `"default"` for normal mode
- **`extra_args`**: Add `--channels` for Telegram/Discord integration
- **`schedule`**: Cron jobs that inject prompts on schedule

## Setup Options

### Option A: Use this repo as your project directory

Clone, customize `USER.md` and `config.json`, run `python clawx.py`.

### Option B: Copy soul files into an existing project

```bash
cp CLAUDE.md AGENTS.md SOUL.md USER.md HEARTBEAT.md MEMORY.md /path/to/project/
mkdir -p /path/to/project/memory
# Update config.json: "project_dir": "/path/to/project"
python clawx.py
```

### Option C: Copy clawx.py into your project

```bash
cp clawx.py config.json /path/to/project/
cd /path/to/project
python clawx.py
```

## CLI Commands

```bash
python clawx.py                    # Start (PTY passthrough)
python clawx.py inject "message"   # Inject into running session
python clawx.py prompt "message"   # One-shot (separate process)
python clawx.py stop               # Stop running session
```

## Telegram Integration

Add `--channels plugin:telegram@claude-plugins-official` to `extra_args` in config.json (included by default). See `CLAUDE.md` for full setup instructions.

## License

MIT
