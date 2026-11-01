# 交易规格：标的、信号、仓位、风控、执行

本文是系统的"交易合同"。任何参数改动都应先改本文，再改 TOML。

---

## 1. 交易标的与时间粒度

| 项 | 取值 | 说明 |
|---|---|---|
| 交易所 | Binance USDⓈ-M 永续（UM Futures） | 单一场所，不做跨所路由 |
| 标的 | `BTCUSDT`（默认），可扩展 `ETHUSDT` | 流动性最好、点差最小 |
| 信号周期 | **1 分钟**（已收盘 bar） | 超短线的信息密度与噪声的最佳平衡点 |
| 辅助周期 | 无独立订阅，趋势由 1m 的 EMA(144) + 斜率隐含 | 避免多周期对齐的复杂度 |
| 持仓周期 | 目标 5~30 分钟，硬上限 40 根 1m（风控） | 与止盈距离/波动率匹配 |
| 合约/精度 | `price_tick=0.1`，`qty_step=0.001`，`min_notional=5` | 从 `exchangeInfo` 同步 |
| 交易时段 | 7×24 | 波动率过滤代替时段过滤 |

**为什么是 1 分钟而不是 15 秒 / tick**：15 秒以下噪声占比压倒信号，且手续费/滑点在
每笔收益中的占比会上升到不可接受的水平（见 §7 成本分析）。1 分钟是"信号可解释、
成本可覆盖"的下限。

---

## 2. 入场信号（趋势回踩确认）

核心思想：**只在"趋势中的短暂休息结束"这一刻进场**。纯突破在 1m 级别假信号率过高，
纯均值回归在趋势日会持续逆势。四重条件同时成立才动手。

### 2.1 符号与变量

| 变量 | 定义 |
|---|---|
| `EMA_f` | `close` 的 21 期 EMA |
| `EMA_s` | `close` 的 55 期 EMA |
| `EMA_t` | `close` 的 144 期 EMA |
| `slope` | `(EMA_t - EMA_t[20]) / EMA_t[20]` |
| `ATR` | 14 期 Wilder ATR |
| `ATR%` | `ATR / close` |
| `RSI` | 14 期 Wilder RSI |
| `VOL_x` | `volume / SMA(volume, 20)` |
| `FLOW` | `taker_buy_base / volume`（主动买占比） |

### 2.2 多头入场（全部满足）

| 编号 | 条件 | 参数 | 作用 |
|---|---|---|---|
| E1 | `EMA_f > EMA_s` 且 `slope > 0` | `ema_fast/slow/trend`, `trend_slope_lookback` | 只做顺势，过滤逆势抄底 |
| E2 | `atr_min_pct ≤ ATR% ≤ atr_max_pct` | `0.0008 / 0.0060` | 过滤死盘（无波动）与瀑布（风险不可控） |
| E3 | `VOL_x ≥ vol_mult` | `1.10` | 要求有真实参与度，避免噪声 bar |
| E4 | 最近 3 根内 `min(low) ≤ EMA_f + 0.30·ATR` | `pullback_lookback=3`, `pullback_atr_mult=0.30` | 价格必须回踩到均线带，而不是追高 |
| E5 | `close > EMA_f` 且 `close > open` | — | 回踩结束、重新收复，且当根为阳线 |
| E6 | `RSI_prev < 50 ≤ RSI` 且 `RSI < 70` | `rsi_cross=50`, `rsi_long_max=70` | 动量由弱转强，但不追超买 |
| E7 | 微观结构过滤（见 §2.4） | `flow_mode` / `min_delta_ratio` / `max_absorption` / `min_obi` | 主动买盘占优 + 未被吸收 + 盘口不逆风 |

空头完全镜像：`EMA_f < EMA_s`、`slope < 0`、`max(high) ≥ EMA_f - 0.30·ATR`、
`close < EMA_f` 且为阴线、`RSI_prev > 50 ≥ RSI` 且 `RSI > 30`、`FLOW ≤ 0.48`。

### 2.4 E7 微观结构过滤（三级降级）

超短线唯一能拿到"先手信息"的地方是成交流与盘口。E7 按数据可用性分三级：

