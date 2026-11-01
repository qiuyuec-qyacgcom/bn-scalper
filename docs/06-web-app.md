# Web 控制台：前后台架构、接口规范与一键部署

> 目标：把已有的量化引擎（`scalpx`）暴露成一个**可交付的 Web 产品**——
> 前台看数据、跑回测；后台管用户、改配置、控风险、查审计。
> 全栈零第三方依赖（Python 标准库 + 原生 JS），一条命令完成部署。

---

## 1. 角色与职责

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| **游客** | 访问登录页、`/api/health` | 任何业务数据 |
| **user（普通用户）** | 看自己的运行/交易/信号/微观结构；提交回测（默认 10 次/日） | 看他人运行；改配置；管用户；控风控 |
| **admin（管理员）** | 全部；另含用户管理、配置在线编辑、数据下载、紧急停机、审计、删除运行 | — |

权限判定全部在服务端（`api.py::ROUTES` 的 `roles` 字段）。前端隐藏入口只是体验优化。

---

## 2. 模块划分

### 2.1 后端

```
scalpx/web/
├── server.py     HTTP 传输层：路由分发、静态资源、Bearer 解析、后台任务、进程生命周期
├── api.py        接口层：参数校验、权限判定、响应封装、业务编排
├── repo.py       数据访问层：web.db（可写）+ 交易库（只读）
├── auth.py       认证：PBKDF2 口令、HMAC 令牌、登录限流、Principal
├── tomlio.py     最小 TOML 写入器（标准库只有 tomllib 读）
└── static/       纯静态单页应用（无构建）
```

依赖方向严格单向：`server → api → repo/auth → (scalpx.core)`。
传输层不碰业务规则，接口层不拼 SQL，数据层不做权限判断。

### 2.2 前台（`static/index.html` + `app.js`）

| 模块 | 内容 | 交互 |
|---|---|---|
| 登录 | 账号/口令表单 | 失败提示、锁定倒计时 |
| 总览 | 4 个 KPI、权益曲线（Canvas）、出场分布、风控事件、最近交易 | 悬停十字线、运行切换、15s 自动刷新 |
| 交易明细 | 分页表格 | 标的筛选、每页条数 |
| 信号流 | 入场/拒绝/出场记录 | 展示微观数据来源（`agg_trade` / `taker_proxy`） |
| 微观结构 | 覆盖度 KPI + CVD 曲线 | 按标的查询 |
| 运行记录 | 运行列表 | 一键「查看」跳转总览 |

### 2.3 后台（`static/admin.html` + `admin.js`）

| 模块 | 内容 | 关键约束 |
|---|---|---|
| 概览 | 用户数/运行数/库大小/运行时长、最近任务、表规模 | — |
| 用户与权限 | 列表、新建、改角色、启用/禁用、重置口令、删除 | 不能删除自己；至少保留一个启用管理员；禁用/改密立即失效旧会话 |
| 标的库 | 跨资产类型主数据：新增/编辑/删除、上移下移排序、批量导入、实时校验 | 判重键 `类型+市场+代码`；批量导入逐行回报结果；停用标的不参与回测 |
| 系统配置 | 按 TOML 分组的表单，布尔/数字/数组自动识别类型 | 保存前用 `load_config()` 校验；成功自动备份；失败不落盘 |
| 数据管理 | 下载 K 线 / 订单流，查看任务队列，删除运行 | 下载走后台任务，不阻塞请求 |
| 风控开关 | 停机/恢复，写 `data/KILL` | 实盘引擎 5 秒内响应 |
| 审计日志 | 全部敏感操作 | 登录、用户、配置、风控、下载、删除 |
| 系统信息 | 运行时/存储/微观结构 | — |

---

## 3. 接口规范

### 3.1 通用约定

- 路径前缀 `/api`，除 `/api/health` 与 `/api/auth/login` 外均需 `Authorization: Bearer <token>`。
- 请求体 `Content-Type: application/json`，上限 1 MiB。
- 时间戳统一为 **毫秒**（`*_ts`），日期参数为 `YYYY-MM-DD`。
- 金额/价格保留浮点，**比例类字段用小数**（`0.025` 表示 2.5%）。

**成功信封**

```json
{ "ok": true, "data": { }, "meta": { } }
```

**失败信封**

```json
{ "ok": false, "error": { "code": "FORBIDDEN", "message": "无权访问该资源" } }
```

### 3.2 状态码与错误码

