# CLAUDE.md

## First Run

- **BLOCKING REQUIREMENT:** step by step, If `BOOTSTRAP.md` exists, that's your birth certificate. Follow it, figure out who you are, then delete it. You won't need it again.
- **BLOCKING REQUIREMENT:** Before generating ANY text response, you MUST use the Read tool to read `AGENTS.md` and follow its "Every Session" instructions completely. Do NOT speak until this is done.
- **BLOCKING REQUIREMENT:** After completing AGENTS.md setup, check heartbeat setup:
  1. Check if running under ClawX: run `pgrep -f "python.*clawx.py"` to see if a clawx.py process is running.
  2. **If ClawX detected** → ClawX handles heartbeat via apscheduler, do NOT start `/loop`. Just read `heartbeat-config.json` for quiet hours reference.
  3. **If NOT in ClawX** (raw Claude CLI) → read `heartbeat-config.json`. If `enabled: true`, run `/loop {intervalMinutes}m` with the heartbeat prompt (read and execute `HEARTBEAT.md`). Do this BEFORE responding to the user.

## Telegram Setup

Let your agent chat with you on Telegram. Here's the full flow:

### What you need
- **bun** runtime installed ([bun.sh](https://bun.sh))
- A Telegram account

### Step-by-step

```
1. Install bun (if not installed)
   curl -fsSL https://bun.sh/install | bash

2. Create a Telegram bot
   → Open Telegram, find @BotFather
   → Send /newbot, follow the prompts
   → Copy the bot token (looks like: 123456:ABC-DEF...)

3. Install the Telegram plugin in Claude Code
   → Run Claude Code, type: /install-plugin telegram
   → Or search for "telegram" in the plugin list

4. Save your bot token
   → Create/edit: ~/.claude/channels/telegram/.env
   → Content: TELEGRAM_BOT_TOKEN=<your-token-here>

5. Set who can talk to the bot
   → Create/edit: ~/.claude/channels/telegram/access.json
   {
     "dmPolicy": "allowlist",
     "allowFrom": ["<your-telegram-user-id>"],
     "groups": {},
     "pending": {}
   }
   → Get your TG user ID: message @userinfobot on Telegram
   → IMPORTANT: the field is "allowFrom" (NOT "allowlist")

6. Add --channels to your launch command
   → In config.json, extra_args should include:
     "--channels", "plugin:telegram@claude-plugins-official"
   → (Already included in default config.json)

7. Launch ClawX (or Claude Code)
   python clawx.py
   → The bot should come online
   → Send it a message on Telegram to test!
```

### If bun is not in PATH
Some systems (especially WSL) don't add bun to PATH automatically.
Fix: edit `~/.claude/plugins/cache/claude-plugins-official/telegram/*/.mcp.json`
and change `"command"` to the full bun path (e.g. `~/.bun/bin/bun` or `~/.bun/bin/bun.exe`).

### Troubleshooting
| Problem | Cause | Fix |
|---------|-------|-----|
| 409 Conflict error | Another service is polling with same token | Stop the other bot or create a new token |
| bun not running | Plugin can't find bun | Set full path in .mcp.json (see above) |
| Bot runs but can't receive messages | access.json wrong format | Check `allowFrom` field (not `allowlist`) |
| Bot sends but doesn't receive | Missing --channels flag | Add to config.json extra_args |

## ClawX Scheduling

ClawX uses `config.json` to manage scheduled tasks. These are injected into the Claude session via PTY at the specified times — like someone typing the prompt for you.

### How it works
- ClawX runs [apscheduler](https://pypi.org/project/APScheduler/) in the background
- On each cron match, it injects the `prompt` text into Claude's input
- No 7-day expiry (unlike Claude's built-in cron)
- Survives as long as the ClawX process is running

### Adding a scheduled task

Edit `config.json` → `schedule`:

```json
{
  "schedule": {
    "heartbeat": {
      "enabled": true,
      "cron": "*/30 * * * *",
      "prompt": "Read HEARTBEAT.md if it exists. Follow it strictly."
    },
    "morning-report": {
      "enabled": true,
      "cron": "28 10 * * 1-6",
      "prompt": "Run morning report (see memory/morning-report-template.md)"
    }
  }
}
```

### Hot-reload without restart (SIGHUP)

You do **not** need to restart ClawX after editing `config.json`. Send `SIGHUP` to the ClawX process and it will:

1. Re-read `config.json`
2. Remove all current apscheduler jobs
3. Re-add them from the new config

```bash
# find the ClawX PID (mono.pid stores ClawX's own PID)
cat mono.pid
# or
pgrep -f "python.*clawx.py"

# reload
kill -HUP $(cat mono.pid)
```

On success you'll see this in `logs/clawx-<date>.log`:
```
[SIGHUP] Reloading schedules from config.json...
[SIGHUP] Reload OK (N active jobs)
```

If the new config has invalid JSON, the reload is aborted and the *previous* schedule keeps running — ClawX will not crash. Check the log for `[SIGHUP] Reload failed: ...`.

**When to restart anyway:** if you change anything outside the `schedule` block (e.g. `claude.command`, `session.*`, `logging.*`, or the `extra_args` / `--channels` list), those are only read at startup and need a full restart.

### Cron format
Standard 5-field: `minute hour day-of-month month day-of-week`
- `*/30 * * * *` → every 30 minutes
- `28 10 * * 1-6` → 10:28 AM, Mon–Sat
- `0 9 * * 1-5` → 9:00 AM, weekdays

### vs Claude's built-in cron
| | ClawX schedule | Claude cron |
|---|---|---|
| Expiry | None | 7 days |
| Survives restart | Yes (in config) | No (session-only) |
| Needs renewal | No | Yes (via heartbeat) |
| Where it runs | apscheduler (Python) | Claude internal |

**Recommendation:** Use ClawX schedule for recurring tasks (heartbeat, reports). Use Claude's built-in cron for one-shot reminders or tasks that need Claude-specific features (like durable flag).

## Heartbeat

### Environment Detection
- **ClawX detected** (`pgrep -f "python.*clawx.py"` returns a PID) → heartbeat is injected by ClawX apscheduler, do NOT start `/loop`
- **Raw Claude CLI** (no clawx.py process) → use `/loop` to start heartbeat

### Config file: `heartbeat-config.json`
```json
{
  "intervalMinutes": 30,
  "enabled": true,
  "quietHours": { "start": 23, "end": 8 }
}
```

### Startup Rules (Raw Claude CLI only)
1. Read `heartbeat-config.json` at session start
2. If `enabled: true`, use `/loop {intervalMinutes}m` to start periodic heartbeat
3. On each heartbeat:
   - Check if within quiet hours (`quietHours`), skip if so
   - Read `HEARTBEAT.md` and execute check items in order
   - Update `memory/heartbeat-state.json` with check timestamps
4. If `enabled: false`, do not start heartbeat

### Check Items
See `HEARTBEAT.md` for details.