| 级别 | 数据源 | 判定 | 记录在信号 meta |
|---|---|---|---|
| L1 | **aggTrade 聚合的真实 CVD** | `delta_ratio ≥ min_delta_ratio` 且 `cvd_slope ≥ 0` 且 `absorption ≤ max_absorption` | `micro_mode="agg_trade"` |
| L2 | bar 的 `taker_buy_base` 占比 | `taker_buy_ratio ≥ flow_min_long` | `micro_mode="taker_proxy"` |
| L3 | 都没有 | 放行（不伪造成 0） | `micro_mode="none"` |

L1 的三个量（`microstructure/flow.py`）：

```
delta_ratio = (主动买量 − 主动卖量) / (主动买量 + 主动卖量)      # [-1, 1]
cvd_slope   = Σ delta / Σ volume （最近 cvd_window 根 bar）      # [-1, 1]，可跨品种比较
absorption  = (bar 内累计 delta 峰值 − 收盘 delta) / (峰值 − 谷值)  # [0, 1]
```

`absorption` 是 taker 占比**无法表达**的时序信息：主动买盘先冲到 +96 手、收盘只剩 +10 手，
说明买盘被吃掉（衰竭），而不是"买盘占优"。这正是超短线最重要的反转证据。

盘口（`microstructure/book.py`，`depth20@100ms`）是**与成交流正交**的独立过滤器：

```
imbalance   = (带宽内买盘深度 − 卖盘深度) / (两者之和)   # ±depth_bps 内，距离加权
microprice  = (bid_px·ask_qty + ask_px·bid_qty) / (bid_qty + ask_qty)
stack_score = (买盘堆叠档数 − 卖盘堆叠档数) / (两者之和)  # 见下
```

**堆叠失衡**（`microstructure/imbalance.py`）：单档失衡可能是冰山单噪声，
连续 `stack_range` 档以上同向主导才代表一段"价格墙"。

```
逐档配对：第 i 档买 vs 第 i 档卖（距中间价等距）
side = +1  if bid_qty/ask_qty >= stack_ratio
       -1  if ask_qty/bid_qty >= stack_ratio
        0   otherwise（含任一侧为空、或总量 < min_level_qty）
连续同向 >= stack_range 档 -> 记为一段堆叠
```

> 为什么**不是** freqtrade 那种"价格轴对角线"（`ask.shift(-1)`）：那是为**成交剖面**设计的
> （每个价位都可能有主动买卖）。盘口阶梯两侧天然不重叠，若按价格对角线比较，
> "对手方为空 ⇒ 比率无穷大 ⇒ 误判失衡"，会把稀疏档位全部判成卖盘墙。
> 本书用等距档位配对，这是 L2 深度的正确度量。该结论有测试锁定
> （`tests/test_microstructure.py::TestStackedImbalance`）。

启用条件：`min_obi > 0` 或 `min_stack_score > -1`。多头要求
`imbalance ≥ min_obi`、`microprice_bias_bps ≥ 0`、`stack_score ≥ min_stack_score`；空头镜像。

> **fail-closed**：一旦声明要看盘口（上述任一阈值启用），`ctx.book is None` 时**直接拒绝入场**，
> 而不是"当作没这回事"。这条规则在回测里尤其重要——没有 L2 历史的场景必须显式失败，
> 否则会得到一个"看起来有盘口过滤、实际完全没生效"的假结论。

> **降级原则**：有数据一定用数据；没有数据降级而不是报错；`flow_mode="agg_trade"` 时
> 强制要求 L1（fail-closed，宁可错过不做）。回测可通过
> `--param flow_mode=taker_proxy|off` 做 A/B，直接量化这条过滤的贡献。

### 2.3 出场规则（由策略管理 + 交易所保护单兜底）

| 编号 | 规则 | 参数 | 实现位置 |
|---|---|---|---|
| X1 | 初始止损 `entry ∓ stop_atr_mult·ATR`，再被夹到 `[min_stop_pct, max_stop_pct]` | `stop_atr_mult=2.0`, `0.4%~0.8%` | 风控 sizing 夹紧 → 交易所 `STOP_MARKET` |
| X2 | 止盈 `entry ± rr·实际止损距离` | `rr=1.5` | 交易所 `TAKE_PROFIT_MARKET` |
| X3 | 浮盈 ≥ 1.0R 时止损推到 `entry ± 0.1R`（保本） | `be_at_r=1.0`, `be_offset_r=0.1` | 策略 → OMS 撤旧挂新 |
| X4 | 浮盈 ≥ 1.5R 后按 `close ∓ 1.2·ATR` 跟踪 | `trail_at_r=1.5`, `trail_atr_mult=1.2` | 同上（单调收紧） |
| X5 | 持仓 ≥ 30 根且浮盈 < 0.5R → 平仓 | `max_hold_bars=30` | 策略 |
| X6 | `EMA_f` 反向穿越 `EMA_s` 且浮盈 < 1R → 平仓 | `trend_flip_exit=true` | 策略 |
| X7 | 风控硬上限：持仓 ≥ `risk.max_bars_held`(40) 无条件平 | `max_bars_held` | OMS（策略可能忘记） |
| X8 | 日亏熔断 / 连亏熔断 / kill switch → 全部强平 | 见 §4 | `RiskManager` |

