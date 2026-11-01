# 运行与运维手册

## 1. 环境

```bash
# 核心功能：零第三方依赖（Python 3.11+）
python -m scalpx strategies          # 自检：能列出策略即环境正常

# 实盘/实时行情：需要 websockets
pip install "scalpx[ws]"             # 或 pip install websockets>=12
```

- 系统时钟必须与 NTP 同步（币安签名对时间敏感，偏差 > 5s 会持续 `-1021`）。
  `BinanceRest.sync_time()` 会在首次签名请求前自动校正本地偏移。
- Windows / Linux / macOS 均可；SQLite 需支持 WAL（Python 内置版本即可）。

---

## 2. 三种模式

| 模式 | 命令 | 行情 | 成交 | API Key | 用途 |
|---|---|---|---|---|---|
| `backtest` | `scalpx backtest` | 本地 CSV/SQLite | 模拟撮合 | 不需要 | 研究、参数敏感性、回归验证 |
| `paper` | `scalpx paper` | 实时 WS（或轮询） | 模拟撮合 | 不需要 | 验证信号在真实行情下的行为、验证执行逻辑 |
| `live` | `scalpx live --i-understand-live-risk` | 实时 WS | **真实下单** | 必需 | 真金白银 |

三者共用同一套 Strategy / Risk / OMS / Portfolio，仅替换 Clock 与 Broker。

---

## 3. 运行流程

### 3.1 准备数据

```bash
# 方式 A：合成数据（无网络，仅验证链路）
python scripts/make_sample_data.py --days 30

# 方式 B：币安公开 K 线（不需要 API Key）
python -m scalpx download --symbol BTCUSDT --interval 1m \
    --start 2024-01-01 --end 2024-03-01 --out data/raw

# 方式 C：按配置批量下载（symbols/interval/start/end 取自配置文件）
python -m scalpx download --config config/backtest.toml
```

CSV 表头：`ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base`
（兼容币安原始 kline CSV 的 12 列格式，见 `data/feed.py::_resolve_columns`）。

**准备订单流数据（CVD 数据源，可选但强烈建议）**

```bash
# 逐笔成交在下载过程中直接聚合成 per-bar 订单流，不落地原始逐笔（磁盘省 3 个数量级）
python -m scalpx download-flow --symbol BTCUSDT --interval 1m \
    --start 2024-01-01 --end 2024-03-01 --persist-db data/scalpx.db
```

产物 `data/raw/BTCUSDT_1m_flow.csv`：`ts,buy_vol,sell_vol,trades,max_delta,min_delta,large_vol`。
回测时 `CsvFlowSource` 自动读取；文件缺失则策略降级到 `taker_buy_base` 代理，
回测结束会打印 `微观结构: 有订单流 N/M bar` 以示区分。

### 3.2 回测

```bash
python -m scalpx backtest --config config/backtest.toml
python -m scalpx backtest --config config/backtest.toml --run-id baseline_01
python -m scalpx backtest --config config/backtest.toml --param rr=1.8 --param vol_mult=1.3
```

产物写入 `runs/<run_id>/`：`metrics.json`、`trades.csv`、`equity.csv`、`report.txt`；
同时写入 `data/scalpx.db` 的 `runs / trades / orders / fills / equity / signals / risk_events` 表。

### 3.3 参数敏感性（不要用它选参数）

```bash
python scripts/grid_search.py --config config/backtest.toml \
    --grid rr=1.2,1.5,1.8 --grid min_stop_pct=0.002,0.004 --grid vol_mult=1.1,1.4
```

输出按 `avg_r`（期望 R）排序。**必须做样本外验证**：把数据切成前后两段，
前段挑参数、后段验证，否则只是在拟合噪声。

### 3.4 纸上交易

```bash
python -m scalpx paper --config config/paper.toml
```

- 使用真实行情 + 模拟撮合，适合连续跑 1~2 周验证：信号频率是否符合预期、
  挂单是否总是超时、日志里 `entries_expired` 是否过高。
- `config/paper.toml` 默认指向 testnet 域名；若只想跑行情，`exchange.feed = "ws"` 即可。
- 退出（Ctrl+C）会走 `_shutdown()`：撤单 → 平仓 → 落库 → 写最终 metrics。

### 3.5 实盘

