# 开源量化系统调研与架构对标

调研对象为 GitHub 上主流、仍在维护、且架构上有代表性的项目。每一条结论都标注了
**来源类型**：`[README]` 表示从仓库首页/文档直接核实；`[社区]` 表示来自第三方评测文章或
项目长期公开的目录结构，仅作参考。

> 调研目的不是"抄一个"，而是找出这些系统在**回测-实盘一致性、风控边界、订单生命周期**
> 三件事上的共识，再判断哪些共识值得一个单机、单交易所、1 分钟级的超短线系统继承。

---

## 1. 项目速览

| 项目 | 语言/规模 | 定位 | 核心抽象 | 来源 |
|---|---|---|---|---|
| **freqtrade** | Python, 56k★ | 加密现货/合约中低频交易机器人 | `IStrategy` + `Exchange` + `Protection` | `[README]` |
| **NautilusTrader** | Rust 内核 + Python 控制面, 31k★ | 生产级确定性事件驱动引擎 | `MessageBus` + `Cache` + `Adapter` | `[README]` |
| **Hummingbot** | Python, 21k★ | 做市/高频，140+ 场所 | `Connector` + `Controller` + `Executor` | `[README]` |
| **Qlib** | Python, 50k★ | AI 量化研究平台（因子→模型→组合→执行） | `DataHandler` + `Expression` + `NestedExecutor` | `[README]` |
| **Backtrader** | Python, 20k★ | 通用事件驱动回测 | `Cerebro` + `Strategy` + `Broker` | `[社区]` |
| **vn.py** | Python + C++, 30k★ | 国内期货/证券一体化交易平台 | `EventEngine` + `Gateway` + `App` | `[社区]` |
| **VectorBT** | Python, 5k★ | 向量化回测/参数扫描 | `Portfolio.from_signals` | `[社区]` |
| **Jesse** | Python, 6k★ | 加密策略研究 + 实盘 | `Strategy` 双引擎 | `[社区]` |

---

## 2. 逐项拆解

### 2.1 freqtrade：**"回测复刻实盘语义"** 路线

- **策略接口是声明式三件套**：`populate_indicators` / `populate_entry_trend` / `populate_exit_trend`，
  全部作用于 DataFrame，输出 `enter_long` 等信号列。策略只描述"什么时候想交易"，不碰下单。
- **一致性靠复用同一段决策链**：`analyze_ticker → advise_indicators → advise_entry/exit → get_signal`
  在回测与实盘里是同一份代码；差异只收敛到两处——`FreqtradeBot.process()`（轮询真实行情）
  与 `Backtesting.backtest()`（历史逐根推进）。
- **交易所层用"能力声明"而非 if-else**：`_ft_has` 字典集中描述各所的差异
  （`ohlcv_candle_limit`、`stoploss_on_exchange`、`supports_ws`…），子类只覆盖差异。
  这是一个非常值得抄的设计：**把"差异"数据化，而不是让差异散落在业务逻辑里**。
- **风控是插件，不是策略的一部分**：`IProtection` + `ProtectionManager` + `PairLocks` 表，
  内置 `StoplossGuard` / `MaxDrawdown` / `LowProfitPairs` / `CooldownPeriod`，
  在"入场前"统一判定，回测与实盘共用同一套锁定逻辑。
- **持久化用 ORM**：`trades` / `orders` / `pairlocks` / `KeyValueStore`，带自动 schema 迁移；
  回测用轻量 `LocalTrade` 避免写库放大。
- **数据层可插拔**：`IDataHandler` 支持 json/feather/parquet/hdf5。

> 对我们的启示：**风控独立成层 + 交易所差异数据化 + 回测复刻实盘路径**。
> 不采纳的部分：DataFrame 策略接口（1m 级全量重算内存与延迟都不可接受）、ccxt 依赖。

### 2.2 NautilusTrader：**"同一套运行时贯通研究与生产"**

