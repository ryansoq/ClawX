---
name: memory-consolidation
description: Weekly REM-style consolidation. Distill the past week's daily notes (memory/YYYY-MM-DD.md) into a single rollup at memory/weekly/YYYY-Www.md, mark daily files as consolidated via frontmatter, and surface candidates for promotion to MEMORY.md. Use when the user asks to "run weekly memory pass", "consolidate this week", or on a Sunday cron tick.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

# Memory Consolidation — Weekly REM Pass

> Once a week, distill scattered daily notes into long-term memory.
> Inspired by REM sleep turning daytime experiences into long-term
> memory in the human brain. **Not a wiki, not RAG** — it's active
> digestion.

## Why

- Daily notes accumulate continuously, but `MEMORY.md` updates only
  via "casual review" during heartbeats — unstable, often misses
  important patterns.
- A focused weekend pass yields one readable page of weekly recap.
- Surface long-term-memory promotion candidates so curated wisdom
  doesn't drift or get lost.

## Data model

```
<workspace>/
├── MEMORY.md                # long-term memory (only edited after distillation)
└── memory/
    ├── 2026-04-09.md        # daily notes (raw log)
    ├── 2026-04-08.md
    ├── ...
    └── weekly/
        ├── 2026-W14.md      # weekly rollup (one page per week)
        └── 2026-W15.md
```

### Frontmatter protocol

After a daily note has been consolidated into a weekly rollup,
prepend:

```markdown
---
consolidated: 2026-04-09
weekly_ref: 2026-W15
---
# Original heading
```

- `consolidated`: most recent date the file was consolidated
- `weekly_ref`: which weekly rollup it was folded into
- Non-destructive: original body is fully preserved
- Daily files that already have a `consolidated` frontmatter → skipped
  (unless the flag is manually cleared)

## Weekly rollup format

`memory/weekly/YYYY-Www.md`:

```markdown
---
week: 2026-W15
span: 2026-04-06 to 2026-04-12
generated: 2026-04-09
daily_files: [2026-04-08.md, 2026-04-09.md]
---

# 2026-W15 Weekly Rollup

## 🎯 Theme of the week
(1-2 sentences naming the most important thing this week)

## 📝 What happened
(3-7 bulleted items, ranked by importance, one or two lines each)

## 💡 Lessons learned
(New patterns, mistakes-turned-rules, tool tricks — especially the
"next time I see this kind of situation, I'll act differently" kind)

## 🔄 MEMORY.md upgrade suggestions
(Should anything from this week be promoted into MEMORY.md? Format:
  - **Add to "Section X"**: one-line new fact
  - **Update "Section Y"**: original Z is now outdated
  - **None** — no pattern this week worth long-term memory
)

## 🧹 Daily files consolidated
- 2026-04-08.md
- 2026-04-09.md
```

## Process

1. **Determine this week's range**
   - Use ISO week: `date -d "2026-04-09" +"%Y-W%V"` → `2026-W15`
   - This week = previous Monday through this Sunday

2. **Scan daily files**
   - Find all `memory/2026-XX-XX.md` for this week
   - Filter out files that already carry `consolidated` frontmatter
   - 0 unconsolidated → log "no new dailies this week" and stop

3. **Write the weekly rollup**
   - Read each unconsolidated daily
   - Produce `memory/weekly/YYYY-Www.md` in the format above
   - "Theme of the week" must be **in your own words**, not a copy-paste

4. **Backfill frontmatter**
   - For each daily that was consolidated, prepend
     `consolidated:` + `weekly_ref:`
   - If frontmatter already exists → merge keys (do not overwrite
     existing ones)

5. **Decide whether to promote into `MEMORY.md`**
   - Look at the rollup's "MEMORY.md upgrade suggestions" section
   - Concrete promotion → manually edit `MEMORY.md` and add it
   - "None" → leave it
   - Record every promotion in the corresponding rollup so it's traceable

6. **Notify** (if you have an outbound channel)
   - Send a short message after the rollup is produced:
     ```
     📚 2026-W15 weekly rollup ready
     N daily files this week → memory/weekly/2026-W15.md
     MEMORY.md upgrades: M items (or "none")
     ```

## First run (Bootstrap / Backfill)

The first time this skill runs, you may have N weeks of historical
daily files that have never been consolidated:

1. Walk all `memory/YYYY-MM-DD.md` and group by ISO week
2. For each week, produce `memory/weekly/YYYY-Www.md`
3. Prepend `consolidated:` frontmatter to every daily
4. **Backfill must NOT auto-edit `MEMORY.md`** — instead emit a
   "candidates" list for the user to review manually

> Why backfill skips automatic MEMORY.md edits: a single run that
> processes 10 weeks at once = uncovering all old material. Auto-batch
> editing long-term memory at that scale risks overwriting carefully
> curated content. Use review → manual apply instead.

## How it gets triggered

- **Automatic**: cron `0 22 * * 0` (every Sunday 22:00) — set in the
  ClawX `config.json` `schedule` block.
- **Manual**: tell the agent "run the memory-consolidation skill" or
  "produce this week's rollup".
- **Backfill**: tell the agent "run memory-consolidation backfill".

## Don't

- ❌ Don't modify the body of any daily file (only frontmatter may be
  added; the body stays as-is).
- ❌ Don't delete daily files (consolidated ones are also kept;
  `consolidated` is just a marker).
- ❌ Don't auto-batch-edit `MEMORY.md` in a single run (only emit
  candidates).
- ❌ Don't overwrite existing frontmatter keys (use merge semantics).
- ❌ Don't touch state files under `memory/*.json` (heartbeat-state.json
  etc.).
- ❌ Don't touch other non-`YYYY-MM-DD.md` files under `memory/`
  (templates, config files, etc.).

## Relationship to memory-index

The two skills are complementary and run sequentially every Sunday:

```
22:00  memory-consolidation (this skill)
       → daily notes → weekly rollup
       → may promote new content into MEMORY.md

22:30  memory-index
       → scan MEMORY.md
       → engineering-heavy sections → split into topics/
       → keep MEMORY.md lean
```

REM handles "short-term → long-term"; Index handles "long-term memory
slimming".

> 中文版本：[`SKILL_zh.md`](SKILL_zh.md)