| HTTP | 语义 | 常见 `code` |
|---|---|---|
| 200 | 成功 | — |
| 400 | 参数/校验失败 | `BAD_REQUEST` `BAD_JSON` `BAD_QUERY` `CONFIG_INVALID` |
| 401 | 未认证/令牌失效 | `UNAUTHORIZED` `BAD_CREDENTIALS` |
| 403 | 已认证但无权限 | `FORBIDDEN` |
| 404 | 资源不存在 | `NOT_FOUND` `NO_DATA` |
| 409 | 状态冲突 | `CONFLICT`（重名、最后一个管理员） |
| 413 | 请求体过大 | `PAYLOAD_TOO_LARGE` |
| 429 | 限流/配额 | `LOCKED`（登录锁定）`QUOTA_EXCEEDED` |
| 500 | 服务端异常 | `INTERNAL_ERROR` `IO_ERROR` |

### 3.3 接口清单

#### 公共

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查，安装脚本与探针使用 |

```json
// GET /api/health
{"ok":true,"data":{"status":"ok","service":"scalpx-web","version":"1.0.0",
 "core":"1.0.0","uptime_s":128,"trading_db":true}}
```

#### 认证

| 方法 | 路径 | 权限 | 请求 | 响应 |
|---|---|---|---|---|
| POST | `/api/auth/login` | 公开 | `{username, password}` | `{token, expires_in, user:{id,username,role}}` |
| POST | `/api/auth/logout` | 任意 | — | `{logged_out:true}` |
| GET | `/api/auth/me` | 任意 | — | `{id,username,role,last_login_ts,capabilities[]}` |

令牌为无状态 HMAC：`base64url(payload).hmac_sha256`，载荷含 `uid/u/r/v/exp`。
`v` 与数据库 `token_version` 比对——改密或禁用后，旧令牌立即失效。

#### 前台数据

| 方法 | 路径 | 权限 | 主要参数 |
|---|---|---|---|
| GET | `/api/dashboard` | 任意 | `run_id?`（缺省取可见范围内最新） |
| GET | `/api/runs` | 任意 | `limit`(≤100) `offset` |
| GET | `/api/runs/{run_id}` | 任意（受归属约束） | — |
| GET | `/api/trades` | 任意 | `run_id?` `limit`(≤500) `offset` `symbol?` |
| GET | `/api/signals` | 任意 | `run_id?` `limit`(≤200) |
| GET | `/api/micro` | 任意 | `symbol?` `limit`(≤1000) |
| POST | `/api/backtest` | 任意（user 受配额） | `{profile?, params?}` → `{job_id, used_today, quota}` |

```json
// GET /api/dashboard?run_id=bt-xxx   （节选）
{"ok":true,"data":{
  "run":{"run_id":"bt-xxx","mode":"backtest","strategy":"trend_pullback",
         "symbols":["BTCUSDT"],"interval":"1m","status":"finished"},
  "kpi":{"total_return":-0.0173,"max_drawdown":0.032,"win_rate":0.4404,
         "avg_r":-0.0253,"trades":277,"profit_factor":0.9155,
         "sharpe":-1.4553,"fee_drag_pct":0.0992,"final_equity":9826.89},
  "equity":[{"ts":1704067200000,"equity":10000.0,"drawdown":0.0}],
  "recent_trades":[{"symbol":"BTCUSDT","side":"long","qty":0.0123,
                    "entry_price":42000.5,"exit_price":42180.0,
                    "entry_ts":1704067260000,"exit_ts":1704068160000,
                    "pnl":2.21,"r_multiple":0.82,"reason":"take_profit"}],
  "exit_breakdown":{"time_stop":175,"signal_flip":65,"take_profit":23,"stop_loss":14},
  "risk_events":[{"ts":1704067200000,"level":"WARNING","code":"COOLDOWN","message":"连亏 3 笔"}]
}}
```