**X1 与 X2 的联动细节（容易出错的地方）**：风控把止损从 ATR 距离夹紧到 `min_stop_pct`
之后，必须**按原 R 倍数重算止盈**，否则会出现"止损被拉宽、止盈没动"，
实际盈亏比远低于设计值。该逻辑在 `risk/sizing.py::size_position` 中固化。

---

## 3. 仓位计算（风险预算法）

```
stop_distance = |entry - stop|                     # 已被夹到 [min_stop_pct, max_stop_pct]
qty_risk      = equity * risk_per_trade / stop_distance
qty_cap       = min(equity * max_notional_pct, equity * max_leverage) / entry
qty           = floor_step(min(qty_risk, qty_cap), qty_step)
```

- 默认 `risk_per_trade = 0.25%`，即单笔最大亏损 25 USD（以 1 万本金计）。
- 波动放大 → `stop_distance` 变大 → 仓位自动缩小，**风险敞口恒定**。
- `min_stop_pct=0.4%` 不只是风控偏好，更是**成本约束**：止损越窄，名义仓位越大，
  手续费在每笔中的占比越高（见 §7）。

拒绝条件（返回可枚举 code）：`SIZE_TOO_SMALL`、`NOTIONAL_TOO_SMALL`、`STOP_TOO_TIGHT`。

---

## 4. 风控三层

### L1 事前（`RiskManager.pre_trade`，唯一可否决入场的函数）

| code | 触发条件 | 默认阈值 |
|---|---|---|
| `RISK_HALTED` | 已熔断 | — |
| `COOLDOWN` | 连亏冷却中 | 连亏 3 笔 → 冷却 30 分钟 |
| `DAILY_TRADE_LIMIT` | 当日笔数超限 | 40 笔 |
| `MAX_POSITIONS` | 持仓数达上限 | 2 |
| `SYMBOL_POSITION_EXISTS` | 该标的已有持仓 | 1（不加仓、不反手） |
| `SYMBOL_COOLDOWN` | 出场后冷却 | 3 根 1m |
| `SPREAD_TOO_WIDE` | 点差过宽 | 4 bps |
| `LEVERAGE_CAP` | 剩余杠杆空间不足 | 上限 3x |
| `NO_LIQUIDITY` | 当根无成交量 | — |
| `COST_TOO_HIGH` | 往返成本 / 止损距离 超限 | `max_cost_ratio=0.35`（即手续费最多吃掉 0.35R） |
| `BOOK_UNTRUSTED` | 盘口数据劣化（丢包/乱序/过期比例超阈值） | `max_bad_ratio=0.5`，仅当策略依赖盘口时生效 |
| `SIZE_TOO_SMALL` / `NOTIONAL_TOO_SMALL` | 仓位过小 | 交易所最小额 |

> `COST_TOO_HIGH` 是超短线最关键的一道闸门。它把 §7 的成本分析变成可执行规则：
> 止损 0.40% 时成本占 0.18R（放行），止损 0.15% 时占 0.47R（拒绝）。
> 计算见 `risk/costs.py::round_trip_cost / cost_to_risk_ratio / net_rr`。

### L2 事中（`OrderManager.advance`，每根 bar）

| 检查 | 动作 |
|---|---|
| 持仓无保护单 | 由 `pos.stop_price` 补挂；补挂失败 → **立即市价强平** |
| 持仓无止损价（数据异常） | 立即强平（`forced_flatten`） |
| 持仓 ≥ 硬上限 | 无条件市价平仓（`TIME_STOP`） |
| 入场挂单超时 | 撤单 → 降级市价（最多 `entry_max_retries` 次） |
| 成交找不到归属订单 | 计 `unmatched_fills` 并告警（属严重 bug） |

### L3 事后（成交回报后）

