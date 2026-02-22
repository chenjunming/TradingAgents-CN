# Proposal: Rule-based Signal Engine + Feishu Alerts + Feedback Loop (TradingAgents-CN)

## 0. 背景与现状

我们已有能力：

* 多-agent 个股分析（基本面/技术面/情绪面等），可生成完整分析报告。
* 持仓同步展示，可通过飞书机器人 / Web UI 操作。
* 当前交易/提醒“规则”主要写死在代码里，不易迭代、不易复盘、不易统计信号效果。

代码可复用点（已在仓库存在）：

* `tradingagents/graph/signal_processing.py::SignalProcessor`：可把分析报告抽取成结构化决策（action/target_price/confidence/risk_score/reasoning）。([GitHub][1])
* `tradingagents/dataflows/interface.py`：已实现多数据源选择（含 LongPort 凭证检测、HK/US 数据源优先级读取）。([GitHub][2])
* `tradingagents/utils/stock_utils.py::StockUtils`：可识别市场/币种/代码格式。([GitHub][3])

---

## 1. 目标（Goals）

### G1：规则触发提醒（核心）

实现一个**规则引擎**，对 watchlist/持仓进行定时扫描（后续可扩展事件驱动），当满足条件时：

1. 生成 `SignalEvent`（结构化信号事件，含证据快照）
2. 做去重/冷却/升级（避免噪音）
3. 通过飞书推送**交互式卡片**（附证据 + 下一步动作剧本 + 一键回看/复盘）

### G2：规则配置化（替换“写死规则”）

用 YAML/JSON 定义规则（Rule DSL），支持：

* 条件（conditions）/确认（confirm）/风险（risk）
* 冷却时间、等级（prewarn/confirm/risk）
* 通知模板（不同场景不同字段）
* 规则集版本号（hash），用于可追溯与回测

### G3：闭环（可回看/可评估）

* Web UI 增加“信号时间线”（按标的/规则/等级筛选）
* 记录每次触发的指标快照、规则版本、是否已读/静默
* 增加信号质量评估：触发后 1/3/5/10 交易日收益分布、胜率等（P2）

---

## 2. 非目标（Non-goals）

* 不做自动下单/代客交易；提醒默认用“条件命中/风险提示/可选动作剧本”表达（降低合规风险）。
* 不在本期实现“全市场扫描选股”；只覆盖 watchlist/持仓。
* 不强依赖 GitHub code search（需要登录），实现以仓库内可直接修改代码为准。

---

## 3. 总体架构

新增模块（建议放 `tradingagents/signals/`，保持核心在 open 部分）：

```
tradingagents/
  signals/
    models.py            # dataclasses / pydantic models
    indicators.py        # 指标计算（MA/RSI/VWAP/ATR/VolumeRatio…）
    rule_dsl.py          # Rule schema + 校验
    rule_engine.py       # 规则执行器：evaluate(snapshot, rules)
    scanner.py           # 扫描器：scan_watchlist()
    dedupe.py            # 去重/冷却/升级逻辑
    store.py             # SignalStore 接口（Mongo/Redis）
    notifier/
      feishu.py          # 飞书卡片发送
      templates.py       # 卡片模板渲染
    orchestrator.py      # 触发后可选调用多-agent分析/SignalProcessor
```

与现有模块的关系：

* `scanner.py` 拉行情数据：优先复用 `tradingagents/dataflows/interface.py` 的数据源能力。([GitHub][2])
* `models.py` 使用 `StockUtils` 生成市场/币种展示信息。([GitHub][3])
* `orchestrator.py` 在“确认级/风险级”触发后，可调用现有多-agent分析并用 `SignalProcessor` 提取结构化结论。([GitHub][1])

---

## 4. 数据模型（Mongo 为主，Redis 可选加速）

### 4.1 Collections

#### `signal_rulesets`

* 保存规则集与版本，用于回滚/灰度

```json
{
  "_id": "ruleset_2026_02_16_v1",
  "version_hash": "sha256:....",
  "created_at": "2026-02-16T10:00:00Z",
  "enabled": true,
  "rules": [ /* Rule DSL objects */ ]
}
```

#### `signal_events`

* 每次触发写一条

