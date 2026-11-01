# 可扩展后台管理 + AI 辅助

> 目标：后台不止能管"写死的几张表"，而是**由 schema 声明驱动**——
> 新增一类数据只加一段声明，界面、校验、API、AI 能力自动具备。
> 同时把 GitHub 主流开源量化系统的核心可配置项，收敛成四类后台实体。

---

## 1. 开源量化系统的数据模型视角

`docs/01` 与 `docs/05` 已从架构与代码层面评审过 freqtrade / Hummingbot / NautilusTrader / Jesse / Qlib / vn.py。
本节换一个视角：**它们各自把什么东西做成了"可配置数据"**——这决定了后台该管什么。

| 系统 | 核心数据模型 | 可配置/可管理对象 |
|---|---|---|
| **freqtrade** | `Trade` / `Order` / `PairLock` + TOML 配置 + `IStrategy` 类属性 | 交易所密钥、交易对白名单、策略参数、`protections` 保护规则、资金管理、干跑/实盘开关 |
| **Hummingbot** | `Connector` 元数据、`TrackedOrder`、`Executor` 配置（triple barrier） | 连接器与密钥、交易对、执行器参数（止盈/止损/时间限制/移动止损）、`activation_bounds` |
| **NautilusTrader** | Instrument（含 `price_precision`/`size_increment`/`multiplier`）、Account、Order | 标的定义、账户与 venue、订单类型与有效期、风控引擎配置 |
| **Jesse** | `Candle`/`Order`/`Position`、策略类钩子、`hyperparameters()` | 交易所与 API、时间周期、策略超参、手续费与滑点模型 |
| **Qlib** | `Instrument`（市场/板块）、Expression 因子、Dataset、Model | 数据handler、因子表达式、模型与回测配置、股票池 |
| **vn.py** | `ContractData`（含 `pricetick`/`size`）、`AccountData`、`OrderData` | 合约乘数与最小变动、账户与网关、风控前置、CTA 参数 |

**归纳出的共性**：所有系统都必须维护四类"主数据"——**场所/账户、标的、策略实例、风控与数据管道**。
交易所差异（精度的、费率的、下单类型的）最终都沉淀为**标的与账户上的属性**；
策略差异沉淀为**实例化的参数组**；风险偏好沉淀为**可复用的档案**。

这就是本系统后台的四类实体（`scalpx/web/schema.py`）：

| 后台实体 | 对应开源系统能力 | 关键字段 |
|---|---|---|
| `broker_accounts` 交易账户 | freqtrade 交易所配置 / Hummingbot connector / Nautilus Account | venue、account_type、api_key_env（只存变量名）、leverage、position_mode、maker/taker_fee_bps |
| `strategy_instances` 策略实例 | freqtrade IStrategy + 参数 / Jesse hyperparameters | strategy、symbols、timeframe、params(JSON)、risk_profile |
| `risk_profiles` 风控档案 | freqtrade protections / 本项目 `[risk]` 段 | risk_per_trade、max_positions、日亏熔断、连亏停手、min/max_stop_pct、max_cost_ratio |
| `data_sources` 数据源 | Qlib DataHandler / freqtrade dataprovider | kind(kline/aggtrade/orderbook/fundamental)、venue、symbol、interval、retention_days |
| `instruments` 标的库 | Nautilus Instrument / vn.py ContractData / Qlib Instrument | 见 [06 §标的库](06-web-app.md)，含 6 大资产类型与按市场校验 |

> 取舍：**不复制大而全的配置树**。开源系统的 TOML/YAML 往往几百行，
> 后台只暴露"会被人改、且改错会出事"的字段，其余留在配置文件里由版本管理。

---

## 2. 可扩展数据模型

### 2.1 为什么不用万能 KV 表

`entity / attr / value` 三张表看似能装下一切，代价是：

| 问题 | 后果 |
|---|---|
| 无法建唯一索引 | 重名记录拦不住，只能靠应用层遍历 |
| 无类型约束 | 数字存成字符串，比较排序全错 |
| 无法 WHERE/ORDER BY | 列表页只能全量拉回内存过滤 |
| 迁移无从校验 | 改结构时旧数据静默失效 |

因此采用**「一实体一真表 + 一份 schema 声明」**：存储是强类型的，
而增删改查、校验、表单渲染、AI 提示词全部由 schema 派生。

### 2.2 三个核心对象