| 规则 | 动作 |
|---|---|
| 单笔亏损 | `consecutive_losses += 1` |
| 连亏 ≥ 3 | 冷却 `cooldown_minutes` |
| 连亏 ≥ 5 | 当日停止（`CONSECUTIVE_LOSS_HALT`） |
| 当日权益回撤 ≥ 2.5% | 当日熔断（`DAILY_LOSS`） |
| 连续 3 次订单被拒 | 熔断（`ORDER_REJECT_STREAK`） |

**熔断解除策略**：`DAILY_LOSS` / `CONSECUTIVE_LOSS_HALT` / `ORDER_REJECT_STREAK`
属于日级熔断，新交易日自动解除并清零连亏计数；`KILL_SWITCH` / `MANUAL` 必须人工 `resume()`。

---

## 5. 订单执行

### 5.1 入场：maker 优先，taker 兜底

```
1. 挂 post-only 限价单（买价 = close - max(1 tick, offset)，卖价反之）
   └─ 若交易所判定会立即成交 → POST_ONLY_REJECT → 立刻转步骤 3
2. 超时（entry_timeout_ms，回测粒度按 1 根 bar）未成交 → 撤单
3. 市价单（IOC）成交，最多重试 entry_max_retries 次
4. 成交瞬间（同一根 bar 内）挂 STOP_MARKET + TAKE_PROFIT_MARKET（reduceOnly + closePosition）
   └─ 止损挂不上 → 立即市价平仓（fail-closed）
```

### 5.2 出场：先撤保护单，再平仓

```
1. 撤销该标的全部挂单（含保护单）
2. 市价单 reduceOnly 平仓
3. 若保护单之一先触发 → 立即撤销另一侧（防止反向开仓）
```

顺序不能反：先平仓后撤单会出现"保护单在平仓后仍在挂"，触发后反向开仓。

### 5.3 异常处理矩阵

| 场景 | 判定依据 | 处理 | 是否计入"拒单连击" |
|---|---|---|---|
| post-only 会立即成交 | `-5022` / 模拟器 `POST_ONLY_REJECT` | 立即降级市价 | 否 |
| 精度/最小额错误 | `-1111` `-4014` `-4164` | 按 tick/step 重新取整后重试一次，仍失败则放弃 | 是 |
| 保证金不足 | `-2010` `-2019` | 放弃本次入场 | 是 |
| 时间戳漂移 | `-1021` | 重新 `sync_time` 并重试 | 否 |
| 限频 | `-1003` | 退避重试（0.25s×2^n） | 否 |
| 网络超时（结果未知） | 传输层异常 | **先 `query_order(origClientOrderId)` 确认**，确认未成交才重试 | 否 |
| 撤单时订单不存在 | `-2011` | 视为已撤/已成交，查询后同步 | 否 |
| 连续 3 次拒单 | 计数器 | 熔断当日 | — |
| 持仓无保护单 | L2 检查 | 补挂 → 失败则强平 | — |
| 账实不符 | 定时对账 | 交易所无仓→本地平账；交易所有未知仓→立即平掉并熔断 | — |

---

## 6. 关键参数配置项速查

| 组 | 参数 | 默认 | 调大/调小的后果 |
|---|---|---|---|
| 市场 | `market.interval` | `1m` | 变大→信号更少更稳；变小→噪声与成本占比上升 |
| 策略 | `vol_mult` | 1.10 | 变大→更少交易更高质量，但可能错过行情 |
| 策略 | `atr_min_pct` | 0.0008 | 变大→过滤死盘更狠，交易量下降 |
| 策略 | `pullback_atr_mult` | 0.30 | 变大→更早入场（更激进），胜率下降 |
| 策略 | `stop_atr_mult` | 2.0 | 变大→止损更宽、仓位更小、成本占比更低 |
| 策略 | `rr` | 1.5 | 变大→胜率下降但盈亏比上升；需与持仓周期匹配 |
| 策略 | `max_hold_bars` | 30 | 变大→给行情更多时间，但占用仓位与暴露 |
| 风控 | `risk_per_trade` | 0.25% | 唯一直接放大亏损的参数，实盘建议 0.1% |
| 风控 | `min_stop_pct` | 0.4% | 直接决定名义仓位与手续费占比 |
| 风控 | `max_daily_loss_pct` | 2.5% | 触及即当日停手 |
| 执行 | `entry_order_type` | `limit_post_only` | 改成 `market` 会显著提高成本 |
| 执行 | `maker_fee` / `taker_fee` | 2 / 4 bps | 必须按真实费率填，否则回测系统性偏乐观 |
| 执行 | `slippage_bps` | 1.0 | 实盘建议 2~5 bps |