```json
{
  "_id": "evt_...",
  "ts": "2026-02-16T10:35:00Z",
  "ticker": "NVDA",
  "market": "us",
  "rule_id": "ma50_reclaim_breakout",
  "level": "confirm",                 // prewarn|confirm|risk
  "ruleset_version_hash": "sha256:..",
  "dedupe_key": "NVDA|ma50_reclaim_breakout|confirm",
  "cooldown_until": "2026-02-16T12:35:00Z",
  "snapshot": {
    "price": 181.55,
    "ma20": 175.1,
    "ma50": 178.3,
    "ma100": 190.2,
    "rsi14": 58.2,
    "vwap": 179.9,
    "vol_ratio_20d": 1.7
  },
  "evidence": {
    "matched_conditions": ["cross_above(close, ma50)", "gte(vol_ratio_20d, 1.5)"],
    "conflicts": ["ma100_down_slope"]
  },
  "status": {
    "acked": false,
    "muted": false,
    "mute_until": null
  },
  "links": {
    "web_ui": "/signals/evt_...",
    "analysis_run": "/api/analysis/run?ticker=NVDA&evt=evt_..."
  }
}
```

#### `signal_mutes`（可选）

* 用于“静默某规则/某标的一段时间”

---

## 5. Rule DSL（YAML/JSON）

### 5.1 Rule schema（最小可用）

```yaml
id: ma50_reclaim_breakout
name: 回踩/站回MA50 + 放量确认
applies_to:
  markets: [china_a, hong_kong, us]
  tickers: []              # 空=适用于所有扫描标的
timeframe: 1d              # 1d / 15m / 60m（先实现 1d）
conditions:
  - op: cross_above
    left: close
    right: ma50
  - op: gte
    left: vol_ratio_20d
    right: 1.5
confirm:                    # 可选：更严格确认，命中则 level 升级
  - op: gte
    left: rsi14
    right: 55
risk:                       # 可选：风险触发，命中则 level=risk
  - op: cross_below
    left: close
    right: ma50
cooldown_minutes: 120
level_on_match: prewarn     # prewarn|confirm
notify:
  channel: feishu
  template: default_signal_card
action_hint: "可考虑分批(40/40/20)。若放量跌回MA50且不收复→降级/减仓。"
```

### 5.2 先支持的 operators（P0）

* `gte/lte/between/within_pct`
* `cross_above/cross_below`
* `new_high_n/new_low_n`（N=20 默认）
* `trend_up/trend_down`（基于 MA slope 或最近 N 根线回归斜率）

### 5.3 规则版本

* 读取所有 `rules/*.yaml` → 规范化 JSON → hash（sha256）作为 `ruleset_version_hash`
* 每条 event 持久化该 hash，用于回溯“当时规则是什么”

---

## 6. 扫描与触发流程（P0）

### 6.1 Watchlist 来源（优先级）

1. 当前持仓（已有同步）
2. 用户自定义 watchlist（已有或新增）
3. 可选：当天重点关注列表

### 6.2 Scan loop（伪代码）

```python
def scan_once():
  tickers = load_watchlist_and_positions()
  ruleset = load_enabled_ruleset()
  for ticker in tickers:
    snap = MarketDataSnapshot.build(ticker, timeframe="1d", lookback=200)
    ind  = IndicatorEngine.compute(snap)      # ma20/50/100, rsi, vwap, vol_ratio...
    events = RuleEngine.evaluate(ticker, ind, ruleset.rules)
    for evt in events:
      if Dedupe.should_emit(evt):             # 去重/冷却/升级
        Store.save_event(evt)
        Notifier.send_feishu_card(evt)
        if evt.level in ["confirm","risk"]:
          Orchestrator.maybe_run_multi_agent_analysis(evt)   # 可异步
```

### 6.3 去重/冷却/升级策略（必须内建）

* `dedupe_key = ticker|rule_id|level`
* 若 `now < cooldown_until`：不推
* 若同 rule 的 `prewarn` 已推，后续升级到 `confirm`：允许推（即“升级可推”）
* 支持 `mute`：用户手动静默后直接跳过

---

## 7. 飞书通知（交互式卡片）

### 7.1 消息内容（强制字段）

* 标的 + 市场 + 当前价
* 命中规则名称 + 等级（预警/确认/风险）
* 关键证据快照：`价 vs MA20/50/100`、`RSI`、`vol_ratio_20d`、`VWAP`
* `action_hint`（动作剧本，不用“强指令”）
* Buttons：

  * “查看详情”（打开 Web UI 信号详情）
  * “一键跑多Agent复盘”（调用 API）
  * “静默此规则 7 天”（写入 mute）
  * “标记已读”（acked）

### 7.2 配置

* 环境变量（示例）：

  * `FEISHU_WEBHOOK_URL`（最简单：群机器人 webhook）
  * 或 `FEISHU_APP_ID/FEISHU_APP_SECRET`（如需更复杂权限）

---

## 8. Web UI / API（P1）