- Rust 原生内核 + Python 控制面（PyO3），强调 **deterministic event-driven architecture**；
  回测与实盘共用同一套时间模型（纳秒级）与事件语义。
- 组件从 `MessageBus` + `Cache` 装配，`Cache`/总线后端可换 Redis。
- 订单语义覆盖 IOC/FOK/GTD、post-only、reduce-only、iceberg，以及 **OCO/OUO/OTO** 关联单。
- 明确承认边界：实盘仍有 venue、传输、时序、持久化、**对账**等仿真无法复现的差异，
  并单独维护 "Backtest and live differences" 文档。
- 注意：README **并未**声称撮合引擎在回测与实盘间复用——复用的是**执行算法与订单语义**。

> 对我们的启示：**把"回测-实盘差异"当成一等公民文档化，而不是假装不存在**。
> 这也直接决定了本项目 `engine/backtest.py` 与 `engine/live.py` 的关系设计：
> 共享策略/风控/OMS，只替换 Clock 与 Broker，并把剩余差异写进文档与对账逻辑。
> 不采纳：Rust 内核（1m 频率下 Python 的 GIL 不是瓶颈，撮合是 O(挂单数)）。

### 2.3 Hummingbot：**Executor 模式 —— 订单生命周期是一等公民**

- `Connector` 抽象抹平 CEX/CLOB DEX/AMM DEX 三类场所。
- V2 架构把「策略逻辑（Controller）」与「订单生命周期（Executor）」拆开，Controller 的配置
  可以 **backtest → deploy → live tune**，同一份配置贯穿三种状态。
- Executor 是自包含单元，覆盖 `position` / `DCA` / `grid` / `arbitrage` / `TWAP` 等；
  `position_executor` 用 **triple-barrier**（止盈/止损/时间）做强制退出。
- 单实例一个 bot，状态边界清晰。

> 对我们的启示：**"订单生命周期"必须有一个专门的宿主（本项目的 `OrderManager`），
> 策略不该知道挂单、撤单、超时、部分成交**。triple-barrier 也正是本策略的出场骨架。

### 2.4 Qlib：**数据与计算的分离做到极致**

- 松耦合模块，可单独使用；数据层用紧凑格式 + `ExpressionCache` + `DatasetCache`，
  官方给的对比是同一份因子构造任务从 HDF5 的 184s 降到 7.4s。
- 表达式引擎定义因子，`DataHandler` 组装成模型输入；模型层与工作流层双向复用。
- 组合层有 `NestedExecutor`，可联合优化"组合决策"与"订单执行"两个层级。
- 提供 point-in-time 数据库、数据健康检查脚本。

> 对我们的启示：**缓存与增量计算是性能前提**（本项目用 `IndicatorSet` 做 O(1) 增量更新）；
> 回测要能跑得足够快，否则参数研究根本做不动。
> 不采纳：表达式因子引擎（超短线只有十几个特征，表达式系统的复杂度不划算）。

### 2.5 其余项目的定位差异

| 项目 | 强项 | 对本项目的可借鉴点 | 不适用原因 |
|---|---|---|---|
| Backtrader | `Cerebro` 事件循环、指标生态完整 | 事件驱动的主循环形态 | 单进程纯 Python、逐 bar 循环在大参数扫描下过慢 |
| VectorBT | 向量化，参数扫描快 100x+ | "先用向量化扫，再用事件驱动精验"的研究流程 | 路径依赖的止损/移动止损很难表达 |
| vn.py | `EventEngine` + `Gateway` + `App` 三段式 | Gateway 抽象与 App 插件化 | 面向国内柜台，重量级，依赖 C++ 扩展 |
| Jesse | 双引擎（向量化 + 逐 bar）、指标内置 | 简单一致的策略 API | 生态与交易所覆盖不如 freqtrade |

> 一个被反复验证的工程结论：**向量化与事件驱动不是二选一，而是串行互补**——
> 向量化做假设筛选，事件驱动做执行真实性验证。本项目只实现后者，前者交给外部工具，
> 但通过 `--param` 覆盖 + `scripts/grid_search.py` 保留了批量敏感性分析的能力。

