# 🦀 ClawX

**讓你的 Claude Code 擁有靈魂 — 持久身份、記憶、自主能力。**

ClawX 是 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 的輕量 PTY 外殼 + 靈魂框架。一組小小的設定檔，讓 Claude Code 擁有持久身份、記憶、心跳和排程任務 — 全部在官方 CLI 框架內運作。

![ClawX Demo](demo.png)

## 運作原理

```
┌─────────────────────────────────────────────────────────────┐
│                        ClawX                                │
│                                                             │
│   ┌─────────────┐    ┌──────────────────────────────────┐   │
│   │  clawx.py   │───>│  Claude Code CLI (PTY)           │   │
│   │  ─────────  │    │  ┌────────────────────────────┐  │   │
│   │  排程器     │    │  │ CLAUDE.md（引導啟動）      │  │   │
│   │  FIFO 注入  │    │  │   └→ BOOTSTRAP.md（首次）  │  │   │
│   │  自動重啟   │    │  │   └→ AGENTS.md             │  │   │
│   │  記錄轉寫   │    │  │       └→ SOUL.md（我是誰） │  │   │
│   └─────────────┘    │  │       └→ USER.md（你是誰） │  │   │
│                      │  │       └→ IDENTITY.md       │  │   │
│   ┌─────────────┐    │  │       └→ MEMORY.md         │  │   │
│   │ mono.fifo   │───>│  │       └→ memory/*.md       │  │   │
│   │（注入管道） │    │  │   └→ HEARTBEAT.md          │  │   │
│   └─────────────┘    │  └────────────────────────────┘  │   │
│                      └──────────────────────────────────┘   │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  通訊頻道：Telegram / Discord / ...                 │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 啟動流程

```
首次啟動：
  CLAUDE.md → BOOTSTRAP.md → 對話 → 填入 IDENTITY.md + USER.md → 刪除 BOOTSTRAP.md

之後每次啟動：
  CLAUDE.md → AGENTS.md → SOUL.md + USER.md + IDENTITY.md + memory/ → HEARTBEAT.md → 上線
```

1. **首次啟動** — `BOOTSTRAP.md` 引導一場對話，你和 agent 一起決定它的名字、個性和風格。它會填入 `IDENTITY.md` 和 `USER.md`，然後自我刪除。
2. **之後每次** — Claude 讀取 `AGENTS.md`，載入靈魂（`SOUL.md`）、你的資訊（`USER.md`）、身份（`IDENTITY.md`）和近期記憶。Agent 醒來時知道自己是誰、你是誰、最近發生了什麼。
3. **心跳** — 定期檢查（硬碟空間、加密貨幣價格、行事曆等）透過 `HEARTBEAT.md` 自動執行。
4. **排程任務** — Cron 在設定的時間注入 prompt（晨報、提醒等）。

### PTY 外殼

ClawX 在偽終端中執行 Claude Code。你看到的**跟直接跑 `claude` 完全一樣** — 顏色、進度條、動畫 — 但多了：
- **FIFO 注入** — 從任何終端送 prompt：`echo "你好" > mono.fifo`
- **排程注入** — apscheduler 按 cron 排程觸發 prompt
- **自動重啟** — Claude 掛了，ClawX 會自動拉起來
- **轉寫記錄** — 完整 session 存到 `logs/`

## 專案結構

```
ClawX/
├── clawx.py              # PTY 外殼 + 排程器
├── config.json           # 啟動 & 排程設定
│
├── CLAUDE.md             # 引導啟動入口
├── BOOTSTRAP.md          # 首次啟動儀式（完成後自刪）
├── AGENTS.md             # Agent 行為規範 & 記憶系統
├── SOUL.md               # Agent 個性 & 價值觀
├── IDENTITY.md           # Agent 身份卡（名字、emoji、風格）
├── USER.md               # 關於你的人類
├── TOOLS.md              # 環境特定的筆記
│
├── HEARTBEAT.md          # 定期檢查項目
├── heartbeat-config.json # 心跳間隔 & 安靜時段
├── MEMORY.md             # 長期記憶索引
└── memory/               # 每日記憶日誌
```

## 快速開始

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# 選裝：排程功能需要
pip install apscheduler

# 啟動 — 畫面跟 `claude` 一模一樣，但有超能力
python clawx.py
```

