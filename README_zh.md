# ClaudexClaw

Claude Code supervisor daemon — 管理、監控、排程長期運行的 Claude Code sessions。

## 快速開始

```bash
# 安裝依賴（排程功能需要）
pip install apscheduler

# 啟動 daemon（會自動開 Claude CLI session）
python clawx.py

# 一次性指令（不需要 daemon）
python clawx.py prompt "跑晨報"

# 查看狀態
python clawx.py status

# 停止
python clawx.py stop
```

## 架構

```
ClaudexClaw (supervisor)
├── 生命週期管理：啟動 / 監控 / 自動重啟 Claude CLI
├── 排程系統：cron-based，不依賴 session（apscheduler）
├── 指令注入：送 prompt 到 running session
└── 日誌：所有 session 輸出都存 logs/

Claude CLI (worker)
├── CLAUDE.md bootstrap
├── MCP plugins (Telegram, etc.)
└── 日常工作與心跳檢查
```

## 設定：config.json

- `claude`：CLI 路徑、專案目錄、model、權限模式、額外參數（如 `--channels`）
- `session`：自動重啟策略、健康檢查間隔
- `schedule`：cron 排程（晨報、心跳等）
- `logging`：log 目錄、大小限制、輪替

## 運作原理

ClaudexClaw 用 `--print` 模式 + stream-JSON I/O 啟動 Claude Code，維持一個持久的互動式 session。它會監控健康狀態、crash 時自動重啟，並透過 stdin 注入排程 prompt。

跟每則訊息 spawn 新 process 的輕量 wrapper 不同，ClaudexClaw 維持**長期運行的 agent**，擁有完整的 context 連續性。

## TODO

- [ ] IPC socket：讓 `clawx.py send` 能跟 running daemon 溝通
- [ ] Web dashboard：簡單的狀態頁面
- [ ] Context 管理：偵測 context 快滿 → 優雅重啟
- [ ] 多 session 支援：同時管理多個 agent
- [ ] Windows service / systemd unit
