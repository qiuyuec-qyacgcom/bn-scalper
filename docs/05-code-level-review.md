# 开源量化项目代码级评审：面向超短线的可复用设计

> 本文与 `01-open-source-research.md` 的分工：
> 01 是**架构层**调研（仓库首页/文档级）；本文是**代码层**评审——逐个打开源文件，
> 看具体类、方法、字段与判定顺序，只提炼**能被超短线直接复用**的东西。
>
> 事实标注规则：`[码]` = 源码中直接可见的类/方法/字段/字面量；`[推]` = 基于代码结构的推断。

---

## 1. 项目筛选标准

不追求"多"，而追求"每一行代码都能回答超短线的一个问题"。六条硬标准：

| # | 标准 | 判定方法 | 目的 |
|---|---|---|---|
| S1 | **有真实下单能力**，不是纯回测/研究库 | 仓库内存在交易所 connector 与订单状态机 | 超短线的难点在执行，不在信号 |
| S2 | **仍在维护** | 近 12 个月有实质提交 | 交易所 API 迭代快，停更项目的实现已失效 |
| S3 | **有独立的风控/保护机制** | 存在 protection / risk-gate / executor 等独立层 | 观察"风控放在哪里"比"风控写了什么"更重要 |
| S4 | **覆盖超短线的至少两个关键环节** | 手续费分档 / post-only / 条件单 / 盘口 / 逐笔 | 否则学不到成本与延迟相关的工程细节 |
| S5 | **架构可被单机系统借鉴** | 不依赖专有基础设施（K8s、券商柜台、商业数据源） | 保证结论可落地 |
| S6 | **源码可拉取且可读** | raw 文件可直接获取；无重型代码生成 | 保证本文结论可验证 |

**入选 6 个**：freqtrade、Hummingbot、NautilusTrader、Jesse、Qlib、vn.py
**范式对照 2 个**：Backtrader（事件驱动）、VectorBT（向量化）

**淘汰及原因**

| 项目 | 淘汰原因 |
|---|---|
| zipline / zipline-reloaded | 原项目停更；Pipeline 面向日频选股，无订单级执行细节 |
| QuantConnect LEAN | C# 生态，可读但移植成本高，且核心撮合与数据订阅重度依赖其云平台 |
| OctoBot / Zenbot | 架构偏"机器人集合"，缺可复用的执行层抽象 |
| alphalens / empyrical / pyfolio | 纯分析库，不含执行，无法回答本问 |
| 各类"策略合集"仓库 | 只有信号表达式，没有风控与执行，学不到超短线真正需要的部分 |

---

## 2. 分析维度

每个项目按同一把尺子量，避免"看到什么夸什么"：

| 维度 | 具体看什么代码 | 超短线关心的问题 |
|---|---|---|
| **D1 策略逻辑与信号表达** | 策略基类的钩子/生命周期、信号如何变成订单、是否有"意图"与"执行"的分离 | 进出场判定能否在**秒级**完成且不自我矛盾 |
| **D2 数据与微观结构** | 逐笔→K 线的聚合、订单流/失衡计算、盘口快照与增量合并、就绪判定 | 能否拿到**可信的**盘口/成交流信号 |
| **D3 风控与仓位** | 止损止盈的判定依据（价格 or 净盈亏）、连亏冷却、仓位公式位置 | 纪律能否**被代码强制**，而不是靠人 |
| **D4 订单执行与生命周期** | 订单状态机、重试/降级、激活区间、撤单与改单顺序、平仓量校验 | 延迟、滑点、重复下单、裸仓 |
| **D5 回测-实盘一致性** | 回测是否复用实盘判定链、时间语义、未来函数防护 | 回测结论能否外推 |
| **D6 工程与可观测性** | 异常分类、就绪门控、指标采集、降级策略 | 出故障时系统行为是否可预测 |

---

## 3. 逐项目代码级评审

### 3.1 Hummingbot `PositionExecutor` —— 屏障与挂单门控

**文件**：`hummingbot/strategy_v2/executors/position_executor/position_executor.py`（类 `PositionExecutor(ExecutorBase)`）

#### 代码事实

**① 屏障顺序固定且短路回查** `[码]`

