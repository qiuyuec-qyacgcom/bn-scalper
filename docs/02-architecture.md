# 系统架构

## 1. 设计目标与非目标

**目标**

| # | 目标 | 验收方式 |
|---|---|---|
| G1 | 回测与实盘共享同一套策略/风控/执行语义 | 同一份 `Strategy`/`RiskManager`/`OrderManager` 代码被两个引擎引用 |
| G2 | 任何时刻的持仓都有确定性的保护单 | 无保护单 → 立即强平；`forced_flatten` 指标为 0 |
| G3 | 模块可替换、可单测 | 依赖方向由 AST 测试强制；核心模块 0 第三方依赖 |
| G4 | 参数全部外置、启动期校验 | 非法配置在启动时 fail-fast |
| G5 | 异常有明确归属 | 拒单/超时/未知状态/对账不一致各有确定分支 |

**非目标**（刻意不做，避免把复杂度带进主流程）

- 多交易所智能路由、跨所套利
- L2 订单簿撮合仿真、tick 级回测
- 分布式部署、多策略进程编排
- 机器学习因子流水线

---

## 2. 分层视图

```
                         ┌──────────────────────────────┐
                         │           CLI (cli.py)        │
                         │  backtest / paper / live / dl │
                         └───────────────┬──────────────┘
                                         │ 装配
        ┌────────────────────────────────┼────────────────────────────────┐
        │                                │                                │
┌───────▼────────┐              ┌────────▼─────────┐            ┌─────────▼────────┐
│ engine/backtest│              │   engine/live    │            │ reporting/metrics│
│  Clock=历史bar │              │  Clock=实时bar    │            │  绩效与报表       │
└───────┬────────┘              └────────┬─────────┘            └──────────────────┘
        │                                │
        │        ┌───────────────────────┴──────────────┐
        └────────►           编排（唯一知道"怎么跑"的地方） ◄──────────┘
                 └───────────────────┬──────────────────┘
                                     │
   ┌──────────────┬──────────────────┼──────────────────┬──────────────────┐
   │              │                  │                  │                  │
┌──▼─────────┐ ┌──▼──────────┐ ┌─────▼───────┐ ┌────────▼────────┐ ┌───────▼───────┐
│ strategy/  │ │  risk/      │ │ execution/  │ │  portfolio.py   │ │   data/       │
│ 只产出意图  │ │ 唯一可否决   │ │ 唯一可下单   │ │  只负责记账      │ │ 行情/存储/REST │
└──┬─────────┘ └──┬──────────┘ └─────┬───────┘ └────────┬────────┘ └───────┬───────┘
   │              │                  │                  │                  │
   └──────────────┴──────────┬───────┴──────────────────┴──────────────────┘
                             │
                    ┌────────▼────────┐   ┌──────────────────┐
                    │ indicators.py   │   │    models.py     │
                    │  增量指标(共用)  │   │ 纯数据结构(无依赖) │
                    └─────────────────┘   └──────────────────┘
```

**依赖方向是单向的**：上层可以依赖下层，下层不得反向依赖。`models.py` 在最底层，
不 import 任何业务模块；`strategy/` 不得 import `execution/`、`risk/`、`data/`、`engine/`。

这条规则不是靠自觉，而是 `tests/test_system.py::TestArchitecture` 用 AST 静态检查强制执行。

---

## 3. 模块职责