```bash
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."        # 只开合约交易权限，禁用提币
python -m scalpx live --config config/live.toml --i-understand-live-risk
```

启动流程：`_preflight()`（时间同步 → 查余额 → 设置逐仓/杠杆）→ 订阅行情 →
启动用户数据流 / 对账 / 心跳三个守护任务 → 进入 bar 循环。

---

## 4. 配置项全表

### `[run]`

| 键 | 默认 | 说明 |
|---|---|---|
| `name` | `scalpx` | 运行名，进入 run_id |
| `mode` | `backtest` | `backtest` / `paper` / `live`，与子命令必须一致 |
| `output_dir` | `runs` | 报表根目录 |
| `log_level` | `INFO` | `DEBUG/INFO/WARNING/ERROR` |
| `seed` | `7` | 合成数据/可复现实验用 |

### `[market]`

| 键 | 默认 | 说明 |
|---|---|---|
| `symbols` | `["BTCUSDT"]` | 多标的会走 `MultiSymbolFeed` 时间序合并 |
| `interval` | `1m` | 信号粒度，同时决定 `bar_ms` |
| `market_type` | `futures` | 仅 `futures` 被完整支持 |
| `price_tick` / `qty_step` / `min_qty` / `min_notional` | 由交易所决定 | 取错会导致 `-4014/-1111` 拒单 |

### `[data]`

| 键 | 默认 | 说明 |
|---|---|---|
| `source` | `csv` | `csv` / `sqlite` / `binance_rest` |
| `csv_dir` / `sqlite_path` | `data/raw` / `data/scalpx.db` | 数据位置 |
| `start` / `end` | 2024-01-01 / 2024-02-01 | UTC；回测区间 |
| `warmup_bars` | 300 | 指标预热根数，不足不产生信号 |
| `persist` | `true` | 关闭后不写库（研究模式提速） |

### `[strategy]`

| 键 | 说明 |
|---|---|
| `name` | 注册表里的名字，见 `scalpx strategies` |
| `[strategy.params]` | 策略私有参数，未注册的键会被策略的 `validate_params` 或默认值处理 |

### `[risk]`

| 键 | 默认 | 说明 |
|---|---|---|
| `risk_per_trade` | 0.0025 | 单笔风险占权益比例（**最重要**） |
| `max_positions` | 2 | 并发持仓数上限 |
| `max_leverage` | 3.0 | 名义/权益上限 |
| `max_notional_pct` | 3.0 | 名义价值/权益上限 |
| `max_daily_loss_pct` | 0.025 | 日内回撤熔断 |
| `max_daily_trades` | 40 | 当日最多交易笔数 |
| `consecutive_loss_cooldown` / `cooldown_minutes` | 3 / 30 | 连亏冷却 |
| `consecutive_loss_halt` | 5 | 连亏当日停手 |
| `min_stop_pct` / `max_stop_pct` | 0.004 / 0.008 | 止损距离夹紧区间（决定名义仓位与成本） |
| `max_bars_held` | 40 | 硬性持仓上限（兜底策略） |
| `max_spread_bps` | 4.0 | 点差闸门 |
| `symbol_cooldown_bars` | 3 | 出场后同标的冷却 |
| `correlated_risk_factor` | 1.5 | 同向持仓的风险折算系数 |
| `max_cost_ratio` | 0.35 | 往返成本 / 止损距离 上限；超限拒绝 `COST_TOO_HIGH`。设 0 关闭 |

### `[execution]`

| 键 | 默认 | 说明 |
|---|---|---|
| `entry_order_type` | `limit_post_only` | `market` / `limit_post_only` / `limit_ioc` |
| `entry_timeout_ms` | 3000 | maker 单存活时间（回测按 1 根 bar 粒度） |
| `entry_max_retries` | 2 | 超时后降级重试次数 |
| `entry_offset_bps` | 0.5 | 挂单相对收盘价的偏移（至少让出 1 tick） |
| `maker_fee` / `taker_fee` | 0.0002 / 0.0004 | **必须按真实费率填** |
| `slippage_bps` | 1.0 | 市价单滑点 |
| `stop_place_deadline_ms` | 800 | 成交后挂保护单的时限 |
| `reconcile_interval_s` | 30 | 实盘对账周期 |
| `max_order_reject_streak` | 3 | 连续拒单熔断阈值 |