> 如果现有 Web UI 已存在对应框架，则只需新增页面与接口；若框架不同，保持 API 层清晰即可。

### 8.1 API endpoints（建议）

* `GET /api/signals?tickers=&level=&rule_id=&from=&to=&acked=`
* `POST /api/signals/{event_id}/ack`
* `POST /api/signals/{event_id}/mute` body: `{ "days": 7 }`
* `GET /api/rulesets/current`
* `POST /api/rulesets/validate`（上传 YAML 校验）
* `POST /api/rulesets/publish`（发布新规则集，生成 version hash）
* `POST /api/analysis/run?ticker=...&event_id=...`（触发多-agent分析）

### 8.2 UI pages

* `/signals`：时间线（筛选/搜索/分页）
* `/signals/:id`：事件详情 + 指标图 + 历史同规则事件
* `/rules`：规则列表 + 启停 + 版本历史（可后置）

---

## 9. 多-agent 调度策略（P2，但建议预留接口）

原则：**规则扫描便宜且频繁；LLM 分析昂贵且“只在值得时跑”。**

* 仅当事件等级 ≥ `confirm` 或 `risk` 时触发多-agent
* 产出报告后用 `SignalProcessor` 抽取结构化 `decision` 并挂到 event 上（或写 `analysis_results` collection）。([GitHub][1])

---

## 10. 测试与验收（Acceptance Criteria）

### P0（必须通过）

* [ ] 能从规则 YAML 加载并成功扫描 5 个 tickers，产生 events
* [ ] 去重/冷却生效：同一 dedupe_key 在 cooldown 内不会重复推送
* [ ] 升级生效：prewarn → confirm 会推送第二次（升级卡片）
* [ ] 飞书卡片包含关键指标字段 + 可点击链接/按钮（按钮先 stub 也可）
* [ ] `signal_events` 落库可查询（按 ticker、rule_id、level）

### P1（强烈建议）

* [ ] Web UI 可查看 signals 列表与详情
* [ ] 支持 ack/mute，且 mute 生效（不再推同类）
* [ ] ruleset 支持版本发布与回滚（至少保留最近 N 个）

### P2（增强）

* [ ] 触发后自动跑多-agent并回填结构化结论
* [ ] 信号质量统计：每条规则的胜率/分布可导出

---

## 11. 实施步骤（Codex 执行清单）

### Step 1：新增 signals 核心模块（不碰 UI）

1. 创建 `tradingagents/signals/` 目录与 `models.py/rule_engine.py/scanner.py/dedupe.py/store.py`
2. 实现 Rule DSL 解析（YAML→Rule对象）与 evaluate
3. 实现指标计算最小集：MA20/50/100、RSI14、VWAP（可选）、vol_ratio_20d
4. 实现 Mongo store（若已有 `app.core.database` 可直接复用连接；否则在 `store.py` 做接口 + fallback to local json）

### Step 2：飞书通知（webhook 版本优先）

1. `notifier/feishu.py`：封装 `send_card(event)`
2. `notifier/templates.py`：卡片 JSON 模板渲染（支持 action buttons）

### Step 3：加一个命令行/脚本入口用于调试

* `python -m tradingagents.signals.scanner --tickers NVDA,TSM --rules rules/default.yaml --once`
* 输出：触发的 events + 是否发送飞书 + 存储结果

### Step 4：接入现有后台定时任务

* 若已有 scheduler：注册 `scan_once()` 每 N 分钟执行（按市场时段）
* 否则先用简单 cron/APS cheduler（后续再升级）

### Step 5：API/UI（可后置）

* 增加 signals 列表接口、ack/mute 接口，再挂 UI

---

## 12. 风险与约束

* GitHub code search 需要登录，不作为实现依赖。
* 规则表达要避免“强制买卖指令”，提醒用“条件命中/风险提示/动作剧本（可选）”表述（降低合规与误导风险）。
* `app/` 与 `frontend/` 的改动若涉及授权/分发，请遵循仓库声明。([GitHub][4])

---

## 13. 附：默认内置规则建议（P0 起步 6 条）

1. `ma50_reclaim_breakout`：站回 MA50 + 放量 + RSI>55（确认）
2. `ma50_breakdown_risk`：放量跌破 MA50（风险）
3. `box_breakout_20d_high`：收盘突破 20D 新高 + 量比≥1.5（确认）
4. `rsi_reversal_buyzone`：RSI 40–50 上拐 + 价格靠近 MA20/50（预警）
5. `rsi_overheat_trim`：RSI>70 且量能背离（预警/风险）
6. `vwap_reclaim_intraday`（可选）：盘中重回 VWAP 且放量（预警）

---