```python
FieldSpec(name, label, type, required, default, min, max, max_length,
          pattern, options, help, widget, readonly, placeholder)

EntitySpec(key, label, table, description, fields, unique,
           search_fields, order_by, reorderable, hook)

EntityStore(web)   # list / get / create / update / delete / validate / reorder
```

- `type` 支持 `string / text / json / int / float / bool / enum`
- `options` 可以是静态列表，也可以是**可调用对象**——策略名就从注册表实时求值，
  不需要在 schema 里硬编码
- `unique` 是**复合唯一键元组列表**，写入前显式查重（不依赖数据库报错文案）
- `hook(payload, partial) -> (normalized, issues)` 用于领域不变量：
  标的库的"代码必须符合该市场规则"就是这样挂上去的

### 2.3 新增一类数据要改什么

只有三步，且都在一个文件里：

```python
# 1) 建表（repo.py 的 WEB_SCHEMA 追加一段 CREATE TABLE）
# 2) 声明实体（schema.py 追加一个 EntitySpec 并注册到 ENTITIES）
# 3) 完成 —— 前端自动出现该类型；如需领域校验，补一个 hook
```

不需要动 API（路由是 `/{entity}` 泛化的）、不需要写模板（表单由字段渲染）、
不需要改 AI（提示词按 schema 生成）。

### 2.4 安全边界

- 表名/列名只来自 `ENTITIES`，**绝不拼接用户输入**；值全部参数化
- 未知字段直接判错（而非静默忽略），避免"以为改了其实没生效"
- 布尔以 `0/1` 存、JSON 以 `TEXT` 存，`_decode` 时还原
- 所有写操作记审计（`{entity}_create/update/delete`）

---

## 3. 字段范围

### `broker_accounts` 交易账户

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| name | string | 必填，唯一，≤32 | 账户标识 |
| venue | enum | BINANCE/OKX/BYBIT/COINBASE/CME/ICE/IBKR | 交易场所 |
| account_type | enum | futures/spot/margin | 账户类型 |
| api_key_env | string | 必填，`^[A-Z][A-Z0-9_]{2,40}$` | **只存环境变量名**，密钥不落库 |
| testnet | bool | — | 测试网开关 |
| leverage | float | 1–20 | 杠杆 |
| position_mode | enum | oneway/hedge | 双向需交易所同步开启 |
| maker_fee_bps / taker_fee_bps | float | 0–100 | 费率，直接影响回测真实度 |
| enabled / note | bool / text | — | 启用、备注 |

### `strategy_instances` 策略实例

| 字段 | 类型 | 约束 |
|---|---|---|
| name | string | 必填，唯一，≤32 |
| strategy | enum | 来自策略注册表（运行时求值） |
| symbols | string(csv) | 必填，逗号分隔 |
| timeframe | enum | 1m/3m/5m/15m/30m/1h/4h/1d |
| params | json | 覆盖策略默认参数，≤2KB |
| risk_profile | string | 引用风控档案名，留空用全局 |
| enabled / note | bool / text | — |

### `risk_profiles` 风控档案

| 字段 | 类型 | 范围 | 对应引擎参数 |
|---|---|---|---|
| risk_per_trade | float | 0.0001–0.02 | `risk.risk_per_trade` |
| max_positions | int | 1–20 | `risk.max_positions` |
| max_leverage | float | 1–20 | `risk.max_leverage` |
| max_daily_loss_pct | float | 0.001–0.5 | `risk.max_daily_loss_pct` |
| max_daily_trades | int | 1–1000 | `risk.max_daily_trades` |
| consecutive_loss_halt | int | 1–50 | 连亏停手 |
| cooldown_minutes | int | 0–1440 | 连亏冷却 |
| min_stop_pct / max_stop_pct | float | 0.0005–0.2 / 0.001–0.5 | 止损夹紧区间 |
| max_cost_ratio | float | 0–1 | 成本/风险闸门 |

### `data_sources` 数据源

| 字段 | 类型 | 约束 |
|---|---|---|
| name | string | 必填，唯一 |
| kind | enum | kline/aggtrade/orderbook/fundamental |
| venue | enum | 同账户 |
| symbol | string | `^[A-Z0-9.\-]{2,24}$` |
| interval | enum | 同策略 |
| path | string | 本地路径，≤200，留空用默认 |
| retention_days | int | 0–3650，0=永久 |
| enabled / note | bool / text | — |