---

## 7. 回测结果与成本分析

**数据说明**：`data/raw/*.csv` 是 `scripts/make_sample_data.py` 生成的**合成行情**
（含趋势/震荡/脉冲三段切换），用于验证工程链路，**不代表真实市场收益**。

基线回测（BTCUSDT 1m，30 天，初始 1 万 U，maker 2bps / taker 4bps）：

| 指标 | 数值 |
|---|---|
| 交易笔数 | 277（9.2 笔/日） |
| 胜率 | 44.0% |
| 盈亏比（profit factor） | 0.92 |
| 平均盈利 / 平均亏损 | +15.38 / -13.22 |
| 期望 R | **-0.025R** |
| 总收益 / 最大回撤 | -1.73% / 3.20% |
| 手续费合计 / 占初始权益 | 991.8 U / **9.92%** |
| 出场分布 | 时间止损 175、结构反转 65、止盈 23、止损 14 |
| 保护单挂出次数 | 554（= 277×2，无缺失） |
| 强制平仓次数 | **0** |

### 结论 1：这套参数在无 alpha 的数据上是"近似零期望"

合成数据是带漂移切换的随机游走，本身没有可利用的 edge。回测给出的期望 R 为 -0.025R，
其量级恰好等于**交易成本**（约 0.15~0.25R/笔）。这正是**引擎没有系统性造假**的证据：
没有 edge 时，回测就应该亏掉成本。

### 结论 2：在 1m 级别，成本是主要对手

```
每笔手续费 ≈ 2 × fee_rate × notional
notional   = risk_amount / stop_pct = (equity × 0.25%) / stop_pct
```

| `min_stop_pct` | 名义仓位（1 万本金） | 每笔往返成本 | 占单笔风险 |
|---|---|---|---|
| 0.15% | 16,667 U | ~10.0 U | **0.40R** |
| 0.25% | 10,000 U | ~6.0 U | 0.24R |
| **0.40%** | 6,250 U | ~3.8 U | **0.15R** |
| 0.60% | 4,167 U | ~2.5 U | 0.10R |

网格搜索（24 组：`rr × vol_mult × stop_atr_mult × min_stop_pct`）也印证了这一点：
`min_stop_pct=0.004` 的组合全面优于 `0.002`，收益从 -31.5% 提升到 -3.8%，
**不是策略变准了，而是成本被压下去了**。

### 结论 3：出场结构与持仓周期必须匹配

时间止损占 175/277，说明 `0.6%` 的止盈目标在 30 分钟内很难达到
（1m 的 15~30 分钟波动幅度通常只有 0.2~0.4%）。改进方向：

1. 用 **ATR 自适应目标**替代固定 R（目标 = k × ATR × √持有 bar 数）；
2. 或把 `rr` 降到 1.0~1.2 并提高 `stop_atr_mult`，让出场发生在波动率可达范围内；
3. 或把信号周期提升到 3m/5m，让持仓周期与目标距离重新对齐。

这些改动都在 `strategy/*.py` 与 `config/*.toml` 内完成，**不需要动引擎**。

### 结论 4：多标的可用但仍然受成本约束

将 `symbols` 改为 `["BTCUSDT","ETHUSDT"]` 后：86,400 根 bar、552 笔交易、
`forced_flatten=0`，但期望 R 降至 -0.15R（更多交易 × 相同成本 + 连亏熔断更频繁）。

---

## 8. 已知局限

| 局限 | 影响 | 缓解 |
|---|---|---|
| 无 L2 订单簿，OBI 因子用 taker-buy 代理 | 入场过滤精度不如真实盘口 | 实盘可接 `depth20@100ms` 替换该因子 |
| 撮合按 4 点路径且全额成交 | 低估部分成交与排队风险 | 保守使用逆势优先；实盘 maker 单常不成交 → 已用降级机制覆盖 |
| 未建模资金费率与强平 | 长持仓的成本被低估 | 持仓上限 40 分钟，费率影响极小 |
| 单一策略、单标的 | 无组合分散 | `MultiSymbolFeed` 已就绪，风控有相关性系数 |
| 参数基于合成数据标定 | 不能直接用于实盘 | 必须用真实数据重新做样本内/样本外验证 |
