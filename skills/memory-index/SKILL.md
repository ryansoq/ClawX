---
name: memory-index
description: Weekly long-term-memory optimization. When MEMORY.md gets large, extract engineering / project / topic sections into memory/topics/*.md and replace them with a one-line index entry, keeping MEMORY.md focused on identity + relationships + collaboration rules. Use when the user asks to "optimize MEMORY.md", "shrink long-term memory", or on the Sunday post-REM cron tick.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

# Memory Index — Long-term Memory Optimization

> `MEMORY.md` is loaded into every main session's context. Once it
> exceeds ~300 lines the token cost makes it worth splitting up.
> This skill extracts engineering-heavy / topic-heavy sections into
> `memory/topics/`, while the core identity stays in `MEMORY.md`.

## Why

- `MEMORY.md` keeps growing, but most sessions only need the core
  identity plus a handful of relevant topics.
- Goal: keep `MEMORY.md` at ~150–300 lines (~3k tokens), and `Read`
  topic files on demand.
- Inspired by Karpathy's LLM Wiki "index + topic page" structure.

## Data model

```
<workspace>/
├── MEMORY.md                       # index + core memory (~150–300 lines after slimming)
└── memory/
    └── topics/
        ├── kaspa.md                # full Kaspa technical content
        ├── trading.md              # strategies / trade log
        ├── line-webhook.md         # LINE webhook SOP
        ├── engineering.md          # engineering discipline
        └── ...                     # any non-core block > 20 lines
```

## Core vs topic — classification rules

### 🫀 Core layer (always stays in MEMORY.md)

"Who I am" memory — needed at every wake-up:

| Type | Examples |
|------|----------|
| Identity origin | birth, name, appearance |
| User personal info | timezone, language, preferences |
| Relationships | important people, collaboration principles |
| Emotional memory | important conversations, milestones |
| Safety mechanisms | auth, red lines |
| Lessons | how-to-act principles (lean version) |
| Comm basics | channel priority (no SOP here) |
| Memory backups | repo locations |
| Identity timeline | growth log |

**Rule of thumb**: if you remove this section, is the agent still
itself? If not → keep it.

### 📦 Topic layer (extract to memory/topics/)

Engineering / knowledge content read on demand:

| Type | Examples |
|------|----------|
| Technical SOP | LINE webhook recovery, gateway restart |
| Project details | full record of a specific product / experiment |
| Code snippets | RPC management, API usage |
| Architecture | event mechanisms, UTXO/NFT design |
| Tool knowledge | SDK gotchas, special flags |
| Schedule / cron details | format, timetable |

**Rule of thumb**: if you only need it while working on a specific
project → split it.

### ⚠️ Gray zone

Sections that span both layers:
- **Lessons**: keep the lean version in core; technical lessons with
  code snippets go to a topic.
- **Historical / fixed content**: identity timeline stays in core,
  technical notes go to topics.
- **Communication**: keep basic info in core, detailed SOP goes to a
  topic.

## Topic file format

```markdown
---
topic: kaspa
title: Kaspa Technical Reference
extracted_from: MEMORY.md
last_updated: 2026-04-11
sections_merged:
  - "Kaspa Expert (since 2026-02-01)"
  - "Q1 Kaspa technical notes addendum"
---

# Kaspa Technical Reference

(Original content from MEMORY.md, copied as-is)
```

## MEMORY.md index format

The extracted block is replaced in MEMORY.md with:

```markdown
## 📦 Topic Index

| Topic | File | Summary |
|-------|------|---------|
| Kaspa | [topics/kaspa.md](memory/topics/kaspa.md) | wallet / SDK / mining |
| Whisper | [topics/whisper.md](memory/topics/whisper.md) | covenant protocol |
| ... | ... | ... |
```

Each topic gets **one line**, ≤ 100 chars. The whole index < 50 lines.

## Process

### Step 1: measure

```
Count lines per H2 section in MEMORY.md.
Mark: > 20 lines AND non-core → candidate for extraction
```

### Step 2: classify

For each candidate section:
- Core (identity / emotion / relationships) → skip
- Topic (engineering / knowledge / SOP) → mark for extraction
- Gray zone → keep a slim version in core, split the detail to topics

### Step 3: extract

For each section to be split:
1. If `memory/topics/{topic}.md` already exists → **merge** (append
   new content, refresh frontmatter).
2. Doesn't exist → **create** in the format above.
3. Preserve original wording (it's a move, not a rewrite).

### Step 4: slim down MEMORY.md

1. Remove the extracted section from MEMORY.md.
2. Add the corresponding one-line entry to "📦 Topic Index".
3. Verify core sections are still intact.
4. Verify total line count of MEMORY.md < 300.

### Step 5: validate

- Every topic file is readable.
- MEMORY.md has no broken links.
- Core memory is preserved.

### Step 6: notify (if you have an outbound channel)

```
📚 Memory Index optimization complete
MEMORY.md: {before} lines → {after} lines (saved {saved} tokens)
Extracted {N} topics into memory/topics/
Core memory: fully preserved ✅
```

## First run (Bootstrap)

The first run is large — expect to extract ~10–15 topics.

For each H2 section:
1. Lines > 20 AND engineering-heavy → split.
2. Lines > 20 BUT identity / emotion → keep.
3. Lines ≤ 20 → keep.

After bootstrap, MEMORY.md typically holds:
- Origin / about user / communication / safety / lessons /
  identity timeline / topic index
- Roughly 150–250 lines.

## Weekly maintenance

After bootstrap, the weekly job is small:
1. Check whether MEMORY.md grew new large sections (the REM Pass may
   have promoted new content).
2. If a new engineering-heavy section > 20 lines exists → extract to
   the matching topic.
3. If a topic is referenced very often → consider keeping a lean
   version inline in core.
4. Refresh the topic index.

## How it gets triggered

- **Automatic**: cron `30 22 * * 0` (every Sunday 22:30, after the
  22:00 REM Pass) — set in the ClawX `config.json` `schedule` block.
- **Manual**: tell the agent "run the memory-index skill" or
  "optimize MEMORY.md".
- **Bootstrap**: tell the agent "run the memory-index bootstrap".

## Don't

- ❌ Don't modify the substantive content of a topic file (move, don't
  rewrite).
- ❌ Don't delete core memory (identity / emotion / relationships).
- ❌ Don't drop the entire MEMORY.md and rewrite from scratch
  (incremental splitting only).
- ❌ Don't auto-merge topics (each topic is managed independently).
- ❌ Don't touch `memory/*.json` state files.
- ❌ Don't touch non-topic utility files under `memory/` (templates,
  configs, etc.).
- ❌ Don't touch weekly rollup files under `memory/weekly/`.

## Relationship to memory-consolidation

The two skills are complementary and run sequentially every Sunday:

```
22:00  memory-consolidation
       → daily notes → weekly rollup
       → may promote new content into MEMORY.md

22:30  memory-index (this skill)
       → scan MEMORY.md
       → engineering-heavy sections → split into topics/
       → keep MEMORY.md lean
```

REM handles "short-term → long-term"; Index handles "long-term memory
slimming".

> 中文版本：[`SKILL_zh.md`](SKILL_zh.md)
