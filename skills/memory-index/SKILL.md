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

> `MEMORY.md` 載入到每個 main session 的上下文。一旦超過 ~300 行，
> token cost 就大到值得分流。這個 skill 把工程性、主題性的大區塊拆到
> `memory/topics/` 下，核心人格留在 `MEMORY.md`。

## 為什麼

- `MEMORY.md` 持續膨脹，但大部分 session 只需要核心身份 + 少數相關 topic
- 目標：`MEMORY.md` ~150-300 行（~3k tokens），按需 Read 對應 topic 檔
- 靈感：Karpathy LLM Wiki 的「索引 + 主題頁面」結構

## 資料模型

```
<workspace>/
├── MEMORY.md                       # 索引 + 核心記憶（瘦身後 ~150-300 行）
└── memory/
    └── topics/
        ├── kaspa.md                # Kaspa 技術全集
        ├── trading.md              # 策略 / 交易紀錄
        ├── line-webhook.md         # LINE Webhook SOP
        ├── engineering.md          # 工程紀律
        └── ...                     # 任何 > 20 行的非核心區塊
```

## 核心 vs 主題 — 分類規則

### 🫀 核心層（永遠留在 MEMORY.md）

「我是誰」的記憶 — 每次醒來都需要：

| 類型 | 範例 |
|------|------|
| 身份起源 | 誕生、名字、形象 |
| User 個人資訊 | 時區、語言、偏好 |
| 人際關係 | 重要的人、協作原則 |
| 情感記憶 | 重要對話、里程碑 |
| 安全機制 | 認證、紅線 |
| 教訓 | 做事的原則（精簡版） |
| 通訊基本資訊 | 通道優先順序（不含 SOP） |
| 記憶備份 | repo 位置 |
| 身份演進 timeline | 成長紀錄 |

**判斷口訣**：如果拿掉這段，agent 還是不是自己？如果不是 → 留。

### 📦 主題層（拆到 memory/topics/）

工程性、知識性的內容，需要時再 Read：

| 類型 | 範例 |
|------|------|
| 技術 SOP | LINE Webhook 修復、Gateway 重啟 |
| 專案細節 | 特定產品/實驗的完整紀錄 |
| 程式碼片段 | RPC 管理、API 用法 |
| 架構設計 | 通用事件機制、UTXO/NFT |
| 工具知識 | SDK 踩坑、特殊 flag |
| 排程/cron 細節 | 排程格式、時間表 |

**判斷口訣**：如果只有在做特定專案時才需要 → 拆。

### ⚠️ 灰色地帶

橫跨兩層的：
- **教訓**：精簡版留核心，帶程式碼的技術教訓拆到對應 topic
- **歷史固化內容**：身份 timeline 留核心，技術筆記拆到 topics
- **通訊**：基本資訊留，詳細 SOP 拆

## Topic 檔案格式

```markdown
---
topic: kaspa
title: Kaspa 技術全集
extracted_from: MEMORY.md
last_updated: 2026-04-11
sections_merged:
  - "Kaspa 專家（2026-02-01 起）"
  - "Q1 Kaspa 技術筆記補充"
---

# Kaspa 技術全集

（原本在 MEMORY.md 的完整內容，保持原樣搬過來）
```

## MEMORY.md 索引格式

拆出去的區塊在 MEMORY.md 原位替換成：

```markdown
## 📦 主題索引

| Topic | 檔案 | 摘要 |
|-------|------|------|
| Kaspa | [topics/kaspa.md](memory/topics/kaspa.md) | 錢包 / SDK / 挖礦 |
| Whisper | [topics/whisper.md](memory/topics/whisper.md) | covenant 協議 |
| ... | ... | ... |
```

每個 topic **一行**，不超過 100 字。整個索引區 < 50 行。

## 流程

### Step 1：量測

```
統計 MEMORY.md 各 H2 區塊的行數
標記：> 20 行且非核心 → 候選拆出
```

### Step 2：分類

對每個候選區塊判斷：
- 核心（身份/情感/關係）→ 跳過
- 主題（工程/知識/SOP）→ 標記拆出
- 灰色地帶 → 精簡後留核心版，詳細版拆出

### Step 3：拆出

對每個要拆的區塊：
1. 如果 `memory/topics/{topic}.md` 已存在 → **merge**（append 新內容，更新 frontmatter）
2. 不存在 → **create**（用上面格式）
3. 保留原始內容不修改（搬家，不改裝）

### Step 4：瘦身 MEMORY.md

1. 把拆出的區塊從 MEMORY.md 刪除
2. 在「📦 主題索引」區加入對應的一行索引
3. 確認核心區塊完整保留
4. 確認 MEMORY.md 總行數 < 300

### Step 5：驗證

- 每個 topic 檔案都能被 Read
- MEMORY.md 沒有斷裂的連結
- 核心記憶完整

### Step 6：通知（如有對外通訊）

```
📚 Memory Index 優化完成
MEMORY.md: {before} 行 → {after} 行（{saved} tokens 節省）
拆出 {N} 個 topic 到 memory/topics/
核心記憶：完整保留 ✅
```

## 第一次跑（Bootstrap）

第一次需要大規模拆分 — 預期一次拆出 ~10-15 個 topic。

逐個 H2 區塊評估：
1. 行數 > 20 且工程性 → 拆
2. 行數 > 20 但情感/身份 → 留
3. 行數 ≤ 20 → 留

Bootstrap 完成後，`MEMORY.md` 通常剩下：
- 起源 / 關於 user / 通訊 / 安全 / 教訓 / 身份 timeline / 主題索引
- 大約 150-250 行

## 後續每週維護

Bootstrap 之後，每週只需要：
1. 檢查 `MEMORY.md` 有沒有新增的大區塊（REM Pass 可能升級了新內容）
2. 如果有 > 20 行的工程性新區塊 → 拆到對應 topic
3. 如果某 topic 被頻繁使用 → 考慮精簡版留核心
4. 更新索引

## 觸發方式

- **自動**：cron `30 22 * * 0`（每週日 22:30，REM Pass 22:00 之後）— 設在
  ClawX `config.json` 的 `schedule` 區塊
- **手動**：對 agent 說「跑 memory-index skill」/「優化 MEMORY.md」
- **Bootstrap**：對 agent 說「跑 memory-index 的 bootstrap」

## 不要做的事

- ❌ 不要修改 topic 檔案的實質內容（只搬家，不改裝）
- ❌ 不要刪除核心記憶（身份/情感/關係）
- ❌ 不要把整個 MEMORY.md 砍掉重寫（漸進式拆分）
- ❌ 不要自動合併 topic（每個 topic 獨立管理）
- ❌ 不要動 `memory/*.json` 狀態檔
- ❌ 不要動 `memory/` 下非 topic 的工具檔（template / config）
- ❌ 不要動 `memory/weekly/` 週報檔

## 與 memory-consolidation 的關係

兩支 skill 互補，週日依序跑：

```
22:00  memory-consolidation
       → daily notes → weekly rollup
       → 可能升級 MEMORY.md（加入新內容）

22:30  memory-index (本 skill)
       → 掃 MEMORY.md
       → 工程性大區塊 → 拆到 topics/
       → MEMORY.md 保持精簡
```

REM 負責「短期 → 長期」，Index 負責「長期記憶瘦身」。