| 文件 | 职责 | 明确**不**负责 | 关键类型 |
|---|---|---|---|
| `models.py` | Bar/Tick/Order/Fill/Position/Trade/Intent 等数据结构 | 任何业务逻辑 | `Order`、`Position`、`EnterIntent` |
| `config.py` | TOML 加载、类型转换、启动期校验 | 运行时可变配置 | `Config` 及各段 dataclass |
| `indicators.py` | 增量 EMA/ATR/RSI/SMA/极值，输出 `Snapshot` | 交易决策 | `IndicatorSet`、`Snapshot` |
| `data/feed.py` | 回测回放 / 实时订阅，统一提供"已收盘 bar" | 加工数据 | `CsvBarFeed`、`WsBarFeed`、`RestPollBarFeed` |
| `data/store.py` | SQLite 落库（行情/订单/成交/持仓/权益/风控事件） | 业务规则 | `Store` |
| `data/binance_rest.py` | 币安 REST（签名/限速/时间同步/错误分类） | 重试策略与业务语义 | `BinanceRest`、`BinanceAPIError` |
| `portfolio.py` | 仓位、费用、已实现/未实现盈亏、权益曲线 | 下单与风控 | `Portfolio` |
| `strategy/base.py` | `Strategy` 抽象与 `StrategyContext` | 订单、仓位 | `Strategy`、`StrategyContext` |
| `strategy/trend_pullback.py` | 主策略：趋势回踩确认 | 决定手数 | `TrendPullbackStrategy` |
| `strategy/donchian.py` | 备选策略：通道突破（证明可插拔） | — | `DonchianBreakoutStrategy` |
| `strategy/registry.py` | 名字 → 工厂，支持 TOML 选型 | — | `register`、`create_strategy` |
| `risk/sizing.py` | 风险预算法定仓 + 止损夹紧 + 盈亏比重算 | 交易时机 | `size_position`、`SizingResult` |
| `risk/manager.py` | L1 事前闸门 / L2 事中校验 / L3 事后熔断 | 下单实现 | `RiskManager`、`RiskDecision` |
| `execution/base.py` | `Broker` 抽象（回测/实盘唯一分歧点） | — | `Broker`、`OrderRejected` |
| `execution/simulator.py` | 模拟撮合（intrabar 路径、费用、滑点、post-only） | 策略逻辑 | `SimulatedBroker` |
| `execution/binance_broker.py` | 实盘下单、用户数据流解析、对账查询 | — | `BinanceBroker`、`PaperBroker` |
| `execution/oms.py` | 订单生命周期：幂等、重试、保护单、成交归属、异常兜底 | 交易信号 | `OrderManager` |
| `engine/backtest.py` | 逐 bar 回测编排与未来函数隔离 | 策略/撮合细节 | `BacktestEngine` |
| `engine/live.py` | 实时编排、用户流守护、定时对账、kill switch | 策略/撮合细节 | `LiveEngine` |
| `metrics.py` / `reporting.py` | 绩效计算与报表落盘 | — | `compute_metrics`、`write_report` |
| `cli.py` | 参数解析与组件装配（唯一的"接线"处） | 业务逻辑 | `main` |

---

## 4. 回测-实盘一致性机制

### 4.1 共享 / 替换 / 差异

| 类别 | 内容 |
|---|---|
| **完全共享** | `Strategy`、`RiskManager`、`sizing`、`OrderManager`、`Portfolio`、`IndicatorSet`、`metrics` |
| **按模式替换** | `Clock`（bar 时间戳来源）、`BarFeed`（文件回放 vs 异步订阅）、`Broker`（模拟 vs 币安 REST） |
| **无法消除的差异** | 网络延迟抖动、交易所撮合队列、部分成交、资金费率、强平、对账延迟 |

### 4.2 一致性是怎么被"逼"出来的

1. **策略只能看到已收盘 bar**：`Bar.closed` 恒为 True，引擎只在 `bar.close` 之后调用策略，
   信号最早在下一根 bar 成交 → 结构上不可能用到未来数据。
2. **指标只有一份实现**：`IndicatorSet` 同时服务回测与实盘，不存在"回测用 talib、
   实盘用自研"的双实现漂移。
3. **成交入口唯一**：所有成交都进 `OrderManager._handle_fill`，回测用同步回调（sink）、
   实盘用用户数据流回调 —— **同一个函数**。
4. **风控只有一处**：`RiskManager.pre_trade` 是唯一能拒绝入场的地方，回测与实盘都调用它。
5. **差异显式化**：实盘引擎额外有 `_reconcile_once()`，把"本地账本与交易所不一致"
   当作常规事件处理，而不是异常。

### 4.3 回测时序（每根 bar）

```
for bar in feed:
    1. broker.on_bar(bar)          # 用 intrabar 路径撮合上一根挂出的订单
       └─ 成交 → sink → oms.handle_fill()  # 入场成交后**本 bar 内**挂上保护单
    2. oms.advance(bar)            # 入场超时降级 / 保护单兜底 / 持仓硬上限 / 成交归属
    3. indicators.update(bar)      # 只用已收盘 bar
       pos.bars_held += 1; update_extremes(high, low)
    4. portfolio.mark(close); risk.on_bar(ts, equity)   # 日切 + 日亏熔断
    5. strategy.on_bar(ctx) -> [Intent...]
    6. Intent → risk.pre_trade → oms.submit_*()         # 下一根才可能成交
    7. 记录权益点
```

**第 6 步刻意延后一根 bar 成交**，这是最保守的假设；**第 1 步的同步回调**保证
止损保护在同一根 bar 内就生效，避免"入场当根无保护"的乐观偏差。

### 4.4 实盘时序

```
WS/REST 行情 ──► bar(closed) ──► _on_bar()
                                   ├─ broker.set_price / (paper) on_bar
                                   ├─ indicators / portfolio.mark / risk.on_bar
                                   ├─ strategy.on_bar → Intent → risk → oms
                                   └─ oms.advance(bar)

用户数据流 ──► broker.on_user_event() ──► fill 入队 ──► oms.handle_fill()   # 异步
定时任务 ──► _reconcile_once()   # 持仓/挂单与交易所对齐，缺保护单→补挂，未知仓位→强平
定时任务 ──► _housekeeping_loop() # 批量 commit、心跳、kill switch 文件检测
```

