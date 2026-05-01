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

> 每週固化一次，把散落在 daily 檔的經驗蒸餾成長期記憶。靈感來自人腦
> REM 睡眠把白天經驗轉成長期記憶。**不是 wiki、不是 RAG** — 是主動消化。

## 為什麼

- daily 檔會持續累積，但 `MEMORY.md` 只靠 heartbeat 「順手 review」很
  不穩定，常漏掉重要 pattern
- 週末跑一次集中消化 → 每週一頁可讀的回顧
- 同步把 pattern 升級候選列出，避免長期記憶失效或遺漏

## 資料模型

```
<workspace>/
├── MEMORY.md                # 長期記憶（精煉後才動）
└── memory/
    ├── 2026-04-09.md        # daily 檔（原始日誌）
    ├── 2026-04-08.md
    ├── ...
    └── weekly/
        ├── 2026-W14.md      # 週報（一週一頁）
        └── 2026-W15.md
```

### Frontmatter 協議

每個 daily 檔被週報消化過後，prepend：

```markdown
---
consolidated: 2026-04-09
weekly_ref: 2026-W15
---
# 原本的標題
```

- `consolidated`: 最近一次被消化的日期
- `weekly_ref`: 收進哪個週報
- 非破壞性：原本內容完全保留
- 已有 `consolidated` frontmatter 的 daily 檔 → 跳過（除非手動清掉 flag）

## 週報格式

`memory/weekly/YYYY-Www.md`：

```markdown
---
week: 2026-W15
span: 2026-04-06 to 2026-04-12
generated: 2026-04-09
daily_files: [2026-04-08.md, 2026-04-09.md]
---

# 2026-W15 週報

## 🎯 本週主軸
（1-2 句話，這週最重要的事）

## 📝 發生了什麼
（條列 3-7 件事，按重要性排序，每件一行或兩行）

## 💡 學到的事
（新 pattern、教訓、工具用法 — 尤其是「下次遇到類似情境會改變我行為」的）

## 🔄 對長期記憶的建議
（本週有沒有東西該升級到 MEMORY.md？格式：
  - **加到「X 章節」**：一行新事實
  - **更新「Y 章節」**：原本的 Z 已過時
  - **無** — 這週沒有 pattern 值得進 MEMORY.md
）

## 🧹 已消化的 daily 檔
- 2026-04-08.md
- 2026-04-09.md
```

## 流程

1. **找出本週範圍**
   - 用 ISO 週：`date -d "2026-04-09" +"%Y-W%V"` → `2026-W15`
   - 本週 = 上週一到本週日

2. **掃 daily 檔**
   - 找本週所有 `memory/2026-XX-XX.md`
   - 過濾掉已有 `consolidated` frontmatter 的
   - 0 個未消化 → log「本週無新 daily」結束

3. **寫週報**
   - 讀每個未消化 daily
   - 按上面格式產出 `memory/weekly/YYYY-Www.md`
   - 「本週主軸」用**自己的話**寫，不是逐條複貼

4. **回填 frontmatter**
   - 對每個被消化的 daily prepend `consolidated:` + `weekly_ref:`
   - 已有 frontmatter 的 → merge key（不覆蓋既有）

5. **判斷是否升級 `MEMORY.md`**
   - 看週報「對長期記憶的建議」區
   - 有具體升級 → 手動編輯 `MEMORY.md` 加進去
   - 「無」→ 不動
   - 每次升級都記錄到對應週報，方便追溯

6. **通知**（如有對外通訊）
   - 週報產出完成發訊息：
     ```
     📚 2026-W15 週報已產出
     本週 N 個 daily 檔 → memory/weekly/2026-W15.md
     MEMORY.md 升級：M 項（或「無」）
     ```

## 第一次跑（Bootstrap / Backfill）

第一次啟用時，可能有 N 週歷史 daily 檔還沒消化：

1. 掃所有 `memory/YYYY-MM-DD.md`，按 ISO 週分組
2. 對每週產出 `memory/weekly/YYYY-Www.md`
3. 為每個 daily prepend `consolidated:` frontmatter
4. **backfill 不自動動 `MEMORY.md`** — 改用「候選清單」讓使用者手動審核

> 為什麼 backfill 不自動改 `MEMORY.md`：一次跑 10 週等於翻出所有舊事，
> 自動批次改長期記憶風險太高（容易覆蓋既有 curated 內容）。改用 review
> → 手動 apply。

## 觸發方式

- **自動**：cron `0 22 * * 0`（每週日 22:00）— 設在 ClawX `config.json`
  的 `schedule` 區塊
- **手動**：對 agent 說「跑 memory-consolidation skill」/「跑這週的週報」
- **Backfill**：對 agent 說「跑 memory-consolidation 的 backfill」

## 不要做的事

- ❌ 不要直接改 daily 檔的內容（只能加 frontmatter，body 保留原樣）
- ❌ 不要刪 daily 檔（消化過也保留，`consolidated` 只是標記）
- ❌ 不要在一次跑裡自動批量改 `MEMORY.md`（只動候選清單）
- ❌ 不要覆蓋既有 frontmatter key（用 merge 語意）
- ❌ 不要動 `memory/*.json` 狀態檔（heartbeat-state.json 等）
- ❌ 不要動 `memory/` 下其他非 `YYYY-MM-DD.md` 的檔（template / config 類）

## 與 memory-index 的關係

兩支 skill 互補，週日依序跑：

```
22:00  memory-consolidation (本 skill)
       → daily notes → weekly rollup
       → 可能升級 MEMORY.md（加入新內容）

22:30  memory-index
       → 掃 MEMORY.md
       → 工程性大區塊 → 拆到 topics/
       → MEMORY.md 保持精簡
```

REM 負責「短期 → 長期」，Index 負責「長期記憶瘦身」。