#### 后台

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/users` | `{items[], roles[]}` |
| POST | `/api/admin/users` | `{username, password, role}` |
| PATCH | `/api/admin/users/{uid}` | `{role?, status?, password?}` |
| DELETE | `/api/admin/users/{uid}` | 删除 |
| GET | `/api/admin/config?profile=` | `{profile, profiles[], data{}}` |
| PUT | `/api/admin/config` | `{profile, data{}}` → `{saved, backup}` |
| GET | `/api/admin/system` | 服务/存储/微观结构/任务 |
| GET | `/api/admin/audit` | `limit` `offset` |
| GET | `/api/admin/jobs` | 后台任务列表（前端轮询用） |
| POST | `/api/admin/data/download` | `{kind, symbol, interval, start, end}` |
| POST | `/api/admin/risk` | `{action: halt\|resume, reason?}` |
| DELETE | `/api/admin/runs/{run_id}` | 删除运行及其明细 |

#### 标的库

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/instruments` | 任意 | 轻量列表（前台筛选/补全用），支持 `type` `q` `active_only` |
| GET | `/api/admin/instruments` | admin | 完整列表（含时间戳、扩展属性） |
| POST | `/api/admin/instruments` | admin | 新增；重复返回 `409 DUPLICATE` |
| POST | `/api/admin/instruments/validate` | admin | 实时校验，返回 `{valid, issues[], duplicate, existing}` |
| POST | `/api/admin/instruments/batch` | admin | 批量导入，`{rows:[...]}` 或 `{text:"..."}` |
| PATCH | `/api/admin/instruments/{id}` | admin | 编辑（整体合并后重新校验） |
| DELETE | `/api/admin/instruments/{id}` | admin | 删除 |
| PUT | `/api/admin/instruments/order` | admin | `{ids:[...]}` 按序重排 |

```json
// POST /api/admin/instruments/batch   （text 形式）
{"ok":true,"data":{
  "total":4,"created":2,"duplicated":1,"invalid":1,
  "items":[
    {"line":1,"code":"600519","name":"贵州茅台","status":"created","id":12},
    {"line":2,"code":"RB2410","name":"螺纹钢","status":"created","id":13},
    {"line":3,"code":"BTCUSDT","status":"duplicate","existing_id":1,"message":"已存在于标的库"},
    {"line":4,"code":"BAD","status":"invalid","issues":[{"field":"code","message":"代码格式不符：6 位数字，如 600519"}]}
  ]}}

// POST /api/admin/instruments  重复时
{"ok":false,"error":{"code":"DUPLICATE","message":"标的已存在：BTCUSDT 比特币（crypto/BINANCE）",
 "details":{"existing":{"id":1,"code":"BTCUSDT"}}}}
```

**资产类型与代码规则**（`scalpx/web/instruments.py::ASSET_TYPES`，新增市场只需加一行）

| 类型 | 市场 | 代码规则 | 示例 |
|---|---|---|---|
| `crypto` | BINANCE / OKX / BYBIT / COINBASE | `^[A-Z0-9]{5,24}$` | `BTCUSDT` |
| `stock` | SH / SZ / BJ | `^\d{6}$` | `600519` |
| | HK | `^\d{5}$` | `00700` |
| | US / JP | `^[A-Z]{1,5}(\.[A-Z]{1,2})?$` / `^\d{4}$` | `AAPL` `BRK.A` |
| `fund` | CN / HK / US | 6 位数字 / 5 位数字 / 字母 | `510300` |
| `bond` | CN / US | 6 位数字 / `^[A-Z0-9]{5,12}$` | `019547` |
| `futures` | SHFE / DCE / CZCE / CFFEX / INE | `^[A-Z]{1,2}\d{3,4}$` | `RB2410` `IF2409` |
| | CME / ICE | `^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$` | `ESZ4` |
| `forex` | FX | `^[A-Z]{6}$` | `EURUSD`（`EUR/USD` 自动归一） |

**规范化与判重**

- 输入先规范化：去空格/`/`/`-`/`_`、转大写、保留数字前导零 → `btc/usdt` 与 `BTCUSDT` 视为同一标的。
- 判重键 `asset_type:market:code`：`000001` 在 `SZ` 与 `SH` 是两个合法标的，同市场下才冲突。
- 批量导入内建两级去重：**批内重复**与**库内已存在**都计入 `duplicated` 并跳过，不报错中断。

**被后续功能引用**

- 前台「交易明细」的标的输入框接入 `datalist` 补全；
- 「运行新回测」可多选标的，`POST /api/backtest` 携带 `symbols`；
- 回测前做两道校验：标的必须**已注册且启用**（否则 `404 INSTRUMENT_NOT_FOUND`），
  且本地必须有对应 `{symbol}_{interval}.csv`（否则 `400 DATA_MISSING`）；
- 所选标的的 `price_tick` / `qty_step` 必须一致（引擎一次运行只支持一套精度，否则 `400 TICK_MISMATCH`），
  一致时用标的库的值覆盖配置，避免按错误精度取整。

