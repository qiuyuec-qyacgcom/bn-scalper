<div align="center">

# scalpx

**币安 USDⓈ-M 永续 · 1 分钟级超短线交易系统**

事件驱动 · 回测与实盘同源 · 核心零第三方依赖 · 自带 Web 控制台

![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Dependencies](https://img.shields.io/badge/core%20dependencies-0-brightgreen.svg)
![Tests](https://img.shields.io/badge/tests-103%20passed-brightgreen.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

[快速开始](#快速开始) · [功能特性](#功能特性) · [架构](#架构) · [使用说明](#使用说明) · [目录结构](#目录结构) · [贡献指南](#贡献指南)

</div>

---

## 项目简介

`scalpx` 是一个面向 **Binance USDⓈ-M 永续合约**的超短线（scalping）交易系统，信号粒度 1 分钟，
目标持仓 5~30 分钟。它不是一个"策略合集"，而是一套**可交付的工程底座**：从行情接入、信号生成、
风控闸门、订单执行，到回测、实盘、Web 控制台与一键部署，完整闭环。

项目的出发点是一个判断：**决定交易系统能不能用的，不是策略多花哨，而是三件事**——

1. **回测和实盘是不是同一套决策代码**（否则回测只是自我安慰）
2. **任何时刻的持仓是不是都有确定性的保护单**（否则一次断网就归零）
3. **异常是不是有明确归属**（拒单、超时、部分成交、对账不一致，各有确定分支）

这三条被做成硬约束，并用测试固化：`tests/test_system.py::TestArchitecture` 用 AST 静态检查
**依赖方向**，策略层一旦 import 执行层就立刻失败。解耦不靠约定，靠 CI。

> ⚠️ 本项目用于学习与研究。默认参数基于合成数据标定，**不可直接用于实盘**，
> 详见 [风险声明](#风险声明)。

---

## 功能特性

### 交易引擎

| 能力 | 实现 |
|---|---|
| **回测 / 实盘同源** | 共享 `Strategy` / `RiskManager` / `OrderManager` / `Portfolio`，只替换 Clock 与 Broker |
| **成交即挂保护单** | 撮合器同步回调 → OMS 在**同一根 bar 内**挂出 `STOP_MARKET` + `TAKE_PROFIT_MARKET` |
| **fail-closed** | 保护单挂不上 → 立即市价平仓；持仓无止损 → 强制平仓并计入 `forced_flatten` |
| **唯一否决点** | `RiskManager.pre_trade` 是唯一能拒绝入场的地方，10 个可枚举拒绝码，全部落库可归因 |
| **成本闸门** | `往返成本 / 止损距离 ≤ 0.35R`，防止"止损过窄 → 名义仓位爆炸 → 手续费吃掉半个 R" |
| **幂等下单** | `newClientOrderId` 由 OMS 生成，重试复用；网络超时先查单再决定 |
| **maker 优先 / taker 兜底** | post-only 挂单 → 超时撤单 → 降级市价（最多 2 次） |
| **增量指标** | `IndicatorSet` 用 `deque` 做 O(1) 更新，回测实盘同一份实现，无 talib/pandas |

### 微观结构

| 能力 | 实现 |
|---|---|
| **真实 CVD** | `aggTrade` → per-bar 主动买卖量 + **bar 内累计 delta 极值** → 斜率与"吸收度" |
| **盘口 OBI** | `depth20@100ms` → 带宽内距离加权失衡 + 微价格偏离 |
| **堆叠失衡** | 连续 N 档同向主导 = 价格墙（等距档位配对，非价格对角线，见 [设计说明](docs/03-strategy-and-risk.md)） |
| **三级降级** | 真实 CVD → `taker_buy` 代理 → 放行，绝不伪造 0 值 |
| **质量熔断** | 盘口丢包/乱序/过期超阈值 → `BOOK_UNTRUSTED` 拒绝入场 |

### 工程与交付

| 能力 | 实现 |
|---|---|
| **零核心依赖** | 仅标准库：SQLite 存储、手写 REST 客户端、自研指标、`http.server` Web 服务 |
| **Web 控制台** | 前台看板（权益/交易/信号/CVD）+ 后台管理（用户权限/标的库/配置热改/数据下载/紧急停机/审计） |
| **标的库** | 股票 / 基金 / 债券 / 期货 / 外汇 / 加密全类型登记：按市场校验代码、批量导入自动去重、拖拽排序、即改即用 |
| **可扩展后台** | schema 驱动的通用增改：交易账户 / 策略实例 / 风控档案 / 数据源；字段级校验、复合唯一键、审计留痕，新增一类数据只加一段声明 |
| **AI 辅助** | 智能填充、自动纠错、自然语言增改（先出计划再确认执行）、结果解读；无模型密钥时自动降级为确定性规则模式 |
| **一键部署** | `deploy/install.sh` 八段式：环境检测 → 依赖安装 → 目录用户 → 备份同步 → 配置账号 → 服务注册 → 启动 → 健康校验；支持 `--dry-run` / `--check` / `--upgrade` / `--rollback` / `--purge`，失败自动回滚 |
| **可观测** | 结构化日志 + SQLite 台账（orders/fills/trades/equity/signals/risk_events/bar_flow） |
| **可测试** | 103 个用例：指标口径、撮合语义、保护单不变量、微观结构、权限与接口契约 |

---

## 架构

```
CLI ──► engine/backtest.py   engine/live.py ──► metrics / reporting
             │                     │
             └──── 编排（只负责"怎么跑"）────┘
                          │
   ┌──────────┬───────────┼───────────┬────────────┐
strategy/   risk/     execution/  portfolio.py  data/
出意图      可否决/定仓位  下单/撤单/保护单  只记账   行情/存储/REST
   └──────────┴───────────┼───────────┴────────────┘
        indicators.py · microstructure/ · models.py
                          │
                     web/ (控制台) ── api → repo/auth → scalpx.core
```

**依赖严格单向**：`models.py` 零业务依赖；`strategy/` 不得 import `execution/`、`risk/`、`data/`、`engine/`。
违规会被 `TestArchitecture` 拦住。

详见 [架构说明](docs/02-architecture.md)。

---

## 快速开始

### 环境要求

- Python **3.11+**（仅标准库，无需 pip install）
- 实盘/实时行情需 `websockets`（可选：`pip install "scalpx[ws]"`）
- 系统时钟需 NTP 同步（币安签名对时间敏感）

### 5 步跑通回测

```bash
git clone https://github.com/qiuyuec-qyacgcom/bn-scalper.git && cd bn-scalper

# 1. 生成联调数据（无网络也可用）
python scripts/make_sample_data.py --days 30

# 2. 跑一次回测
python -m scalpx backtest --config config/backtest.toml

# 3. 查看报表（终端 + runs/<run_id>/ 下 4 个文件）
python -m scalpx report --run-id <run_id>

# 4. 自检
python -m unittest discover -s tests -t . -v

# 5. 启动 Web 控制台
python -m scalpx.web create-user --username admin --role admin
python -m scalpx.web serve --port 8787
```

打开 `http://127.0.0.1:8787`（前台）与 `http://127.0.0.1:8787/admin`（后台）。

---

## 使用说明

### CLI 命令

| 命令 | 作用 |
|---|---|
| `scalpx backtest` | 运行历史回测，支持 `--param rr=1.8` 覆盖策略参数 |
| `scalpx download` | 下载币安历史 K 线到 CSV / SQLite |
| `scalpx download-flow` | 下载逐笔成交并**在下载中聚合**为 per-bar 订单流（不落地原始逐笔） |
| `scalpx paper` | 纸上交易：真实行情 + 模拟撮合 |
| `scalpx live` | 实盘（需凭证 + 显式 `--i-understand-live-risk`） |
| `scalpx report` | 从 SQLite 回读某次运行的绩效 |
| `scalpx strategies` | 列出已注册策略 |

### 三套配置

| 配置 | 用途 | 成交 | 需要 Key |
|---|---|---|---|
| `config/backtest.toml` | 历史回测 | 模拟撮合 | 否 |
| `config/paper.toml` | 纸上交易 | 模拟撮合 | 否 |
| `config/live.toml` | 实盘 | **真实下单** | 是 |

### 交易规格（摘要）

- **标的**：`BTCUSDT` 永续；**周期**：1m 已收盘 bar；**持仓**：5~30 分钟
- **入场（7 条件全满足）**：趋势向上 → 波动率在区间内 → 放量 → 回踩 EMA 带 → 收复且收阳 →
  RSI 上穿 50 → 微观结构确认（delta / CVD 斜率 / 吸收度 / 盘口失衡）
- **出场**：`2×ATR` 止损（夹到 0.4%~0.8%）· `1.5R` 止盈 · 1R 保本 · 1.5R 移动止损 ·
  30 根时间止损 · 结构反转
- **仓位**：`qty = equity × 0.25% / 止损距离`，再受名义价值与 3x 杠杆约束

完整公式与参数表见 [策略与风控](docs/03-strategy-and-risk.md)。

### 一键部署（Linux 服务器）

```bash
bash deploy/install.sh -y --port 8787
```

安装完成会输出访问地址与**初始管理员凭据**（同时落盘 `data/web/initial_credentials.txt`，权限 600）。

支持 Debian/Ubuntu（apt）、RHEL/CentOS/Rocky（dnf/yum）、Alpine（apk）、Arch（pacman）、
openSUSE（zypper）、macOS（brew）；无 systemd/OpenRC 时自动退化为 `nohup`。

```bash
bash deploy/install.sh --check         # 只自检，不改系统
bash deploy/install.sh --dry-run       # 预演，打印将执行的命令
bash deploy/install.sh --upgrade -y    # 升级（保留 data/ 与 config/）
bash deploy/install.sh --rollback      # 回滚到最近一次备份
bash deploy/uninstall.sh               # 卸载（默认保留数据，--purge 才删）
bash deploy/healthcheck.sh             # 外部健康探针（cron/监控用）

# 完整参数、退出码、幂等性与回滚说明见 deploy/README.md
```

### 回测结果（合成数据，诚实结论）

BTCUSDT 1m × 30 天，1 万 U 初始权益，maker 2bps / taker 4bps：

| 指标 | 数值 |
|---|---|
| 交易笔数 / 胜率 | 133 / 45.1% |
| 期望 R | **-0.079R** |
| 总收益 / 最大回撤 | -2.59% / 4.27% |
| 手续费 | 473.84 U |
| 保护单挂出 / 强制平仓 | 266 / **0** |
| 订单流覆盖 | 42,901 / 43,200 bar（99.3%） |

**这不是一个盈利的策略，而是一个诚实的引擎**：合成数据本身没有 edge，回测给出的期望 R
恰好等于交易成本的量级——说明引擎没有系统性造假。真实研究请用 `download` 拉真实 K 线后重跑，
并做样本外验证。成本分析见 [docs/03 §7](docs/03-strategy-and-risk.md)。

---

## 目录结构

```
scalpx/
├── scalpx/                     # 主包（零第三方依赖）
│   ├── models.py               # 纯数据结构：Bar/Tick/Order/Fill/Position/Flow/Book
│   ├── config.py               # TOML 加载 + 启动期强校验
│   ├── indicators.py           # 增量指标（EMA/ATR/RSI/SMA），回测实盘共用
│   ├── portfolio.py            # 仓位、费用、已实现/未实现盈亏
│   ├── metrics.py              # 夏普/索提诺/最大回撤/期望 R
│   ├── reporting.py            # 报表落盘（metrics.json / trades.csv / equity.csv）
│   ├── cli.py                  # 命令行入口
│   ├── microstructure/         # 成交流（CVD/吸收度）、盘口（OBI/堆叠失衡）、质量熔断
│   ├── data/                   # 行情回放/订阅、SQLite 台账、币安 REST、订单流数据源
│   ├── strategy/               # 策略基类 + 注册表 + trend_pullback + donchian
│   ├── risk/                   # 风险预算定仓、成本模型、三层风控闸门
│   ├── execution/              # Broker 抽象、模拟撮合、OMS、币安实盘 Broker
│   ├── engine/                 # 回测引擎 / 实盘引擎
│   └── web/                    # Web 控制台（server/api/repo/auth/tomlio + static）
├── config/                     # backtest.toml / paper.toml / live.toml
├── deploy/                     # install.sh / uninstall.sh
├── docs/                       # 01 调研 · 02 架构 · 03 策略风控 · 04 运维 · 05 代码评审 · 06 Web
├── scripts/                    # make_sample_data.py / grid_search.py
├── tests/                      # core / execution / system / microstructure / web
├── pyproject.toml
└── README.md
```

**运行时产物**（已在 `.gitignore` 中排除）：`data/raw/*.csv`、`data/*.db`、`runs/`、`logs/`。

---

## 文档

| 文档 | 内容 |
|---|---|
| [01 开源系统调研](docs/01-open-source-research.md) | freqtrade / NautilusTrader / Hummingbot / Qlib 架构拆解、8 条共性、本项目的取舍 |
| [02 架构说明](docs/02-architecture.md) | 分层图、模块职责、回测-实盘一致性机制、订单状态机、8 条 ADR |
| [03 策略与风控](docs/03-strategy-and-risk.md) | 入场/出场公式、仓位公式、风控三层、异常处理矩阵、成本分析 |
| [04 运维手册](docs/04-operations.md) | 配置项全表、运行流程、监控指标、故障 SOP、实盘检查清单 |
| [05 代码级评审](docs/05-code-level-review.md) | 逐文件评审 6 个开源项目，提取 12 条可复用模式并映射到超短线要求 |
| [06 Web 控制台](docs/06-web-app.md) | 前后台模块划分、接口规范与错误码、数据模型、部署六阶段、安全设计 |
| [07 后台管理与 AI](docs/07-admin-and-ai.md) | 开源系统数据模型对照、schema 驱动的可扩展实体、字段范围、AI 四种能力与降级策略 |

---

## 贡献指南

欢迎 Issue 与 PR。为了让改动可被快速评审，请遵循以下约定。

### 开发流程

```bash
# 1. fork 并克隆
git clone https://github.com/<you>/scalpx.git && cd scalpx

# 2. 建分支（feat/ fix/ docs/ refactor/）
git checkout -b feat/my-feature

# 3. 开发 + 自测（必须全绿）
python -m unittest discover -s tests -t . -v

# 4. 提交（Conventional Commits）
git commit -m "feat(strategy): add vwap reversion strategy"

# 5. 推送并开 PR
git push origin feat/my-feature
```

### 代码规范

| 项 | 要求 |
|---|---|
| 依赖 | **核心不得引入第三方依赖**；可选能力放到 `pyproject.toml` 的 `[project.optional-dependencies]` |
| 分层 | 遵守单向依赖：`strategy` 不碰 `execution/risk/data/engine`；`models` 零业务依赖 |
| 类型 | 全部函数带类型注解，`from __future__ import annotations` |
| 注释 | 解释**为什么**，不解释"这行在做什么"；复杂决策写进 docstring |
| 参数 | 不写魔法数字，一律进 `config/*.toml` 并在对应 dataclass 里给默认值 + 校验 |
| 异常 | 新增拒绝/失败路径必须有**可枚举的错误码**，并落 `risk_events` 或返回错误信封 |

### 新增策略（不改引擎）

```python
# scalpx/strategy/my_strategy.py
from ..models import EnterIntent, Side
from .base import Strategy, StrategyContext
from .registry import register

@register("my_strategy")
class MyStrategy(Strategy):
    @classmethod
    def default_params(cls) -> dict:
        return {"ema": 20, "rr": 1.5}

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

在 `scalpx/strategy/__init__.py` 注册后，把 `config/*.toml` 里 `[strategy] name` 改成 `my_strategy` 即可。
**策略只产出意图，不决定仓位**——仓位由 `risk/sizing.py` 计算。

### PR 检查清单

- [ ] `python -m unittest discover -s tests -t .` 全绿
- [ ] 新增行为有对应测试（尤其是风控/执行的不变量）
- [ ] 没有引入第三方依赖，或已在 `pyproject.toml` 声明为可选
- [ ] 新增配置项同步更新 `config/*.toml` 与 `docs/04-operations.md`
- [ ] 若改变交易逻辑，同步更新 `docs/03-strategy-and-risk.md`
- [ ] commit message 符合 Conventional Commits（`feat/fix/docs/refactor/test/chore`）

### 报告问题

请附：Python 版本、操作系统、复现命令、完整报错栈、`runs/<run_id>/metrics.json`（如涉及回测）。

---

## 路线图

- [x] 事件驱动引擎 + 回测/实盘同源
- [x] 三层风控 + 成本闸门 + 保护单不变量
- [x] 真实 CVD + 盘口 OBI + 堆叠失衡 + 质量熔断
- [x] Web 控制台 + 一键部署
- [ ] 止盈 maker 化（`LIMIT` + GTX），每笔省 2bps
- [ ] 挂单跑价即撤（激活区间）
- [ ] walk-forward 框架（当前仅参数敏感性分析）
- [ ] 成交剖面版堆叠失衡（按价格分箱）

---

## 风险声明

本项目用于学习与研究。加密货币永续合约带杠杆，可能在极短时间内损失全部本金。

**默认配置不可直接用于实盘**：参数基于合成数据标定，必须先完成真实数据的样本外验证、
`paper` 模式连续运行 ≥ 7 天、testnet 全链路演练（开仓 → 保护单挂出 → 触发止损 → 对账一致）。
实盘命令需显式追加 `--i-understand-live-risk` 才会启动，且首月建议 `risk_per_trade ≤ 0.1%`、
`max_positions = 1`。上线前请逐项核对 [实盘检查清单](docs/04-operations.md)。

---

## 许可证

[MIT](LICENSE)