---

## 5. 订单状态机

```
                    ┌──────────────┐
      submit() ────►│ PENDING_NEW  │
                    └──────┬───────┘
          ┌────────────────┼─────────────────┐
          │                │                 │
   拒单/精度错误      挂单成功            市价/IOC 立即成交
          │                │                 │
   ┌──────▼──────┐  ┌──────▼──────┐   ┌──────▼──────┐
   │  REJECTED   │  │     NEW     │   │   FILLED    │
   └─────────────┘  └──────┬──────┘   └─────────────┘
                           │
              ┌────────────┼─────────────┐
       部分成交│      超时/撤单      条件触发│
   ┌──────────▼────────┐ ┌──▼────────┐ ┌─▼───────────┐
   │ PARTIALLY_FILLED  │ │ CANCELED  │ │   FILLED    │
   └──────────┬────────┘ └───────────┘ └─────────────┘
              │ 全量成交
        ┌─────▼──────┐
        │   FILLED   │
        └────────────┘
```

`EXPIRED` 用于 IOC 未成交、以及交易所侧 GTD/TTL 到期。

**幂等键**：`newClientOrderId = {E|X}-{SYMBOL}-{ts%1e8}{seq%100}`，长度 ≤ 36。
同一意图的所有重试复用该 ID，交易所侧不会产生第二笔成交。

---

## 6. 关键设计决策（ADR 摘要）

| # | 决策 | 备选方案 | 选择理由 | 代价 |
|---|---|---|---|---|
| A1 | 事件驱动 + 单进程同步策略 | 多线程/多进程 | 消除状态竞争；1m 频率 CPU 充裕 | 单标的阻塞风险（当前标的少，可接受） |
| A2 | 增量指标而非 DataFrame | pandas 全量重算 | 内存恒定 O(窗口)，与实盘一致 | 新指标要手写增量版 |
| A3 | 风控集中在 `RiskManager` | 策略内 self-check | 唯一否决点，可枚举拒绝码，可回放 | 策略无法做"特殊放行" |
| A4 | 保护单用交易所原生条件单 | 本地监控价格后平仓 | 断网/进程崩溃时保护仍生效 | 需要 extra 挂单/撤单，且要处理 OCO 手工逻辑 |
| A5 | 成交同步回调（sink） | 轮询成交队列 | 消除"成交→挂保护单"窗口 | 回调中不能做重活（当前只挂单） |
| A6 | bar 内 4 点路径 + 逆势优先 | 单一收盘价撮合 | 保守估计，避免高估收益 | 仍非真实 tick 序列 |
| A7 | SQLite(WAL) 单文件 | PostgreSQL/ClickHouse | 单机、可随策略归档、零运维 | 跨机不可用；写入串行化 |
| A8 | 零第三方依赖核心 | ccxt + pandas | 攻击面小、部署简单、行为可审计 | 需要自己维护 REST 客户端与指标 |

---

## 7. 扩展点

| 想做什么 | 改哪里 | 是否需要动引擎 |
|---|---|---|
| 新增策略 | 在 `scalpx/strategy/` 加文件，`@register("name")`，在 `strategy/__init__.py` 注册 | 否 |
| 新增指标 | `indicators.py` 加增量类，挂到 `IndicatorSet` 与 `Snapshot` | 否（引擎自动透传） |
| 新增交易所 | 实现 `Broker` 子类（参考 `binance_broker.py`） | 否 |
| 新增数据源 | 实现 `BarFeed` / `AsyncBarFeed` | 否 |
| 新增风控规则 | `risk/manager.py::pre_trade` 加一条带 code 的判定 | 否 |
| 新增出场方式 | `Strategy._manage` 返回新的 `Intent` 类型 | 是（`models.py` + 两个 engine 的 dispatch） |
| 多标的组合 | `MultiSymbolFeed` 已支持；风控已有 `max_positions` 与相关性系数 | 否 |

---

## 8. 目录结构

```
bn-scalper/
├── config/                 # backtest / paper / live 三套 TOML
├── docs/                   # 调研、架构、策略风控、运维
├── data/                   # 行情 CSV、SQLite、KILL 文件（gitignore）
├── runs/                   # 回测报告产物（gitignore）
├── scalpx/
│   ├── models.py config.py indicators.py portfolio.py metrics.py reporting.py cli.py
│   ├── data/    feed.py store.py binance_rest.py
│   ├── strategy/ base.py registry.py trend_pullback.py donchian.py
│   ├── risk/    sizing.py manager.py
│   ├── execution/ base.py simulator.py oms.py binance_broker.py
│   └── engine/  backtest.py live.py
├── scripts/     make_sample_data.py  grid_search.py
└── tests/       test_core.py  test_execution.py  test_system.py
```