首次啟動時，agent 會自我介紹並詢問你是誰。自然對話就好。

### CLI 指令

```bash
python clawx.py                    # 啟動（PTY 透傳）
python clawx.py inject "訊息"      # 注入到執行中的 session
python clawx.py prompt "訊息"      # 一次性執行（獨立 process）
python clawx.py stop               # 停止執行中的 session
```

### 注入訊息

ClawX 執行中，從另一個終端：

```bash
# 透過 FIFO（最簡單）
echo "執行晨報" > mono.fifo

# 透過 CLI
python clawx.py inject "執行晨報"
```

## 設定

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

| 欄位 | 說明 |
|------|------|
| `model` | Claude 模型（`opus`、`sonnet`、`haiku`） |
| `permission_mode` | `null` = 跳過權限（預設），`"default"` = 正常模式 |
| `extra_args` | 額外 CLI 參數（如 `--channels` 接 Telegram） |
| `schedule` | Cron 排程，時間到自動注入 prompt |

### Telegram 整合

在 `extra_args` 加上 `--channels plugin:telegram@claude-plugins-official`（預設已包含）。詳見 `CLAUDE.md`。

## 安裝方式

### A. 全新開始 — 直接用這個 repo

Clone 下來，跑 `python clawx.py`。`BOOTSTRAP.md` 會引導首次對話，設定 agent 的身份和個性。

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX
pip install apscheduler  # 選裝，排程功能需要
python clawx.py
```

### B. 把 ClawX 加到現有專案（已有靈魂文件）

如果你的專案已經有 `SOUL.md`、`IDENTITY.md`、`MEMORY.md` 等（例如 OpenClaw workspace），只需要複製外殼和設定：

```bash
# 只複製外殼 + 設定到你的專案
cp clawx.py config.json /path/to/your-project/

# 編輯 config.json — project_dir 設為 "."（或保持預設）
# 依需求調整 model、permission_mode、extra_args

# 如果專案還沒有 CLAUDE.md，也一起複製
cp CLAUDE.md /path/to/your-project/

# 啟動
cd /path/to/your-project
python clawx.py
```

現有的靈魂文件（`SOUL.md`、`IDENTITY.md`、`USER.md`、`MEMORY.md`、`memory/` 等）會自動載入，不需要重複複製。

**注意：** 不要把 `BOOTSTRAP.md` 複製到已有身份文件的專案 — 它只用於首次設定，會覆蓋現有身份。

### C. 把現有專案搬進 ClawX

如果想用 ClawX repo 作為工作目錄，把現有的靈魂文件搬進來：

```bash
git clone https://github.com/ryansoq/ClawX.git
cd ClawX

# 刪除 BOOTSTRAP.md — 你已經有身份了
rm BOOTSTRAP.md

# 把你的靈魂文件複製過來（覆蓋範本）
cp /path/to/your-project/SOUL.md .
cp /path/to/your-project/IDENTITY.md .
cp /path/to/your-project/USER.md .
cp /path/to/your-project/MEMORY.md .
cp -r /path/to/your-project/memory/ ./memory/

# 其他需要的文件也一起複製（AGENTS.md、TOOLS.md、HEARTBEAT.md 等）
# 然後啟動
python clawx.py
```

### D. 讓 ClawX 指向遠端專案目錄

ClawX 可以獨立存在，透過 `config.json` 指向你的專案：

```bash
# 在 config.json 中設定 project_dir 為你的專案路徑
{
  "claude": {
    "project_dir": "/home/user/my-project",
    ...
  }
}
```

Claude 會用 `--add-dir` 載入該目錄。ClawX 在自己的資料夾，專案在自己的資料夾，互不干擾。

## 設計理念

傳統 AI 助手是無狀態的 — 每次對話從零開始。ClawX 給 Claude Code：

- **身份** — 名字、個性、價值觀，跨 session 持續
- **記憶** — 每日日誌 + 策劃過的長期記憶
- **自主性** — 心跳、排程任務、主動行為
- **關係** — 記得你是誰、你們怎麼合作

全部建立在小小的 markdown 檔案上。不需要資料庫、雲服務、訂閱。只有檔案和官方 Claude Code CLI。

## License

MIT