```python
def control_barriers(self):
    if self._open_order and self._open_order.is_filled and ...:
        self.control_stop_loss()
        if self.status != RunnableStatus.RUNNING: return
        self.control_trailing_stop()
        if self.status != RunnableStatus.RUNNING: return
        self.control_take_profit()
        if self.status != RunnableStatus.RUNNING: return
        self.control_time_limit()
```

止损永远先于止盈判定，且一旦触发立刻 `return`——不存在"同一轮里既止损又止盈"的歧义。

**② 屏障判定用净盈亏（已扣手续费），不是价格** `[码]`

```python
def control_stop_loss(self):
    if self.net_pnl_pct <= -self.config.triple_barrier_config.stop_loss:
        self.place_close_order_and_cancel_open_orders(close_type=CloseType.STOP_LOSS)
```

```python
def get_net_pnl_quote(self) -> Decimal:
    return self.trade_pnl_quote - self.cum_fees_quote

def get_net_pnl_pct(self) -> Decimal:
    return self.net_pnl_quote / self.open_filled_amount_quote
```

代码里**没有**"止损价"这个下单参数——止损完全靠净盈亏比例触发后下发市价单。

**③ 止盈优先用 maker，并按盘口修正** `[码]`

```python
@property
def take_profit_price(self):
    if self.config.side == TradeType.BUY:
        take_profit_price = self.entry_price * (1 + self.config.triple_barrier_config.take_profit)
        if self.config.triple_barrier_config.take_profit_order_type == OrderType.LIMIT_MAKER:
            take_profit_price = max(take_profit_price,
                self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.BestAsk))
```

**④ 激活区间门控 + 越界撤单** `[码]`

```python
def control_open_order(self):
    if not self._open_order:
        if self._is_within_activation_bounds(self.config.entry_price, self.config.side,
                                             self.config.triple_barrier_config.open_order_type):
            self.place_open_order()
    else:
        if self._open_order.order and not self._open_order.is_filled and \
                not self._is_within_activation_bounds(...):
            self.cancel_open_order()
```

**⑤ 平仓量必须匹配才收尾** `[码]`

```python
def open_and_close_volume_match(self):
    if self.open_filled_amount == Decimal("0"):
        return True
    return self._close_order and self._close_order.is_filled
```

**⑥ 节拍与重试** `[码]`：`update_interval: float = 1.0`、`max_retries: int = 10`；
平仓重试路径 `self.cancel_open_orders(); await self._sleep(5.0)`。

#### 优点 → 超短线映射

| 优点 | 对应超短线底层要求 | 说明 |
|---|---|---|
| 净盈亏判定屏障 | **手续费控制** | 目标价扣费后才算"达标"，避免"看着赚实际亏" |
| TP 用 LIMIT_MAKER + 盘口修正 | **手续费控制** | 出场省下 taker 费；1m 级别这笔钱等于 0.2~0.4R |
| activation_bounds 门控 | **低延迟执行** | 价格已跑掉的单子不挂、已挂的撤掉，避免无效请求占用限频额度 |
| 屏障顺序 + 短路 | **仓位与止损纪律** | 止损不可被止盈"抢跑" |
| 平仓量匹配校验 | **低延迟执行** | 防止"以为平了其实只平一半" |
| ONEWAY 模式不用 reduceOnly `[码]` | 执行健壮性 | 规避部分交易所 reduce-only 拒单 |

#### 局限（不能照抄的部分）

- `update_interval=1.0s` 的轮询节拍对**秒级**超短线偏慢；本项目改为事件驱动（bar 收盘即以 bar 内路径撮合）。
- 止损走市价单，滑点不可控；超短线应尽量用**交易所原生条件单**（在交易所侧撮合，不经过本地延迟）。
- 完全不含盘口/订单流信号。

---

### 3.2 Freqtrade `StoplossGuard` + `binance.py` —— 冷却机制与场所能力声明

**文件**：`freqtrade/plugins/protections/stoploss_guard.py`、`freqtrade/exchange/binance.py`

#### 代码事实

**① 只统计"止损类离场"，不统计所有亏损** `[码]`

```python
trades = [trade for trade in trades1
    if (str(trade.exit_reason) in (
            ExitType.TRAILING_STOP_LOSS.value,
            ExitType.STOP_LOSS.value,
            ExitType.STOPLOSS_ON_EXCHANGE.value,
            ExitType.LIQUIDATION.value)
        and trade.close_profit and trade.close_profit < self._profit_limit)]
```