---

## 4. 交互流程

### 4.1 新增 / 编辑

```
用户打开表单
   └─ 前端按 FieldSpec 渲染控件（enum→下拉、bool→是/否、json→文本域…）
输入过程中（350ms 防抖）
   └─ POST /api/admin/entities/{e}/validate  →  字段级 issues + 重复检测
点击保存
   ├─ 再校验一次（服务端权威）
   ├─ 通过  → POST/PATCH  → 200 {item}  → toast「已保存」+ AI 解读
   └─ 失败  → 400 {code, message, details[]} → 字段下方红字 + AI 建议
```

**双重校验**是有意的：前端校验保证体验（即时反馈），服务端校验保证正确性（不可绕过）。

### 4.2 AI 参与的三处介入点

| 介入点 | 触发 | 行为 |
|---|---|---|
| 表单填充 | 弹窗内「AI 填充」 | 输入一句自然语言 → 回填空字段，列出推断依据 |
| 校验纠错 | 弹窗内「AI 纠错」 | 按 issue 修复类型/范围/未知字段，逐条说明改了什么 |
| 结果解读 | 保存后 / 失败后 | 一句话结论 + 下一步建议 |

### 4.3 自然语言增改

```
输入「新增数据源 name=demo kind=kline symbol=BTCUSDT」
   → POST /api/admin/ai/command
   → 返回 plan（entity/action/data）+ preview_issues（预演校验）
   → 前端展示计划卡片（动作、字段、风险提示、置信度）
   → 用户点「确认执行」
   → POST /api/admin/ai/apply  → 仍然走 schema 校验 → 落库 → 审计
```

**AI 永远不直接写库**：命令接口只产出计划，执行是独立接口且复用同一套校验。
这样即使模型幻觉出一个不存在的字段或越界数值，也会在执行前被 schema 拦下。

---

## 5. 接口规范

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/entities` | 实体清单 + 字段元数据 + AI 模式 |
| GET | `/api/admin/entities/{entity}` | 列表（`q` 搜索） |
| POST | `/api/admin/entities/{entity}` | 新增 |
| PATCH | `/api/admin/entities/{entity}/{id}` | 修改（合并后整体重校验） |
| DELETE | `/api/admin/entities/{entity}/{id}` | 删除 |
| POST | `/api/admin/entities/{entity}/validate` | 只校验不写库 |
| POST | `/api/admin/ai/suggest` | `{entity, partial, hint}` → `{values, notes, mode}` |
| POST | `/api/admin/ai/fix` | `{entity, payload}` → `{payload, changes[], unresolved[]}` |
| POST | `/api/admin/ai/command` | `{text}` → `{plan, preview_issues, requires_confirmation}` |
| POST | `/api/admin/ai/apply` | `{plan}` → `{result, explanation}` |
| POST | `/api/admin/ai/explain` | `{entity, action, ok, message, issues}` → `{summary, suggestions[]}` |

```json
// GET /api/admin/entities   （节选）
{"ok":true,"data":{
  "ai":{"mode":"local","model":"","base_url":""},
  "items":[{"key":"risk_profiles","label":"风控档案","icon":"⛨",
    "unique":[["name"]],"search_fields":["name"],
    "fields":[{"name":"risk_per_trade","label":"单笔风险","type":"float",
               "required":true,"min":0.0001,"max":0.02,"default":0.0025,
               "options":[],"help":"占权益比例，0.0025 = 0.25%"}]}]}}

// POST /api/admin/ai/command
{"ok":true,"data":{
  "mode":"local","confidence":0.5,"requires_confirmation":true,
  "plan":{"entity":"data_sources","action":"create","id":null,
          "data":{"name":"demo","kind":"kline","symbol":"BTCUSDT"},
          "summary":"create 数据源：name=demo，kind=kline，symbol=BTCUSDT",
          "warnings":[]},
  "preview_issues":[]}}