---

## 3. 归纳：8 条跨项目共性

1. **分层 + 事件驱动**：数据 → 策略 → 组合 → 风控 → 执行，单向依赖，禁止反向调用。
2. **策略只产出信号，不决定仓位**：所有项目都把 sizing 放在策略之外（protections / wallets / risk engine）。
3. **回测与实盘共享"决策语义"**，有三种实现流派：
   - 复刻语义（freqtrade：回测重写一遍流程，但复用策略与风控函数）
   - 同一运行时（NautilusTrader：同一套事件内核）
   - 同一配置（Hummingbot：Controller 配置贯穿回测与实盘）
4. **交易所适配器 + 能力声明**：把场所差异收敛到数据（`_ft_has`），业务代码不写 if-else。
5. **统一订单模型 + 显式状态机 + 客户端订单 ID**：幂等是重试的前提。
6. **风控是可插拔的独立层**：冷却、连亏、回撤熔断、对锁，全部在入场前统一判定。
7. **数据层与计算层分离**：紧凑存储 + 缓存 + 增量指标，否则研究迭代速度不可接受。
8. **可观测性与对账**：结构化日志、交易台账、纸面交易前置、启动自检。

---

## 4. 本项目的取舍（明确说明"为什么不抄"）

| 维度 | 开源主流做法 | 本项目选择 | 理由 |
|---|---|---|---|
| 语言/运行时 | Rust 内核 或 多进程 | 纯 Python 3.11+ 单进程 | 1m 频率、单交易所、挂单数 < 10；瓶颈在交易所延迟而非 CPU |
| 依赖 | ccxt / pandas / talib / SQLAlchemy | **核心零第三方依赖**（标准库） | 交易系统攻击面越小越好；手写 8 个端点可审计；增量指标用 `deque` 即可 |
| 策略接口 | DataFrame 全量计算 | 增量 `Snapshot` + `Intent` | 1m 级全量重算会产生 GB 级内存抖动；增量实现与实盘天然一致 |
| 回测-实盘一致 | 复刻语义 / 同一内核 | 共享 Strategy/Risk/OMS，仅换 Clock+Broker | 少量代码即可获得同等保证，且不引入内核复杂度 |
| 撮合模型 | tick/L2 撮合 | bar 内 4 点路径 + 逆势优先 | 没有 L2 历史数据时，tick 级仿真是伪精度；逆势优先是**保守**假设 |
| 交易所覆盖 | 10~140 家 | 币安 UM 永续一家 | 覆盖多家意味着把每家的差异都塞进主流程；单所可以把差异写死并测透 |
| 风控 | 插件式 Protection | 固定分层（L1/L2/L3）+ 可枚举拒绝码 | 超短线的风控规则很少但必须**确定**，插件化反而增加不确定性 |
| 策略生态 | 用户策略目录 + 动态加载 | `@register` 注册表 + TOML 选型 | 同一个包内闭环，便于 CI 覆盖与类型检查 |

### 我们主动放弃的能力（写进风险清单）

- 不支持多交易所路由/套利（需要 `routing_policy`，复杂度高出一个量级）。
- 不做 L2 订单簿撮合仿真：策略里的 OBI 因子在回测中退化为 taker-buy 代理。
- 不做分布式/多进程：单机故障即停机，靠 kill switch 与对账兜底。
- 不做 tick 级回测：`aggTrade` 只用于实盘成交质量评估，不参与信号。

---

## 5. 与本地已有项目 `astra-quant-agent` 的对照

同机另有一个成熟的多 LLM 交易系统 `../../astra-quant-agent`，它不是"调查对象"而是
"参照物"——两者目标不同，因此架构选择也不同，对照能说明我们的取舍不是随意的。

### 5.1 它的架构事实（代码核实）