**② 滚动锁：解禁时间 = 最后一笔止损 + stop_duration** `[码]`

```python
if len(trades) < self._trade_limit:
    return None
until = self.calculate_lock_end(trades)     # 最后一笔 close_date + stop_duration
return ProtectionReturn(lock=True, until=until, reason=self._reason(),
                        lock_side=(side if self._only_per_side else "*"))
```

窗口内每来一笔新止损，解禁时间就顺延——高频止损期会持续锁仓。

**③ 方向隔离** `[码]`：`only_per_side=True` 时只统计同向止损，且 `lock_side` 变单一方向。

**④ 交易所差异是"数据"，不是 if-else** `[码]`

```python
_ft_has_futures: FtHas = {
    "stoploss_order_types": {"limit": "stop", "market": "stop_market"},
    "stoploss_blocks_assets": False,
    "stop_price_type_field": "workingType",
    "stop_price_type_value_mapping": {PriceType.LAST: "CONTRACT_PRICE", PriceType.MARK: "MARK_PRICE"},
    "stoploss_query_requires_stop_flag": True,
    "stoploss_algo_order_info_id": "actualOrderId",
    "ohlcv_candle_limit": 499,
    "fetch_orders_limit_minutes": 7 * 1440,
    "order_props_in_contracts": ["amount", "cost", "filled", "remaining"],
}
```

**⑤ 资金费的 15 秒宽限** `[码]`

```python
def funding_fee_cutoff(self, open_date: datetime):
    return open_date.minute == 0 and open_date.second < 15
```

**⑥ 异常三分类** `[码]`：`DDoSProtection` → 限频；`TemporaryError`（可重试）；`OperationalException`（致命）。

**⑦ 启动期拒绝不兼容账户模式** `[码]`：Hedge Mode / Multi-Asset Mode 直接抛 `OperationalException`。

#### 优点 → 映射

| 优点 | 对应要求 | 说明 |
|---|---|---|
| 只统计止损类离场 | **止损纪律** | 区分"策略失效"与"正常盈亏"，不会因一次止盈掩盖连续止损 |
| 滚动冷却窗口 | **止损纪律** | 冷却随新止损顺延，天然抑制"报复性交易" |
| 方向隔离 | **进出场时机** | 单边行情只停一边，保留另一侧机会 |
| 能力声明数据化 | **低延迟执行** | post-only 支持、K 线上限 499、查单 7 天窗口一次说清，避免运行期才发现 |
| `workingType=MARK_PRICE` | **止损纪律** | 用标记价而非最新价触发，减少插针扫损——超短线最致命的细节之一 |
| 异常三分类 | **低延迟执行** | 直接决定"能否重试"，是幂等的前提 |

#### 局限

- `lookback_period` / `stop_duration` 以**分钟**计，对秒~分钟级超短线粒度太粗（本项目的冷却改为"bar 数"）。
- protections 是"入场前检查"，不覆盖"持仓中保护单丢失"这类执行层故障。

---

### 3.3 Jesse `Strategy` —— 生命周期与"先校验后撤单"

**文件**：`jesse/strategies/Strategy.py`

#### 代码事实

**① 一次执行内价格锁定 + 防重入** `[码]`

```python
def _execute(self) -> None:
    if self._is_executing: return
    self._is_executing = True
    self._cached_price = self.close
    self.before(); self._check(); self.after()
    ...
    self._cached_price = None
    self._is_executing = False
```

**② 信号互斥强校验** `[码]`：`should_long` 与 `should_short` 同时为真直接抛 `ConflictingRules`；
spot 下 `should_short` 为真抛 `InvalidStrategy`。

**③ 保护单"先校验新值，再撤旧单"** `[码]`

```python
@staticmethod
def _validate_exit_order_values(orders: np.ndarray, name: str) -> None:
    # Runs before the existing exit orders are cancelled. A NaN price ... would otherwise be rejected
    # only after the protective order is already gone, leaving the position unprotected.
    for qty, price in orders:
        if not np.isfinite(qty) or not np.isfinite(price):
            raise exceptions.InvalidStrategy(...)
```

**④ 止损方向错误自动降级为市价** `[码]`

```python
if self.is_long and o[1] >= self.position.entry_price:
    self.broker.sell_at_market(o[0])   # 多头止损价高于入场价 → 直接市价出
elif self.is_short and o[1] <= self.position.entry_price:
    self.broker.buy_at_market(o[0])
```

