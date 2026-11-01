<div align="center">

# scalpx

**币安 USDⓈ-M 永续 · 1 分钟级超短线交易系统**

事件驱动 · 回测与实盘同源 · 真实微观结构 · 零第三方依赖 · 自带 Web 控制台

![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Dependencies](https://img.shields.io/badge/core%20dependencies-0-brightgreen.svg)
![Tests](https://img.shields.io/badge/tests-198%20passed-brightgreen.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

[项目简介](#项目简介) · [核心功能](#核心功能) · [快速开始](#快速开始) · [配置](#配置) · [使用示例](#使用示例) · [API](#api-概览) · [FAQ](#常见问题) · [贡献](#贡献指南)

</div>

---

## 项目简介

`scalpx` 是一套面向 **Binance USDⓈ-M 永续合约**的超短线交易系统：信号粒度 1 分钟，目标持仓 5~30 分钟。
它不是一个"策略合集"，而是一套**可交付的工程底座**——行情接入、信号生成、风控闸门、订单执行、
回测、实盘、Web 控制台与一键部署，完整闭环。

### 它解决什么问题

决定一套交易系统能不能用的，往往不是策略多花哨，而是三件事：

| 痛点 | 常见后果 | 本项目的做法 |
|---|---|---|
| 回测和实盘是两套代码 | 回测漂亮、实盘崩盘 | 共享 `Strategy`/`Risk`/`OMS`/`Portfolio`，只换 Clock 与 Broker |
| 持仓在某个瞬间没有保护单 | 一根插针爆掉账户 | 成交**同步回调** OMS，在同一根 bar 内挂出止损/止盈；挂不上立即强平 |
| 异常没有归属 | 出问题只能猜 | 10+ 个可枚举拒绝码；每个决策都写 `signals.meta`，可回测归因 |

### 适用场景

- 想研究 **1m~5m 级超短线**策略，需要一套能跑真实撮合语义的回测框架
- 需要一个**能上服务器**的量化控制台（看板 + 用户权限 + 数据管理 + 风控开关）
- 想把开源交易系统的设计（freqtrade / Hummingbot / Nautilus / Qlib）落到自己的代码里

### 不适合什么

- 高频做市（本项目是 bar 级事件驱动，不做微秒级撮合）
- 多交易所套利（当前单所单策略，见 [docs/01](docs/01-open-source-research.md) 的取舍说明）
- 直接拿去实盘赚钱：默认参数基于合成数据标定，**必须先做样本外验证**

---

## 核心功能

### 交易引擎

| 能力 | 实现 |
|---|---|
| **回测 / 实盘同源** | 同一套策略、风控、订单管理；`Broker` 接口切换模拟撮合与真实下单 |
| **成交即挂保护单** | 撮合器同步回调 → OMS 在同一根 bar 内挂 `STOP_MARKET` + `TAKE_PROFIT_MARKET` |
| **fail-closed** | 保护单挂不上 → 立即市价平仓；持仓无止损 → 强制平仓并计数 `forced_flatten` |
| **唯一否决点** | `RiskManager.pre_trade` 是唯一能拒绝入场的地方，拒绝码全部落库 |
| **成本闸门** | `往返成本 / 止损距离 ≤ 0.35R`，防止"止损过窄 → 名义仓位爆炸 → 手续费吃掉半个 R" |
| **幂等下单** | `newClientOrderId` 由 OMS 生成，超时先查单再重试 |
| **maker 优先 / taker 兜底** | post-only 挂单 → 超时撤单 → 降级市价（限次） |
| **增量指标** | `deque` 实现的 O(1) 指标更新，回测实盘同一份代码，无 talib/pandas |

### 微观结构

| 能力 | 实现 |
|---|---|
| **真实 CVD** | `aggTrade` → per-bar 主动买卖量 + **bar 内累计 delta 极值** → 斜率与"吸收度" |
| **盘口 OBI** | `depth20@100ms` → 带宽内（±5bps）距离加权失衡 + 微价格偏离 |
| **堆叠失衡** | 等距档位配对（非价格对角线），连续 N 档同向 = 价格墙 |
| **三级降级** | 真实 CVD → `taker_buy` 代理 → 放行，绝不伪造 0 值 |
| **质量熔断** | 丢包/乱序/过期超阈值 → `BOOK_UNTRUSTED` 拒绝入场 |

### 工程与交付

| 能力 | 实现 |
|---|---|
| **零核心依赖** | 仅标准库：SQLite、手写 REST 客户端、自研指标、`http.server` |
| **Web 控制台** | 前台看板（权益/交易/信号/CVD）+ 后台（用户权限/标的库/主数据/AI/停机/审计） |
| **可扩展管理** | schema 驱动的通用增改，新增一类数据只加一段声明 |
| **AI 辅助** | 智能填充、自动纠错、自然语言增改、结果解读；无密钥自动降级为规则模式 |
| **一键部署** | 八段式安装器：环境检测→依赖→目录→备份同步→配置→服务→启动→健康校验，失败自动回滚 |

---

## 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 类型注解、`tomllib`、`slots` dataclass |
| 存储 | SQLite（WAL） | 单机零运维，台账可 SQL 查询 |
| HTTP | `http.server.ThreadingHTTPServer` | 单机私有部署足够，免框架依赖与升级风险 |
| 前端 | 原生 HTML/CSS/JS + Canvas | 无构建、无 npm、无 CDN，离线可用 |
| 配置 | TOML | 可读、可版本化、标准库可解析 |
| 实时行情 | WebSocket（可选 `websockets`） | 仅实盘订阅时需要 |
| 部署 | Bash（systemd / OpenRC / nohup） | 覆盖主流 Linux 发行版 |

---

## 快速开始

### 环境要求

- Python **3.11+**（核心零依赖，无需 `pip install`）
- 实时行情需要 `pip install "scalpx[ws]"`（仅实盘/纸上交易）
- 系统时钟需 NTP 同步（币安签名对时间敏感）

### 1. 获取代码并跑通回测

```bash
git clone https://github.com/qiuyuec-qyacgcom/bn-scalper.git && cd bn-scalper

python scripts/make_sample_data.py --days 30      # 生成合成数据（含订单流）
python -m scalpx backtest --config config/backtest.toml
python -m scalpx report --run-id <run_id>         # 从 SQLite 回读绩效
```

### 2. 启动 Web 控制台

```bash
python -m scalpx.web create-user --username admin --role admin
python -m scalpx.web serve --port 8787
```

打开 `http://127.0.0.1:8787`（前台）与 `http://127.0.0.1:8787/admin`（后台）。

### 3. 一条命令部署到服务器

```bash
bash deploy/install.sh            # 交互式
bash deploy/install.sh -y         # 静默
```

安装完成会输出访问地址与**初始管理员凭据**（同时落盘 `data/web/initial_credentials.txt`，权限 600）。

```bash
bash deploy/install.sh --check    # 只自检，不改系统
bash deploy/install.sh --dry-run  # 预演，打印将执行的命令
bash deploy/install.sh --upgrade  # 升级（保留 data/ 与 config/）
bash deploy/install.sh --rollback # 回滚到最近一次备份
bash deploy/uninstall.sh          # 卸载（默认保留数据，--purge 才删）
```

支持 Debian/Ubuntu、RHEL/CentOS/Rocky、Alpine、Arch、openSUSE、macOS；
无 systemd/OpenRC 时自动退化为 `nohup`。完整参数与退出码见 [deploy/README.md](deploy/README.md)。

---

## 配置

三套配置分别对应三种运行模式：

| 配置文件 | 用途 | 成交方式 | 需要 API Key |
|---|---|---|---|
| `config/backtest.toml` | 历史回测 | 模拟撮合 | 否 |
| `config/paper.toml` | 纸上交易（实时行情 + 模拟成交） | 模拟撮合 | 否 |
| `config/live.toml` | 实盘 | **真实下单** | 是 |

### 关键配置项

```toml
[market]
symbols = ["BTCUSDT"]
interval = "1m"

[risk]
risk_per_trade   = 0.0025   # 单笔风险 0.25%
max_positions    = 2
min_stop_pct     = 0.0040   # 止损下限，配合成本闸门
max_cost_ratio   = 0.35     # 往返成本 / 止损距离 上限

[microstructure]
use_agg_trades = true       # aggTrade → 真实 CVD
use_order_book = true       # depth20@100ms → OBI
stack_range    = 3          # 连续 3 档同向 = 价格墙
max_bad_ratio  = 0.5        # 盘口异常比例上限

[strategy.params]
flow_mode       = "auto"    # auto | agg_trade | taker_proxy | off
min_delta_ratio = 0.04
min_stack_score = -1.0      # -1 关闭；0.0 要求无逆向价格墙
flow_exit_delta = 0.0       # >0 时订单流反转即离场
```

完整参数表见 [docs/04-operations.md](docs/04-operations.md)。

### 环境变量

| 变量 | 作用 |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | 实盘凭证（只在实盘模式读取） |
| `SCALPX_AI_KEY` | 大模型密钥，配置后启用 AI（不配则用本地规则） |
| `SCALPX_AI_BASE_URL` / `SCALPX_AI_MODEL` | 兼容 OpenAI 协议的地址与模型名 |
| `SCALPX_AI_RETRIES` / `SCALPX_AI_CACHE_TTL` | 重试次数与缓存时长 |
| `SCALPX_AI_FALLBACK` | `local`（降级规则）或 `error`（严格模式） |
| `SCALPX_AI=off` | 强制本地模式 |

后台「主数据管理 → AI 助手 → 模型与运行配置」可在线调整超时、重试、并发、缓存，
**密钥永远只从环境读取，不落库、不进接口响应**。

---

## 使用示例

### 命令行

```bash
# 回测（可覆盖策略参数做敏感性分析）
python -m scalpx backtest --config config/backtest.toml --param rr=1.6
python -m scalpx backtest --config config/backtest.toml --param flow_mode=taker_proxy

# 下载历史 K 线
python -m scalpx download --symbol BTCUSDT --interval 1m \
    --start 2024-01-01 --end 2024-03-01

# 下载逐笔并聚合为 per-bar 订单流（不落地原始逐笔，磁盘省 3 个数量级）
python -m scalpx download-flow --symbol BTCUSDT --interval 1m \
    --start 2024-01-01 --end 2024-03-01 --persist-db data/scalpx.db

# 纸上交易 / 实盘
python -m scalpx paper --config config/paper.toml
python -m scalpx live  --config config/live.toml --i-understand-live-risk
```

### 作为库使用

```python
from scalpx.config import load_config
from scalpx.data.feed import build_feed
from scalpx.data.store import Store
from scalpx.engine.backtest import BacktestEngine
from scalpx.strategy.registry import create_strategy

cfg = load_config("config/backtest.toml")
strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
store = Store(cfg.data.sqlite_path, run_id="demo")

engine = BacktestEngine(cfg, strategy, build_feed(cfg), store=store, run_id="demo")
result = engine.run()
print(result.metrics["total_return"], result.oms_stats["forced_flatten"])
```

### 新增策略（不改引擎）

```python
from scalpx.models import EnterIntent, Side
from scalpx.strategy.base import Strategy, StrategyContext
from scalpx.strategy.registry import register

@register("my_strategy")
class MyStrategy(Strategy):
    @classmethod
    def default_params(cls) -> dict:
        return {"rr": 1.5}

    def on_bar(self, ctx: StrategyContext) -> list:
        if ctx.position is not None:
            return []
        if ctx.bar.close > ctx.snapshot.ema_fast:
            return [EnterIntent(
                symbol=ctx.symbol, side=Side.BUY, ts=ctx.bar.ts, ref_price=ctx.bar.close,
                stop_price=ctx.bar.close * 0.996, take_profit_price=ctx.bar.close * 1.006,
                reason="demo",
            )]
        return []
```

策略**只产出意图，不决定仓位**——仓位由 `risk/sizing.py` 按风险预算计算。

### AI 自然语言操作（后台）

```
输入：新增数据源 name=demo kind=kline symbol=BTCUSDT interval=1m
→ 解析为操作计划（实体 / 动作 / 字段 / 风险提示 / 置信度）
→ 预览校验 → 点击「确认执行」→ 仍然经过 schema 校验后落库
```

---

## 目录结构

```
scalpx/
├── scalpx/                     # 主包（零第三方依赖）
│   ├── models.py               # 纯数据结构：Bar/Order/Fill/Position/Flow/Book
│   ├── config.py               # TOML 加载 + 启动期强校验
│   ├── indicators.py           # 增量指标（EMA/ATR/RSI/SMA），回测实盘共用
│   ├── portfolio.py            # 仓位、费用、已实现/未实现盈亏
│   ├── metrics.py              # 夏普/索提诺/最大回撤/期望 R
│   ├── reporting.py            # 报表落盘（metrics.json / trades.csv / equity.csv）
│   ├── cli.py                  # 命令行入口
│   ├── microstructure/         # 成交流(CVD/吸收度) · 盘口(OBI/微价格/堆叠) · 质量熔断
│   ├── data/                   # 行情回放/订阅、SQLite 台账、REST、订单流数据源
│   ├── strategy/               # 策略基类 + 注册表 + trend_pullback + donchian
│   ├── risk/                   # 成本模型、风险预算定仓、三层风控闸门
│   ├── execution/              # Broker 抽象、模拟撮合、OMS、币安实盘 Broker
│   ├── engine/                 # 回测引擎 / 实盘引擎
│   └── web/                    # 控制台：server/api/repo/auth/schema/generic/ai/instruments + static
├── config/                     # backtest.toml / paper.toml / live.toml
├── deploy/                     # install.sh / uninstall.sh / healthcheck.sh / README.md
├── docs/                       # 01 调研 · 02 架构 · 03 策略风控 · 04 运维 · 05 代码评审 · 06 Web · 07 后台与 AI
├── scripts/                    # make_sample_data.py / grid_search.py / publish.sh
├── tests/                      # 198 个用例
├── pyproject.toml
├── LICENSE
└── README.md
```

运行时产物（已 gitignore）：`data/raw/*.csv`、`data/*.db`、`runs/`、`logs/`、`data/web/`。

---

## API 概览

服务端统一返回信封：

```json
{"ok": true,  "data": {...}}
{"ok": false, "error": {"code": "DUPLICATE", "message": "标的已存在", "details": [...]}}
```

### 前台（需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（安装器与探针使用），返回 `checks{web_db,static,config,trading_db}` |
| POST | `/api/auth/login` · `/api/auth/logout` | 登录 / 登出 |
| GET | `/api/dashboard?run_id=` | KPI + 权益曲线 + 出场分布 + 风控事件 |
| GET | `/api/runs` · `/api/trades` · `/api/signals` · `/api/micro` | 运行 / 交易 / 信号 / 微观结构 |
| GET | `/api/instruments` | 标的库（供筛选与补全） |
| POST | `/api/backtest` | 提交回测（可带 `symbols[]`、`params{}`） |

### 后台（需 admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/admin/users` · PATCH/DELETE `/{id}` | 用户与权限 |
| GET/POST | `/api/admin/instruments` · PATCH/DELETE `/{id}` | 标的库增改 |
| POST | `/api/admin/instruments/batch` | 批量导入，返回逐行结果与 `retry_rows` |
| PUT | `/api/admin/instruments/order` | 拖拽排序 |
| GET/POST/PATCH/DELETE | `/api/admin/entities/{entity}` | 通用实体（交易账户/策略实例/风控档案/数据源） |
| GET/PUT | `/api/admin/ai/config` | AI 运行时配置（密钥不可写） |
| POST | `/api/admin/ai/suggest` · `fix` · `command` · `apply` · `explain` | AI 四项能力 |
| POST | `/api/admin/ai/cache/clear` | 清空结果缓存 |
| POST | `/api/admin/risk` | 紧急停机 / 恢复 |
| DELETE | `/api/admin/runs/{id}` | 删除运行及其明细 |

完整接口规范、错误码表与数据模型见 [docs/06-web-app.md](docs/06-web-app.md) 与 [docs/07-admin-and-ai.md](docs/07-admin-and-ai.md)。

---

## 回测结果（合成数据，诚实结论）

BTCUSDT 1m × 30 天，1 万 U 初始权益，maker 2bps / taker 4bps：

| 指标 | 数值 |
|---|---|
| 交易笔数 / 胜率 | 133 / 45.1% |
| 期望 R | **-0.079R** |
| 总收益 / 最大回撤 | -2.59% / 4.27% |
| 保护单挂出 / 强制平仓 | 266 / **0** |
| 订单流覆盖 | 42,901 / 43,200 bar（99.3%） |

**这不是一个盈利的策略，而是一个诚实的引擎**：合成数据没有 alpha，回测给出的期望 R
恰好等于交易成本的量级——说明引擎没有系统性造假。真实研究请用 `download` 拉真实 K 线后重跑。

成本视角（为什么止损不能太窄）：

| `min_stop_pct` | 名义仓位（1 万本金） | 往返成本 | 占单笔风险 |
|---|---|---|---|
| 0.15% | 16,667 U | 10.0 U | **0.40R** |
| 0.40% | 6,250 U | 3.8 U | 0.15R |

---

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

当前 **198 个用例**，覆盖：指标口径、撮合语义、保护单不变量、成本闸门、微观结构
（CVD/OBI/堆叠/质量熔断）、schema 校验、权限与接口契约、安装脚本幂等性。

其中 `tests/test_system.py::TestArchitecture` 用 AST 静态检查**依赖方向**——
策略层一旦 import 执行层立刻失败。解耦不靠约定，靠测试。

---

## 常见问题

<details>
<summary><b>没有网络 / 没有 API Key，能跑吗？</b></summary>

能。核心与回测完全离线：`scripts/make_sample_data.py` 生成合成数据即可跑通全链路。
实盘/纸上交易才需要网络与凭证。
</details>

<details>
<summary><b>为什么不引入 pandas / numpy / talib？</b></summary>

指标是 O(1) 增量的（`deque` + 滚动和），回测 4 万根 bar 只要几秒；引入重型依赖反而
增加部署摩擦（版本冲突、编译、PEP 668 限制）。需要向量化研究时再单独装，不影响核心。
</details>

<details>
<summary><b>回测和实盘结果对不上？</b></summary>

先按顺序排查：① 是否有未来函数（用 `--param` 复现同一时段）；② 撮合假设
（`pessimistic_intrabar` 是否开启）；③ 手续费与滑点配置是否与账户一致；
④ 微观数据覆盖度（回测结束会打印"有订单流 N/M bar"）。若仍有差异，属正常——
NautilusTrader 也单独文档化了这点，见 [docs/05](docs/05-code-level-review.md)。
</details>

<details>
<summary><b>AI 功能必须配密钥吗？</b></summary>

不必须。未配置时自动使用**确定性规则引擎**（类型纠正、范围夹紧、关键词解析、错误码模板），
界面徽标会显示"本地规则"，绝不假装调用过模型。
</details>

<details>
<summary><b>模型调用老是失败/超时？</b></summary>

后台「主数据管理 → 模型与运行配置」可调超时、重试次数、并发上限与缓存 TTL；
`SCALPX_AI_FALLBACK=error` 可切换为"宁可报错也不降级"的严格模式。
缓存命中率与平均延迟在同一面板可见。
</details>

<details>
<summary><b>安装脚本失败后留下了半套环境？</b></summary>

不会。每个改动系统的动作都登记在回滚栈里，失败会**反向撤销**并以退出码 7 结束；
连续执行会打印失败行号与日志尾部。也可手动 `--rollback` 从最近备份恢复。
</details>

<details>
<summary><b>能同时回测 A 股和 BTC 吗？</b></summary>

不能在一次运行里混跑：引擎只支持一套 `price_tick`/`qty_step`，精度不一致会被
`TICK_MISMATCH` 拒绝并提示分批。
</details>

---

## 贡献指南

欢迎 Issue 与 PR。

### 流程

```bash
git clone https://github.com/<you>/bn-scalper.git && cd bn-scalper
git checkout -b feat/my-feature
python -m unittest discover -s tests -t .      # 必须全绿
git commit -m "feat(strategy): add vwap reversion"
git push origin feat/my-feature
```

### 代码规范

| 项 | 要求 |
|---|---|
| 依赖 | 核心不得引入第三方库；可选能力放 `pyproject.toml` 的 `optional-dependencies` |
| 分层 | `strategy` 不碰 `execution/risk/data/engine`；`models` 零业务依赖 |
| 类型 | 全部函数带类型注解，`from __future__ import annotations` |
| 注释 | 解释**为什么**，不解释"这行在做什么" |
| 参数 | 不写魔法数字，一律进 TOML 并给默认值 + 校验 |
| 异常 | 新增失败路径必须有可枚举错误码，并落 `risk_events` 或错误信封 |

### PR 检查清单

- [ ] `python -m unittest discover -s tests -t .` 全绿
- [ ] 新增行为有对应测试（尤其是风控/执行的不变量）
- [ ] 未引入第三方依赖，或已在 `pyproject.toml` 声明为可选
- [ ] 新增配置项同步更新 `config/*.toml` 与 `docs/04-operations.md`
- [ ] 改变交易逻辑时同步更新 `docs/03-strategy-and-risk.md`
- [ ] commit message 符合 Conventional Commits

### 报告问题

请附：Python 版本、操作系统、复现命令、完整报错栈、`runs/<run_id>/metrics.json`（如涉及回测）。

---

## 文档

| 文档 | 内容 |
|---|---|
| [01 开源系统调研](docs/01-open-source-research.md) | freqtrade / NautilusTrader / Hummingbot / Qlib 架构拆解与取舍 |
| [02 架构说明](docs/02-architecture.md) | 分层图、模块职责、回测-实盘一致性、订单状态机、ADR |
| [03 策略与风控](docs/03-strategy-and-risk.md) | 入场/出场公式、仓位公式、风控三层、成本分析 |
| [04 运维手册](docs/04-operations.md) | 配置项全表、监控指标、故障 SOP、实盘检查清单 |
| [05 代码级评审](docs/05-code-level-review.md) | 逐文件评审 6 个开源项目，12 条可复用模式 |
| [06 Web 控制台](docs/06-web-app.md) | 模块划分、接口规范与错误码、部署、安全设计 |
| [07 后台与 AI](docs/07-admin-and-ai.md) | schema 驱动管理、AI 四项能力与降级策略 |
| [deploy/README](deploy/README.md) | 安装器参数、退出码、幂等矩阵、回滚 |

---

## 路线图

- [x] 事件驱动引擎 + 回测/实盘同源
- [x] 三层风控 + 成本闸门 + 保护单不变量
- [x] 真实 CVD + 盘口 OBI + 堆叠失衡 + 质量熔断
- [x] Web 控制台 + 一键部署 + schema 驱动后台 + AI 辅助
- [ ] 止盈 maker 化（每笔省 2bps）
- [ ] walk-forward 框架（当前只有参数敏感性）
- [ ] 成交剖面版堆叠（按价格分箱，需保留逐笔价位）

---

## 风险声明

本项目用于学习与研究。加密货币永续合约带杠杆，可能在极短时间内损失全部本金。

**默认配置不可直接用于实盘**：参数基于合成数据标定，必须先完成真实数据的样本外验证、
`paper` 模式连续运行 ≥ 7 天、testnet 全链路演练（开仓 → 保护单挂出 → 触发止损 → 对账一致）。
实盘命令需显式追加 `--i-understand-live-risk` 才会启动，首月建议 `risk_per_trade ≤ 0.1%`、
`max_positions = 1`。上线前请逐项核对 [docs/04 的实盘检查清单](docs/04-operations.md)。

---

## 许可证

[MIT](LICENSE) © 2026 scalpx contributors
