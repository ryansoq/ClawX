# Bundled Skills / 內建 Skills

> Skills are bundled instruction packs the agent can read and execute on
> demand. Drop more skills into this folder; each one is a directory
> with a `SKILL.md` file at the top.

> Skills 是 agent 隨用隨讀的指令包。要加新 skill，就在這個資料夾下開
> 子目錄，裡面放一份 `SKILL.md` 即可。

```
skills/
├── memory-consolidation/SKILL.md   # 週日 REM pass：daily → weekly rollup
└── memory-index/SKILL.md           # 週日：MEMORY.md 瘦身、拆 topics
```

## How the agent discovers a skill / Agent 怎麼發現 skill

There's nothing magical — it's just a Markdown file at a known path.

1. **Manual** (chat) — tell the agent in plain language:
   > Read `skills/memory-consolidation/SKILL.md` and follow it strictly.

2. **Scheduled** (ClawX cron) — add an entry to `config.json` and the
   ClawX scheduler will inject the same prompt at the chosen time:

   ```json
   {
     "schedule": {
       "memory-consolidation-weekly": {
         "enabled": true,
         "cron": "0 22 * * 0",
         "prompt": "Read skills/memory-consolidation/SKILL.md and run this week's REM pass."
       },
       "memory-index-weekly": {
         "enabled": true,
         "cron": "30 22 * * 0",
         "prompt": "Read skills/memory-index/SKILL.md and optimise MEMORY.md."
       }
     }
   }
   ```

   Cron is standard 5-field (`minute hour dom month dow`). After editing
   `config.json`, send `SIGHUP` to the ClawX process so it reloads
   schedules without restart:
   `kill -HUP $(cat mono.pid)`.

3. **Heartbeat** (optional) — if you want the heartbeat to also nudge
   these skills (e.g. on Sunday evening), add a section to
   `HEARTBEAT.md` referencing the SKILL path. The current bundled
   skills are designed for cron, not per-tick, so this is rarely needed.

## Why two separate skills / 為什麼分兩支

`memory-consolidation` and `memory-index` are intentionally separate:

- **memory-consolidation** turns *short-term* (daily) → *long-term*
  (weekly + MEMORY.md candidates). It only adds.
- **memory-index** keeps *long-term* lean. When MEMORY.md grows past
  ~300 lines it extracts engineering / topic sections into
  `memory/topics/*.md` and replaces them with a one-line index.

Run consolidation first (Sunday 22:00), then index (22:30). The output
of the first feeds the input of the second.

## Adding your own skill / 加自己的 skill

1. `mkdir skills/your-skill && cd skills/your-skill`
2. Create `SKILL.md` with this frontmatter:

   ```yaml
   ---
   name: your-skill
   description: One paragraph — what the skill does and *when* to use it.
                Be specific about triggers (user phrases, cron events).
   user-invocable: true
   allowed-tools:
     - Read
     - Write
     - Edit
     - Bash
   ---

   # Your Skill Title

   ## When to Use
   ## How It Works
   ## Examples
   ## Don'ts
   ```

3. Test by asking the agent: *"Read skills/your-skill/SKILL.md and run it."*
4. Optionally schedule via `config.json` (see above).

## See also / 延伸閱讀

- `CLAUDE.md` — general agent instructions for this workspace
- `HEARTBEAT.md` — periodic check items
- `AGENTS.md` — what files the agent reads on each session boot