**⑤ filters 是可插拔的进场否决器** `[码]`：返回**函数对象列表**（不带括号），任一 `False` → `_reset()` 放弃本次进场，且 filters 在提交订单**之前**执行。

**⑥ SL/TP 完全相同直接报错** `[码]`。

#### 优点 → 映射

| 优点 | 对应要求 | 说明 |
|---|---|---|
| 先校验后撤单 | **止损纪律** | 消除"旧保护单已撤、新单被拒"的裸仓窗口，这是超短线最危险的一类 bug |
| 一次执行内价格缓存 | **进出场时机** | 同一判定内用同一价格，避免"信号用 A 价、止盈用 B 价" |
| filters 与信号分离 | **成本控制** | 点差/波动/时段过滤器可插拔，不改策略主体 |
| SL 方向异常降级市价 | 纪律 | fail-safe 而非 fail-silent |
| 信号互斥校验 | 时机 | 结构上排除"同时看多做空" |

#### 局限

- 仓位由策略自己算（`self.buy = (qty, price)`），框架层没有风险预算概念。
- 无盘口/订单流；信号只能基于 OHLCV 与内置指标。

---

### 3.4 Freqtrade `orderflow.py` —— 从逐笔到微观结构因子

**文件**：`freqtrade/data/converter/orderflow.py`

#### 代码事实

**① 主动买卖只用成交方向判定** `[码]`

```python
bid = np.where(trades_grouped_df["side"].str.contains("sell"), trades_grouped_df["amount"], 0)
ask = np.where(trades_grouped_df["side"].str.contains("buy"),  trades_grouped_df["amount"], 0)
```

**② 价格分箱 + Delta** `[码]`

```python
df["price"] = ((trades["price"] / scale).round() * scale).astype("float64")
df["delta"] = df["ask_amount"] - df["bid_amount"]
df = df.groupby("price").sum(numeric_only=True)
```

**③ 线内累计 Delta 的极值（本根 bar 内的吸收/衰竭）** `[码]`

```python
deltas_per_trade_cumsum = (ask - bid).cumsum()
dataframe.at[index, "max_delta"] = deltas_per_trade_cumsum.max()
dataframe.at[index, "min_delta"] = deltas_per_trade_cumsum.min()
```

**④ 失衡是沿"价格轴"比较，不是时间轴** `[码]`

```python
bid = df.bid
ask = df.ask.shift(-1)                      # 索引是分箱价格
bid_imbalance = (bid / ask) > imbalance_ratio
volume_check_mask = df.total_volume < imbalance_volume
bid_imbalance_filtered = np.where(volume_check_mask, False, bid_imbalance)
```

**⑤ 堆叠失衡** `[码]`：连续 `stacked_imbalance_range` 个价位同时失衡，输出区间起始价列表。

**⑥ 防未来函数 + 缓存** `[码]`

```python
df["candle_start"] = pd.to_datetime(df["date"], unit="ms").dt.floor(timeframe_frequency)
df["candle_end"]   = df["candle_start"] + dofs     # 交给 _now_is_time_to_refresh_trades 判定是否闭合
...
cached_grouped_trades = dataframe.tail(config_orderflow["cache_size"]).copy()
```

#### 优点 → 映射

| 优点 | 对应要求 | 说明 |
|---|---|---|
| 用成交方向算 delta | **微观结构** | 比"涨了就算买"可靠得多，是 CVD 的正确口径 |
| 线内累计 delta 极值 | **进出场时机** | 捕捉"同根 bar 内买盘被吸收"，这正是超短线反转的第一手证据 |
| 堆叠失衡 | **进出场时机** | 连续价位失衡 = 支撑/阻力强度，可用于"回踩在哪里接" |
| 低量过滤 | 微观结构 | 避免零成交价位产生 `inf` 误判 |
| 整根闭合才算完整 | 一致性 | 明确"未闭合 bar 的订单流语义不完整" |

#### 局限

- 强依赖逐笔成交数据；只有 OHLCV 时必须退化（如本项目用 `taker_buy_base/volume` 代理）。
- `bid/ask` 除零产生 `inf` 被当成 `True`，仅靠低量过滤兜底 `[码]`。

---

### 3.5 Hummingbot `OrderBookTracker` —— 盘口可信度工程