| 维度 | 观察 | 证据 |
|---|---|---|
| 控制面 | FastAPI + 多 Router（system/exchanges/risk/strategy/llm/gateway/auth） | `astra_backend/app.py` |
| 决策 | LLM 委员会：辩论 → 共识 → 政策（macro/trend/microstructure/contrarian 多角色） | `astra_backend/council/debate.py`（40KB）、`council_manager.py:412 execute_council_debate` |
| 风控 | 独立目录 + **纯函数闸门**：`risk_gates.py`、`venue_gate.py`、`circuit_breaker.py`、`cooldowns.py`、`sizing.py` | `astra_backend/execution/__init__.py` 模块清单 |
| 执行 | `scripts/trader/` 下 36 个模块，按生命周期拆分：`entry_execution` / `order_submit` / `order_lifecycle` / `position_exit` / `legs` / `scale_out` / `cloud_protection` | `scripts/trader/__init__.py` 逐模块职责表 |
| 交易所 | OKX + Binance 双所 + 路由策略 + 失败回退 | `astra_backend/exchanges/{okx,binance,registry,routing_policy}.py` |
| 调度 | **轮询式**：`JOBS = (JobSpec("trader","ai_factor_trader.py",15*60,1260), …)`，`ThreadPoolExecutor` 执行 | `astra_gateway/scheduler.py:48` |
| 主循环 | 单文件门面 76KB，`execute_portfolio()` | `scripts/ai_factor_trader.py:1284` |

### 5.2 与 scalpx 的关键差异

| 维度 | astra-quant-agent | scalpx | 差异原因 |
|---|---|---|---|
| 决策来源 | LLM 委员会（15 分钟一轮） | 确定性规则（每根 1m bar） | 决策延迟容忍度差 3 个数量级 |
| 驱动方式 | 定时轮询 + 子进程 | 事件驱动，bar 到达即处理 | 超短线必须在秒级响应 |
| 风控位置 | **独立纯函数闸门** | 独立 `RiskManager`（L1/L2/L3） | ✅ 共识：风控绝不能写在策略里 |
| 执行拆分 | 按生命周期拆 36 个模块 | 单个 `OrderManager` 内部分方法 | 模块粒度应与复杂度匹配；36 个模块对**单所单策略**是过度拆分 |
| 交易所 | 双所 + 路由 | 单所 | 单所可以把差异写死并测透 |
| 保护单 | `cloud_protection`（交易所侧 OCO） | `STOP_MARKET` + `TAKE_PROFIT_MARKET` + 手工互撤 | ✅ 共识：保护单必须在交易所侧，不能靠本地监控 |
| 回测一致性 | 独立回测引擎 | 共享 Strategy/Risk/OMS，只换 Broker | scalpx 更进一步：连撮合语义都通过同一 `Broker` 接口切换 |

### 5.3 从对照中确认的三条判断

1. **"风控独立成层 + 纯函数闸门"在两个完全不同的系统里都被验证了**——这是本项目
   `risk/manager.py` 设计的直接依据。
2. **LLM 决策与确定性执行必须解耦**（astra 用独立 gateway 隔离）。scalpx 没有 LLM，
   但同样保持"策略只出意图、执行层不理解策略"的边界。
3. **模块数量应该由复杂度决定，而不是由职责名词的数量决定**。astra 的 36 个 trader 模块
   服务于双所 + 多品种 + 分批进出 + 云保护，是合理的；scalpx 单所单策略，用一个
   `OrderManager` 反而更容易保证不变量（成交必有保护单）。

---

## 6. 结论

真正决定一个交易系统能不能用的，不是策略有多花哨，而是这三件事：

1. **回测与实盘是否是同一套决策代码**（否则回测只是自我安慰）；
2. **任何时刻的持仓是否都有确定性的保护单**（否则一次断网就归零）；
3. **异常是否有明确归属**（拒单、超时、部分成交、对账不一致，各自有确定的处理路径）。

本项目把这三件事作为架构的硬约束，并在代码里用测试固化（见 `tests/test_system.py`
的架构依赖测试与 `tests/test_execution.py` 的保护单测试）。