```

错误码沿用统一信封：`VALIDATION_FAILED(400)` / `DUPLICATE(409)` / `NOT_FOUND(404)`，
`details` 里是**字段级数组**，前端直接对应到输入框。

---

## 6. AI 设计

### 6.1 两种模式

| 模式 | 触发条件 | 实现 |
|---|---|---|
| `llm` | 配置了 `SCALPX_AI_KEY` | OpenAI 兼容 `/chat/completions`，强制 `response_format=json_object` |
| `local` | 未配置 / 请求失败 / 输出不可解析 | **确定性规则**：类型纠正、范围夹紧、关键词+键值解析、错误码模板 |

界面用徽标明示当前模式（`AI: 大模型` / `AI: 本地规则`），
**绝不假装调用过模型**——这一点和项目"不伪造数据"的原则一致。

环境变量：

```bash
SCALPX_AI_KEY=sk-...            # 存在即启用
SCALPX_AI_BASE_URL=https://api.openai.com/v1
SCALPX_AI_MODEL=gpt-4o-mini
SCALPX_AI_TIMEOUT=20
SCALPX_AI=off                   # 强制本地模式
```

### 6.2 本地模式真的有用

不是占位实现：

- **填充**：解析 `key=value`（支持中文标签与标签后缀，`名称` → `数据源名称`）+ 枚举默认值
- **纠错**：`"99999"` → 夹紧到 `3650`、`"5"` → `5.0`、未知字段移除、首尾空白清理，逐条给出 `why`
- **指令**：动作词（新增/修改/删除/查询）+ 实体标签/英文 key 匹配 + 键值抽取 + `id=` 识别
- **解读**：按错误码与 issue 文案生成可执行建议（"名称类字段要求全局唯一，换一个名字或编辑已有记录"）

### 6.3 约束与安全

| 风险 | 处理 |
|---|---|
| 模型幻觉字段 | 输出先与 `FieldSpec` 求交集，未知字段直接丢弃并写进 `notes` |
| 模型编造数值 | 执行前仍走 `EntitySpec.normalize`，越界即拒 |
| 模型误删数据 | `command` 只出计划；`apply` 需显式确认；删除同样要求 `id` |
| 提示词过大 | 只发精简 schema（字段名/类型/必填/范围/枚举），不发帮助文本 |
| 密钥泄漏 | 只在服务端环境变量读取，不出现在任何响应里（`describe()` 不返回 key） |
| 模型不可用 | 捕获所有异常 → 回退本地 → 在结果里注明"模型不可用（原因），已使用本地规则" |

---

## 7. 验证

| 项 | 方式 | 结果 |
|---|---|---|
| schema 校验 | `tests/test_generic_admin.py` 13 例：必填/范围/枚举/类型/JSON/未知字段 | 通过 |
| 存储层 | 同文件 8 例：默认值、唯一冲突、合并更新、删除、搜索、JSON 往返 | 通过 |
| AI 本地能力 | 同文件 7 例：填充/纠错/指令/计划校验/降级 | 通过 |
| 接口契约 | `tests/test_web.py::TestGenericAdmin` 12 例（含权限 403） | 通过 |
| 全量回归 | `unittest discover` | **173 通过** |
| 真机端到端 | 启动服务 → 建风控档案 → AI 指令 → 确认执行 → 数据落库 → 清理 | 通过 |

真机实测输出：

```
catalog: 4 entities, ai=local
create risk_profile: id=1 rpt=0.001 cost=0.35
command: entity=data_sources action=create data={"name":"btc-kline","kind":"kline","symbol":"BTCUSDT","interval":"1m"} mode=local
apply: name=btc-kline | 创建成功。
fix: retention_days=3650 changes=1
```

> 备注：首次用 PowerShell 的 `Invoke-RestMethod -Body <string>` 测试时中文指令未被正确编码
> （PS 5.1 默认按 ASCII 发送），导致实体识别为空。改用
> `[System.Text.Encoding]::UTF8.GetBytes()` 后正常——这是测试脚本的问题，不是服务端的问题。

---

## 8. 已知限制

| 限制 | 影响 | 方向 |
|---|---|---|
| 新增实体需改代码（建表 + 声明） | 不能"运行时建表" | 有意为之：运行时 DDL 无法做类型约束与迁移审计 |
| 不支持一对一/一对多关联 | `risk_profile` 只能按名称引用 | 后续可加 `ref` 字段类型 + 下拉 |
| AI 不参与"批量导入纠错" | 标的库批量导入只做确定性校验 | 可复用 `fix` 接口逐行处理 |
| 无字段级权限 | 所有 admin 可改所有字段 | 可给 `FieldSpec` 加 `roles` |
| 无变更历史（只有审计摘要） | 无法回滚单条记录 | 可加 `entity_history` 表存前后快照 |