**文件**：`hummingbot/core/data_type/order_book_tracker.py`

#### 代码事实

**① 单汇流 → 按交易对 fan-out → 每对一协程** `[码]`：`_order_book_diff_stream` / `_order_book_snapshot_stream` / `_order_book_trade_stream` 三个全局队列，路由后进入 `_tracking_message_queues[pair]`。

**② UID 水位过滤** `[码]`

```python
if order_book.snapshot_uid > ob_message.update_id:
    messages_rejected += 1
    continue
```

**③ 快照到达即整体重建 + 重放最近 32 条 diff** `[码]`

```python
if message.type is OrderBookMessageType.DIFF:
    order_book.apply_diffs(message.bids, message.asks, message.update_id)
    past_diffs_window.append(message)
elif message.type is OrderBookMessageType.SNAPSHOT:
    order_book.restore_from_snapshot_and_diffs(message, list(past_diffs_window))
```

`PAST_DIFF_WINDOW_SIZE = 32` `[码]`。

**④ 快照前的 diff 先进缓冲** `[码]`：`_saved_message_queues[pair]`（`maxlen=1000`），
消费循环每轮**优先**消费缓冲，保证 UID 连续。

**⑤ `ready` 是 Event，且有非对称闸门** `[码]`：`_order_books_initialized.set()` 在所有 pair 的
REST 首簿建完后才 set；三个消费者 `await wait()`，而 diff router **不**等（必须先收缓冲）。

**⑥ 永不退出、5 秒重试** `[码]`

```python
except asyncio.CancelledError:
    raise
except Exception:
    self.logger().network(..., app_warning_msg="... Retrying after 5 seconds.")
    await asyncio.sleep(5.0)
```

**⑦ REST 兜底只修 `last_trade_price`** `[码]`：3 分钟无 WS 成交且距上次 REST 更新 > 5 秒才补，
且**不修深度**。

**⑧ 明确不内置买卖压力** `[码]`：`snapshot` 是唯一对外读取入口，OBI 由调用方算。

#### 优点 → 映射

| 优点 | 对应要求 | 说明 |
|---|---|---|
| UID 水位过滤 | **微观结构** | 丢弃被快照覆盖的旧 diff，盘口不"退档" |
| 快照重放 32 条 diff | 微观结构 | 断线重连后盘口立即一致，不产生假的失衡信号 |
| ready 语义 + 三道闸门 | **进出场时机** | 盘口未就绪绝不参与决策，避免用空盘口算出 OBI=0 |
| 缓冲优先消费 | 一致性 | 保证"先到的 diff 先应用"，UID 才连续 |
| 5 秒重试保留状态 | 低延迟执行 | 单对故障不影响整个策略循环 |
| 只暴露 snapshot，不算 OBI | 架构 | 指标留在策略层，框架不替策略做决定 |

#### 局限（可被本项目借鉴的反面教材）

- `_saved_message_queues` 溢出（>1000 条）会静默丢 diff，**没有 gap 检测**。
- `ready=True` 只代表"首簿已建"，**不代表 WS 已对齐**——用它当"可以交易"的判据是错的。
- metrics 的延迟均值采样口径不一致（分子采样、分母全量），会系统性低估延迟。

---

### 3.6 三个"只取一点"的对照项目

| 项目 | 可取的一点 | 代码依据 |
|---|---|---|
| **NautilusTrader** | **OCO/OUO/OTO 关联单**的原子语义；纳秒级统一时间模型；官方明确"回测≠实盘"并单列文档 | `[README]` 订单有效性/执行指令列表；Project 声明 deterministic event-driven |
| **vn.py** | `EventEngine + Gateway + App` 三段式：行情与交易都走同一条事件总线，App 只订阅 | `[社区]` 目录结构 |
| **VectorBT** | 向量化先扫参、事件驱动再精验的**研究流程**，而非单个实现 | `[社区]` |

对超短线的直接启示：**OCO 的原子性**——止盈成交后止损必须立即消失，否则会反向开仓。
本项目用"成交回调里立即 `_cancel_symbol_orders`"实现同等效果（`oms.py::_handle_fill`）。

---

## 4. 可复用设计模式（R1–R12）