### `[microstructure]`

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；关闭后策略自动降级到 taker 占比 |
| `use_agg_trades` | `true` | 订阅 `aggTrade` 计算真实 CVD（回测读 `*_flow.csv` / `bar_flow` 表） |
| `use_order_book` | `true` | 订阅 `depth20@100ms` 计算 OBI |
| `depth_bps` | 5.0 | 盘口带宽：只统计 ±5bps 内的挂单 |
| `obi_levels` | 20 | 盘口档数（与订阅的 20 档对齐） |
| `cvd_window` | 20 | CVD 斜率窗口（bar 数），20 ≈ 20 分钟 |
| `max_book_age_ms` | 2000 | 盘口超时阈值，超过即视为不可用（实盘建议 1500） |
| `large_trade_usd` | 50000 | 大单阈值，用于 `large_ratio` |
| `stack_ratio` | 3.0 | 档位失衡阈值（买/卖 ≥ 3 记为主导） |
| `stack_range` | 3 | 连续多少档同向才算"价格墙" |
| `min_level_qty` | 0.0 | 单档最小量，过滤冰山单噪声 |
| `max_bad_ratio` | 0.5 | 滚动窗口内异常比例上限（超过判定盘口不可信） |
| `gate_on_book_quality` | `true` | 盘口不可信时是否熔断入场（`BOOK_UNTRUSTED`） |
| `persist_flow` | `true` | 实盘把每根 bar 的成交流聚合落 `bar_flow` 表 |
| `persist_book` | `false` | 实盘录制盘口到 `book_snapshot`（约 1 行/秒/标的） |
| `persist_book_interval_ms` | 1000 | 盘口录制采样间隔 |

### `[backtest]` / `[exchange]`

| 键 | 默认 | 说明 |
|---|---|---|
| `initial_equity` | 10000 | 初始权益（实盘时作为名义基准） |
| `pessimistic_intrabar` | `true` | 逆势优先路径，**不要关** |
| `assumed_spread_bps` | 1.5 | 历史数据无盘口，用固定点差代理 |
| `exchange.feed` | `ws` | `ws`（需 websockets）/ `rest_poll`（零依赖降级） |
| `exchange.testnet` | `true` | 实盘配置里应为 `false`，并显式写 `base_url` |
| `exchange.base_url` / `ws_url` | fapi / fstream | testnet 时改为 `testnet.binancefuture.com` |
| `exchange.api_key_env` / `api_secret_env` | `BINANCE_API_KEY` / `..._SECRET` | 只从环境变量读取，不落配置文件 |

---

## 5. 可观测性

### 5.1 日志

- 每笔订单、成交、风控拒绝都会打日志；`--log-level DEBUG` 可看到 post-only 降级细节。
- 风控事件同时写入 `risk_events` 表（`level/code/message/meta`），可按 code 做归因统计。

### 5.2 数据库

```sql
-- 当日交易与盈亏
SELECT symbol, side, entry_price, exit_price, pnl, r_multiple, reason
FROM trades WHERE run_id = ? ORDER BY exit_ts DESC LIMIT 20;

-- 风控拦截分布
SELECT code, COUNT(*) FROM risk_events WHERE run_id = ? GROUP BY code ORDER BY 2 DESC;

-- 权益曲线
SELECT ts, equity, drawdown FROM equity WHERE run_id = ? ORDER BY ts;
```

### 5.3 心跳与关键指标

`housekeeping` 每 5 秒写 `runtime_state['heartbeat']` 并打一行日志：

```
心跳 equity=10000.00 pos=0 dd=0.00% trades=12 halted=False
```

必须监控的四项：

| 指标 | 健康值 | 异常含义 |
|---|---|---|
| `oms.forced_flatten` | **0** | > 0 说明出现过无保护持仓，立即停产排查 |
| `oms.protections_failed` | **0** | > 0 说明交易所挂单失败（精度/权限/网络） |
| `BOOK_QUALITY` 事件 | 罕见 | 频繁翻转说明 WS 不稳或带宽不足，此时 OBI 因子不可信 |
| `book.stale_drops / out_of_order` | 增长缓慢 | 快速上升说明盘口在追包，`depth20` 档数可能需要调小 |
| `entries_expired / entries_attempted` | < 50% | 过高说明 maker 挂单价太保守，长期无法成交 |
| `entries_rejected` | ≈ 0 | 持续增长说明精度/保证金/权限有问题 |