```json
// POST /api/admin/config   非法配置示例
{"ok":false,"error":{"code":"CONFIG_INVALID",
 "message":"配置校验失败：risk.risk_per_trade 必须在 (0, 0.02] 内"}}

// POST /api/admin/risk
{"ok":true,"data":{"action":"halt","kill_switch":true,"reason":"盘口异常"}}
```

### 3.4 错误处理约定（前端）

统一封装在 `app.js::api()`：

- `401` → 清 token、回登录页、提示「会话已过期」；
- 其他非 2xx → 抛带 `code` 的 `Error`，由调用方 `toast` 展示 `message`；
- 所有按钮在请求期间置 `disabled`，防止重复提交。

---

## 4. 数据模型

### 4.1 `web.db`（应用状态，可写）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | `id, username, password_hash, salt, role, status, token_version, created_ts, last_login_ts` | 口令 PBKDF2-SHA256 / 26 万轮 / 16 字节盐 |
| `audit` | `ts, uid, username, action, target, detail, ip` | 敏感操作流水 |
| `settings` | `key, value(JSON), updated_ts` | 如 `user_backtest_daily` |
| `run_owner` | `run_id, owner_id` | 运行归属（交易库里没有归属字段，故单独建表） |
| `quota` | `uid, day, used` | 每日回测配额 |
| `instruments` | `symbol_key(唯一), code, name, asset_type, market, currency, price_tick, qty_step, min_notional, lot_size, is_active, sort_order, meta_json` | 跨资产类型标的库；`symbol_key = 类型:市场:代码` |

### 4.2 交易库（只读）

`runs / trades / orders / fills / equity / signals / risk_events / bar_flow / book_snapshot`，
由 `scalpx` 引擎写入。Web 层通过 `file:...?mode=ro` 只读连接访问，
**唯一写操作**是管理员删除运行（独立可写连接，显式运维动作）。

缺失表/缺库一律降级为空结果，不抛 500——首页在空库上也必须能打开。

---

## 5. 一键部署

### 5.1 快速开始

```bash
# 在项目根目录
bash deploy/install.sh                 # 交互式
bash deploy/install.sh -y --port 9000  # 静默
bash deploy/install.sh --no-service --dir ~/scalpx   # 只部署文件（容器/开发机）
```

安装完成后终端会输出访问地址与**初始管理员凭据**（同时落盘到
`<安装目录>/data/web/initial_credentials.txt`，权限 600）。

### 5.2 六个阶段（流程与校验点）

| # | 阶段 | 具体动作 | 失败处理 |
|---|---|---|---|
| 1 | 环境检测 | 识别发行版/包管理器/init 系统/架构；检查内存、磁盘、端口占用、重复安装 | 端口占用直接中止并提示换端口 |
| 2 | 依赖检测与安装 | 查找 Python ≥ 3.11；缺失则按包管理器安装；校验 `sqlite3/hmac/http.server` 可导入 | 安装后仍不满足则中止 |
| 3 | 目录与运行用户 | 创建系统用户 `scalpx`（root 时）、`data/ web/ runs/ logs/`、同步代码（rsync 或 tar 管道） | 权限/复制失败即中止 |
| 4 | 配置与账号生成 | 生成 `secret.key`（已有则保留）、复制 TOML（已有则保留）、用户表为空时创建 admin 并写凭据 | 生成失败即中止 |
| 5 | 注册并启动服务 | systemd（含安全加固）或 OpenRC；都没有则退出为 `nohup` + pid 文件 | 启动失败打印 `systemctl status` 与日志尾部 |
| 6 | 健康校验与自检 | 轮询 `/api/health` 最多 30 次；再跑 `scalpx.web check` | 超时打印日志尾部并中止 |

### 5.3 环境适配

| 发行版 | 包管理器 | 服务管理 |
|---|---|---|
| Debian / Ubuntu | `apt-get` | systemd |
| RHEL / CentOS / Rocky / Alma | `dnf` / `yum` | systemd |
| Alpine | `apk` | OpenRC |
| Arch | `pacman` | systemd |
| openSUSE | `zypper` | systemd |
| macOS（开发） | `brew` | launchd / nohup |

无 systemd/OpenRC 时自动退化为 `nohup`，并打印手动启动命令。

### 5.4 安装器设计要点

- **`set -Eeuo pipefail` + `trap ERR`**：任何一行失败都打印行号、退出码与日志尾部，
  不会出现「静默失败但显示成功」。