| # | 模式 | 来源 | 一句话 | 超短线为什么需要 |
|---|---|---|---|---|
| **R1** | 净盈亏屏障 | Hummingbot | 用"扣费后的盈亏"而非价格判定止盈止损 | 1m 级别 0.07% 的往返成本 ≈ 0.18~0.47R，用价格判定必然高估 |
| **R2** | 屏障固定优先级 | Hummingbot | 止损 → 移动止损 → 止盈 → 时间，逐级短路 | 消除"同根 bar 内先止盈"的乐观歧义 |
| **R3** | maker 优先出场 | Hummingbot | 止盈挂 LIMIT_MAKER，并按 BestBid/Ask 修正价格 | 省下的 taker 费直接等于提高期望 R |
| **R4** | 激活区间门控 | Hummingbot | 价格偏离参考价过远不下单、已挂则撤 | 降低无效请求与追高/追低 |
| **R5** | 平仓量匹配校验 | Hummingbot | 开平成交量必须对齐才允许收尾 | 防"以为平了其实只平一半" |
| **R6** | 滚动冷却 | freqtrade | 解禁时间 = 最后一笔止损 + 冷却时长 | 连续止损期自动延长停手，抑制报复交易 |
| **R7** | 只统计止损类离场 | freqtrade | 用 `exit_reason` 白名单统计 | 精准识别"策略失效"而非普通亏损 |
| **R8** | 场所能力声明化 | freqtrade | 用字典声明 post-only/条件单/K 线上限/查单窗口 | 换所不改业务代码；运行期不踩隐藏限制 |
| **R9** | 标记价触发止损 | freqtrade | `workingType=MARK_PRICE` | 减少插针扫损 |
| **R10** | 先校验后撤单 | Jesse | 新保护单参数合法后才撤旧单 | 消除裸仓窗口（超短线最危险 bug） |
| **R11** | 成交方向口径的 orderflow | freqtrade | delta = 主动买 − 主动卖；线内累计极值；堆叠失衡 | 盘口/成交流是超短线唯一的"先手信息" |
| **R12** | UID 水位 + 快照重放 + ready 闸门 | Hummingbot | 盘口必须"可证明一致"才可用于决策 | 否则 OBI 因子全是噪声 |

---

## 5. 映射矩阵：模式 × 五条超短线底层要求

图例：`✅ 已落地` / `🔶 部分落地` / `❌ 未落地`

