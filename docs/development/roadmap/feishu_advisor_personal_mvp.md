# 个人投研助手 MVP（已实现）

## 核心能力
- 飞书命令：`/daily` `/pick` `/rebalance` `/position`
- 报告上下文命令：`/report 600519`（加载该标的最新分析报告为当前对话上下文）
- 持仓维护命令：`/wl add 贵州茅台 [数量] [成本]`、`/wl del 600519`、`/wl list`
- 飞书自然语言代理：例如“把贵州茅台加入自选股”，系统会解析为 API 指令并执行；非指令内容走普通对话回复
  - 已启用确认机制：识别到可执行动作后，先生成待确认单；需 `/confirm <编号>` 才执行，`/cancel <编号>` 可取消
  - 支持回复链上下文：按飞书 `root_id/parent_id` 维护会话历史，可基于报告上下文连续追问
- 统一持仓：A股（SQLite）+ 港美（LongPort）
- 顾问引擎：选股评分 + 稳健调仓建议
- 分市场自动推送：开市后30m / 收盘前30m / 收盘后30m
- 非开市时间自动跳过（优先使用 `exchange_calendars`）

## 新增 API
- `POST /api/advisor/daily-picks/generate`
- `POST /api/advisor/rebalance/generate`
- `GET /api/advisor/daily-brief`
- `GET /api/advisor/positions/latest`
- `POST /api/advisor/a-share/import-csv`
- `GET /api/advisor/a-share/positions`
- `POST /api/advisor/push/dispatch-now`
- `GET /api/advisor/push/events`
- `GET /api/advisor/push/next`
- `POST /api/feishu/webhook`
- `GET /api/portfolio/positions/latest`
- `GET /api/portfolio/markets/exposure`
- `POST /api/a-share/import/csv`
- `GET /api/a-share/positions`

## 环境变量
见 `.env.example` 中“个人投研助手（飞书+分市场推送）”章节。

## 快速使用
1. 初始化 SQLite
```bash
python scripts/init_a_share_sqlite.py
```

2. 导入 A 股持仓 CSV（字段：`symbol,name,quantity,cost_price,market_value`）
```bash
python scripts/import_a_share_positions.py /path/to/positions.csv
```

3. 配置 LongPort 与飞书环境变量后启动后端。

## 说明
- 当前实现为个人项目快速版，默认单用户（`default`）调度。
- 未实现自动下单，仅生成建议并推送。