### 5.4 报表

```bash
python -m scalpx report --run-id bt-btc-trend-pullback-xxx --db data/scalpx.db
```

---

## 6. 故障处置 SOP

| 现象 | 排查 | 处置 |
|---|---|---|
| 持续 `-1021` | 本机时钟 | `w32tm /resync`（Win）/ `chronyc makestep`（Linux）；`sync_time` 会自动校正 |
| 持续 `-1003` | 请求频率 | 提高 `RateLimiter.refill_per_s` 间隔；减少 `symbols` |
| `-2010/-2019` 保证金不足 | 仓位/杠杆 | 降低 `risk_per_trade`；确认逐仓保证金划转 |
| `-4014/-1111` 精度 | `price_tick`/`qty_step` | 用 `/fapi/v1/exchangeInfo` 校准配置 |
| 持仓无保护单 | `risk_events` 中 `MISSING_STOP` | 系统会自动补挂/强平；事后必须查清为何丢失（网络？手动撤单？） |
| 账实不符 | 对账日志 `RECONCILE_UNKNOWN_POSITION` | 系统已强平 + 熔断；人工核对交易所流水后再 `resume()` |
| 连续亏损熔断 | `CONSECUTIVE_LOSS_HALT` | 当日停手；复盘信号质量，不要当天重启 |
| 需要紧急停机 | — | `touch data/KILL`，5 秒内自动撤单+平仓+退出 |
| 进程崩溃 | 重启后 | 启动时 `_reconcile_once` 会重建状态；持仓/挂单以交易所为准 |

**紧急停机三步**（任何一步都可独立生效）：

```bash
touch data/KILL                       # 1. 软停止（推荐，会自动平仓）
# 2. 若进程已失联：登录交易所手动全平
# 3. 若交易所也失联：确认保护单是否已在交易所侧挂好（这是最后防线）
```

---

## 7. 实盘上线检查清单

上线前逐项打勾，任何一项"不确定"都不要开真仓：

- [ ] 用**真实历史数据**（≥ 6 个月）跑通回测，且做了样本外验证
- [ ] `paper` 模式连续运行 ≥ 7 天，`forced_flatten = 0`
- [ ] `entries_expired / attempted < 50%`（说明 maker 定价合理）
- [ ] testnet 实盘下单验证过：开仓 → 保护单挂出 → 触发止损 → 平仓 → 对账一致
- [ ] API Key 只开「合约交易」，**关闭提币**，并绑定 IP 白名单
- [ ] 账户为**逐仓**模式，`max_leverage` 与交易所设置一致
- [ ] `risk_per_trade ≤ 0.1%`、`max_daily_loss_pct ≤ 1%`（首月）
- [ ] `max_positions = 1`（首月）
- [ ] 本机 NTP 正常，且已配好进程守护（systemd / 计划任务）
- [ ] kill switch 演练过一次：`touch data/KILL` → 确认 5 秒内平仓退出
- [ ] 有一条能触达你的告警通道（`ERROR` 级别日志）

---

## 8. 常见问题

**Q：回测赚、实盘亏，最可能是什么原因？**
按可能性排序：① 手续费/滑点填低了（`maker_fee/taker_fee/slippage_bps`）；
② maker 单实际不成交（`entries_expired` 高），实盘被迫吃单；
③ 数据含未来信息（本项目结构上已排除）；④ 样本内过拟合。

**Q：为什么 `entries_expired` 这么高？**
post-only 单挂在同侧最优价之外，价格不回头就不成交。这是**用成交率换手续费**的必然结果。
想提高成交率：减小 `price_tick` 偏移（至少 1 tick）、缩短 `entry_timeout_ms`、
或接受 `entry_order_type = "market"` 的成本。

**Q：能不能加止损追踪到 1 个 tick？**
可以，但每次 `adjust_stop` 都是「撤单 + 新挂」，在 1m 频率下会显著增加请求量与
触发 `-1003` 的风险。建议只在 R ≥ 1.0 / 1.5 两个节点调整。

**Q：能否同时跑多个策略？**
当前是单进程单策略。多策略建议各自独立进程 + 独立 DB（或不同 `run_id`），
由外部撮合/资金分配层控制，避免共享 Portfolio 状态。