| 超短线底层要求 | 具体机制 | 来源 | 状态 | 落地位置 |
|---|---|---|---|---|
| **① 高频进出场时机** | 屏障固定优先级（先止损后止盈） | R2 | ✅ | `execution/simulator.py::_intrabar_path`（逆势优先排序）+ 交易所侧条件单 |
| | 一次决策内价格锁定 | Jesse | ✅ | `indicators.Snapshot`（信号只用 `bar.close`，下单用同一 `ref_price`） |
| | 信号互斥校验 | Jesse | ✅ | `strategy/trend_pullback.py` 多空条件互斥 + `validate_params` |
| | 时间屏障（超时平仓） | Hummingbot | ✅ | X5 策略软限 30 bar + X7 风控硬限 40 bar |
| | filters 可插拔否决 | Jesse | ✅ | `risk/manager.py::pre_trade` 10 个可枚举拒绝码 |
| | 盘口就绪才可交易 | R12 | ✅ | `BookTracker.get()` 过期即返回 None；`min_obi>0` 时无盘口直接否决 |
| **② 低延迟执行** | 事件驱动而非轮询 | 本项目 | ✅ | `engine/backtest.py` / `engine/live.py`（bar 收盘即处理） |
| | 成交即挂保护单（同步回调） | 本项目 | ✅ | `simulator.set_fill_sink` → `oms.handle_fill` |
| | 下单幂等 + 超时查单 | freqtrade 异常分类 + 本项目 | ✅ | `binance_broker.py::_query_after_error`、`new_client_order_id` |
| | maker→taker 降级 | Hummingbot | ✅ | `oms.py::_sweep_entry_timeouts` |
| | 激活区间门控（跑价撤单） | R4 | ❌ | 当前只有超时撤单，无"跑价即撤" |
| | 异常三分类重试 | freqtrade | ✅ | `binance_broker._RETRYABLE_CODES / _REJECT_CODES` |
| **③ 滑点与手续费控制** | 净盈亏口径的成本模型 | R1 | ✅ | `risk/costs.py`（往返成本、成本/风险比、净盈亏比） |
| | **成本闸门**（成本/止损 ≤ 0.35R） | R1 衍生 | ✅ | `risk/manager.py::pre_trade` → `COST_TOO_HIGH` |
| | maker 优先入场 + 至少让 1 tick | Hummingbot | ✅ | `oms.py::_build_entry_order` |
| | 止盈 maker 化 | R3 | ❌ | 当前 TP 用 `TAKE_PROFIT_MARKET`（taker） |
| | 用标记价触发止损 | R9 | ✅ | `binance_broker._to_exchange_params`：`workingType=MARK_PRICE` |
| | 点差闸门 | freqtrade | ✅ | `max_spread_bps` + `OrderBookSnapshot.spread_bps` |
| **④ 仓位与止损纪律** | 风险预算法定仓 | freqtrade/Hummingbot | ✅ | `risk/sizing.py::size_position` |
| | 止损夹紧 + R 倍数重算 | 本项目（评审中发现的问题） | ✅ | `sizing.py`（夹紧后按原 RR 重算 TP） |
| | 滚动冷却 | R6 | ✅ | `RiskManager.on_trade_closed`（每笔亏损重置 `cooldown_until`） |
| | 只统计止损类离场 | R7 | ✅ | `on_trade_closed` 按 `trade.pnl` 与 `ExitReason` 分支 |
| | 先校验后撤单 | R10 | ✅ | `Portfolio.update_stop`（单调收紧）→ `oms.adjust_stop`（失败回滚旧价） |
| | 无保护单即强平 | 本项目 | ✅ | `oms.advance` L2 检查 + `forced_flatten` 计数 |
| | 方向隔离熔断 | freqtrade | ❌ | 当前熔断是全局的 |
| **⑤ 盘口与微观结构信号** | 主动买占比代理（无逐笔时的降级） | freqtrade orderflow | ✅ | `models.Bar.taker_buy_ratio` → 策略 E7 条件 |
| | delta / CVD 线内累计极值 | R11 | ✅ | `microstructure/flow.py`（`FlowAggregator` + `FlowFeatureBuilder`） |
| | 堆叠失衡 | R11 | ✅ | `microstructure/imbalance.py`（等距档位配对，非价格对角线） |
| | OBI（订单簿失衡）+ 微价格 | R12 | ✅ | `microstructure/book.py` + 策略 E7（`min_obi`） |
| | 盘口质量门控（拒绝率/乱序率） | R12 | ✅ | `BookTracker.quality()` + `BOOK_UNTRUSTED` 熔断 + `BOOK_QUALITY` 事件 |

---

## 6. 整合结论

### 6.1 本次评审直接落地的代码改动

**成本闸门**（新增 `scalpx/risk/costs.py` + `RiskManager` 接线）

评审中最有普适价值的一条是 Hummingbot 的 **"用净盈亏判定屏障"**（R1）。本项目此前用价格判定，
而 `docs/03` 的成本分析已经证明：往返成本 7bps 在 0.4% 止损下等于 0.18R，在 0.15% 止损下
等于 0.47R。也就是说**止损越窄，策略越可能"设计上就不可盈利"**，这一点必须在事前拦住：

```python
# scalpx/risk/manager.py :: pre_trade
cost = round_trip_cost(exec_cfg, entry_is_maker=..., exit_is_maker=False, spread_bps=ctx.spread_bps)
cost_ratio = cost_to_risk_ratio(cost.total, stop_pct)
if cost_ratio > cfg.max_cost_ratio:          # 默认 0.35
    return RiskDecision.reject("COST_TOO_HIGH", f"往返成本 {cost.as_bps():.1f}bps 占止损 {cost_ratio:.2f}R")
```

同时把 `net_rr`（扣费后的真实盈亏比）写进 `RiskDecision.message`，让每笔决策都可解释。

验证（46 个测试通过、基线结果不变）：

| 配置 | 成本/风险 | 结果 |
|---|---|---|
| 止损 0.40%（默认） | 0.18R | 放行，收益 -1.73% 与改动前一致 |
| 止损 0.15% | 0.47R | **拒绝**：`COST_TOO_HIGH` |

这条闸门的意义不在当前参数，而在于**防止未来的参数回归**：任何人把 `min_stop_pct` 调窄，
都会在回测里立刻看到拒绝码，而不是在实盘亏掉 30%。