- **全量日志**：`/var/log/scalpx-web-install-<时间戳>.log`（无权限时退到 `TMPDIR`），
  同时输出到终端。
- **可重复执行**：代码可覆盖同步，`data/`、`runs/`、`logs/` 与已有 `config/*.toml` 一律保留；
  `secret.key` 与管理员账号不会重复生成（否则会话全失效 / 口令被重置）。
- **`--force`**：只覆盖程序文件，不碰数据。
- **不污染系统 Python**：不执行 `pip install`，不建 venv（核心零依赖），因此不受 PEP 668
  「externally-managed-environment」限制。

### 5.5 卸载

```bash
bash deploy/uninstall.sh            # 交互：是否删除数据目录
bash deploy/uninstall.sh -y         # 全自动（含删除数据）
```

停止并禁用服务、删除 unit 文件；数据目录默认保留。

### 5.6 反向代理（可选，公网必做）

```nginx
server {
    listen 443 ssl http2;
    server_name quant.example.com;

    ssl_certificate     /etc/letsencrypt/live/quant.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/quant.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

服务已读取 `X-Forwarded-For` 作为客户端 IP（登录限流按 IP 维度统计）。

---

## 6. 安全设计

| 面 | 措施 |
|---|---|
| 口令存储 | PBKDF2-HMAC-SHA256，26 万轮，每用户随机盐，`hmac.compare_digest` 比较 |
| 会话 | HMAC 签名令牌，12 小时过期；`token_version` 支持「一键失效全部会话」 |
| 暴力破解 | `(IP, 用户名)` 双维度计数，6 次失败锁定 5 分钟 |
| 越权 | 路由级 `roles` + 资源级归属校验（`run_owner`） |
| 注入 | 全部 SQL 参数化；配置文件路径用正则白名单 |
| 路径穿越 | 静态资源 `resolve()` 后校验前缀，越界返回 403 |
| 配置误改 | 写临时文件 → 正式加载器校验 → 备份 → 原子替换 |
| 越权提级 | 不能删除自己；不能把最后一个启用管理员降级/禁用 |
| 审计 | 登录、登录失败、用户增删改、配置修改、风控、下载、删除运行全记录 |
| 服务加固 | systemd `NoNewPrivileges` / `ProtectSystem=full` / `ProtectHome` / `ReadWritePaths` 限定 |
| 响应头 | `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer` |

**明确不做**（单机私有部署场景下的取舍）：CSRF token（同源 + Bearer 而非 Cookie，
天然免疫）、多租户隔离、OAuth/SSO、操作二次确认（除删除类）。

---

## 7. 故障排查

| 现象 | 排查 | 处置 |
|---|---|---|
| 安装卡在第 2 步 | 无外网 / 源不可用 | `--skip-deps` 预装 Python 后重跑 |
| 第 6 步健康检查超时 | `journalctl -u scalpx-web -n 50` | 多为端口占用或 `data/` 权限不对 |
| 登录 401 | 账号未创建 | `python -m scalpx.web create-user --username admin --role admin` |
| 忘记口令 | — | `python -m scalpx.web reset-password --username admin`（旧会话失效） |
| 页面空白但接口正常 | 浏览器缓存了旧 JS | 强刷（服务端已对 html/js 设 `no-cache`） |
| 后台 403 | 当前账号不是 admin | 用 admin 登录，或改库中 `role` 字段 |
| 首页无数据 | 交易库为空 | 先在「运行记录 → 运行新回测」跑一次 |

---

## 8. 验证记录

| 项 | 方式 | 结果 |
|---|---|---|
| 接口契约与权限 | `tests/test_web.py` 真启 HTTP 服务，22 个用例 | 通过 |
| 全量回归 | `python -m unittest discover -s tests -t .` | 103 个用例通过 |
| 安装脚本语法 | `bash -n deploy/install.sh` | 通过 |
| 安装脚本实跑 | Git Bash（无 systemd、无 root）`--no-service --skip-deps` | 6 步全过，自检 OK，凭据生成正确 |
| 真实服务 | 启动后 `/api/health`、登录、`/api/runs`、`/api/dashboard`、`/api/admin/system` | 返回真实数据（601 个权益点 / 4799 笔交易） |

> 未在真实 systemd 主机上验证（当前环境无 systemd）。`write_systemd_unit` 生成的 unit
> 可在任意 systemd 主机上通过 `systemctl cat scalpx-web` 复核。