### 6.2 已完成：真实 CVD + 盘口（原 P0/P1）

| 事项 | 来源 | 落地位置 |
|---|---|---|
| aggTrade → per-bar `FlowBar`（主动买卖量、线内 delta 极值） | R11 | `microstructure/flow.py::FlowAggregator` |
| CVD / CVD 斜率 / 吸收度 | R11 | `microstructure/flow.py::FlowFeatureBuilder` |
| `depth20@100ms` → OBI / 微价格（带宽内、距离加权） | R12 | `microstructure/book.py::BookTracker` |
| 三级降级 + 盘口正交过滤 | R1/R12 | `strategy/trend_pullback.py::_microstructure_ok` |
| 历史订单流下载（逐笔在下载中聚合，不落原始） | R11 | `binance_rest.download_flow` + `scalpx download-flow` |
| 落库与回放 | — | `bar_flow` / `book_snapshot` 表 + `data/flow_source.py` |

**实测（同一份合成数据，30 天 BTCUSDT 1m）**

| `flow_mode` | 交易数 | 期望 R |
|---|---|---|
| `off`（不过滤） | 289 | -0.1384 |
| `taker_proxy`（原代理） | 289 | -0.1414 |
| `agg_trade`（真实 CVD） | 133 | **-0.0791** |

交易数减半、期望 R 改善 0.06R。但要诚实标注：合成数据的 delta 路径本身是噪声，
改善主要来自"少做一半无效交易"（成本减半），**不是**来自 alpha。真实数据上是否有增益
必须用 `download-flow` 拉真实逐笔后重跑——这也是这次改动的真正目的：让这个 A/B 变得可执行。

### 6.3 未落地的项（按投入产出排序）

| 优先级 | 事项 | 来源 | 预计改动 | 预期收益 |
|---|---|---|---|---|
| P0 | 止盈 maker 化（`LIMIT` + GTX 挂 TP，成交后撤止损） | R3 | `oms.py` | 每笔省 2bps ≈ 0.05R |
| P1 | 跑价即撤（激活区间） | R4 | `oms._sweep_entry_timeouts` | 减少无效挂单与追价 |
| P1 | 成交剖面版的堆叠失衡（按价格分箱，需逐笔保留价位） | R11 | `microstructure/flow.py` | 定位成交密集区，与盘口版互补 |
| P2 | 方向隔离熔断 | R6 衍生 | `risk/manager.py` | 单边行情只停一侧 |

### 6.3 明确不抄的三件事

1. **不抄 1 秒轮询控制循环**（Hummingbot `update_interval=1.0`）。
   超短线的进出场窗口就是秒级，轮询会持续吃掉 0~1s 的延迟预算。本项目用事件驱动 + bar 内路径。
2. **不抄"策略自己算仓位"**（Jesse 的 `self.buy = (qty, price)`）。
   仓位必须由独立风控按风险预算算，否则策略作者的一个手误就是爆仓。
3. **不抄"停更项目的时间语义"**（zipline 式日频对齐）。
   超短线的所有超时、冷却、屏障都必须以 **bar 或毫秒** 为单位，分钟级粒度在这里等于没有。

---

## 7. 一页纸总结

**筛选**：6 条硬标准 → 6 个入选 + 2 个范式对照，淘汰纯研究库与停更项目。

**六个项目各自贡献了一条最关键的、可被超短线直接复用的设计**：

- Hummingbot → **净盈亏屏障 + 激活区间**（成本与无效下单）
- freqtrade（protections）→ **滚动冷却 + 只统计止损类离场**（纪律）
- freqtrade（binance.py）→ **场所能力声明 + 标记价触发**（执行健壮性）
- Jesse → **先校验后撤单**（消除裸仓窗口）
- freqtrade（orderflow）→ **成交方向口径的 delta 与线内极值**（微观结构）
- Hummingbot（order book）→ **UID 水位 + 快照重放 + ready 闸门**（盘口可信度）

**整合后的判断**：超短线系统的竞争力不在信号复杂度，而在
**"成本是否被逐笔算清楚、执行是否有确定性、盘口是否可证明一致"** 这三件事上。
本项目已把第 1、2 件事做成代码级硬约束（成本闸门 + 保护单不变量），
第 3 件事（真实盘口）是下一步唯一值得投入的方向。
