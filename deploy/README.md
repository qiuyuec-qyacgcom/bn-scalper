# 部署手册（安装器 v2）

一键部署 `scalpx-web`（前台 + 后台 + 引擎）。设计目标是**可预演、可重复、失败可回滚**。

---

## 1. 目标环境

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux：Debian/Ubuntu、RHEL/CentOS/Rocky/Alma、Alpine、Arch、openSUSE；macOS 可跑（服务以 nohup 方式） |
| Shell | **bash ≥ 4.0**（Alpine 需先 `apk add bash`） |
| Python | **≥ 3.11**，缺失时脚本按包管理器自动安装 |
| 权限 | 注册系统服务需 root 或 sudo；非 root 自动退化为 `--no-service` |
| 端口 | 默认 8787，启动前检测占用 |
| 资源 | 内存 ≥ 512MB、磁盘 ≥ 512MB（不足仅告警，不阻断） |
| 网络 | 仅自动装依赖时需要包源；纯部署离线可跑（`--skip-deps`） |

**不需要**：Node、Nginx、Redis、数据库服务（SQLite 内嵌）。核心零第三方依赖，不执行 `pip install`，
因此不受 PEP 668（externally-managed-environment）限制。

---

## 2. 输入参数

### 动作（互斥，默认 install）

| 参数 | 说明 |
|---|---|
| *(无)* | 安装或升级，幂等 |
| `--check` | 只做环境与部署自检，不改动系统 |
| `--dry-run` | 预演：打印将要执行的命令，不修改任何文件 |
| `--upgrade` | 升级：保留 `data/` 与 `config/`，强制刷新程序文件 |
| `--rollback [DIR]` | 回滚到最近一次备份（或指定备份目录） |
| `--uninstall` | 卸载服务，默认**保留**数据目录 |
| `--purge` | 与 `--uninstall` 联用，一并删除数据目录 |

### 配置项与默认值

| 参数 | 默认 | 说明 |
|---|---|---|
| `--dir <路径>` | `/opt/scalpx` | 安装目录 |
| `--user <用户>` | `scalpx` | 运行服务的系统用户（root 安装时自动创建） |
| `--port <端口>` | `8787` | 监听端口 |
| `--host <地址>` | `0.0.0.0` | 监听地址 |
| `--backup-dir <路径>` | `/var/backups/scalpx` | 备份根目录（不可写时退到 `<安装目录>/.backup`） |
| `--keep <N>` | `5` | 保留最近 N 份备份 |
| `--no-service` | 关 | 只部署文件，不注册系统服务 |
| `--skip-deps` | 关 | 跳过依赖安装 |
| `-y, --yes` | 关 | 不交互（**注意**：卸载时 `-y` 仍保留数据，删除数据必须显式 `--purge`） |
| `-v, --verbose` | 关 | 更详细的过程日志 |
| `-h, --help` | — | 帮助 |

环境变量：`SCALPX_DIR`、`SCALPX_USER`、`SCALPX_BACKUP_DIR`（等价于对应参数，优先级低于命令行）。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 未分类失败 |
| 2 | 参数错误 |
| 3 | 前置条件不满足（端口占用、源码不完整等） |
| 4 | 依赖安装失败 |
| 5 | 服务启动失败 |
| 6 | 健康检查超时 |
| 7 | 已执行回滚（安装失败但系统已复原） |

---

## 3. 分步流程

```
[1/8] 环境检测      发行版/包管理器/init 系统/架构、bash 版本、源码完整性、
                    内存与磁盘（告警）、端口占用（阻断）、重复安装识别
[2/8] 依赖检测安装  Python ≥3.11 探测 → 缺失按 apt/dnf/yum/apk/pacman/zypper/brew 安装
                    → 校验 sqlite3/http.server 可导入 → 确定服务管理器
[3/8] 目录与用户    系统用户、data/ web/ runs/ logs/、权限
[4/8] 备份与同步    备份旧代码与配置 → rsync 或 tar 管道同步（排除 data/runs/logs/config）
[5/8] 配置与账号    secret.key（已有则保留）、TOML（缺失才复制）、用户表为空才建 admin
[6/8] 服务注册      systemd unit（安全加固）/ OpenRC 脚本 / nohup 降级、logrotate
[7/8] 启动服务      停旧进程 → 启动 → 记录 pid
[8/8] 健康校验      轮询 /api/health（≤40s）→ 校验 web_db/static/config → 跑 scalpx.web check
```

每一步都会打印耗时；健康检查每 5 秒输出一个点，避免自动化环境误判为卡死。

---

## 4. 幂等性

重跑同一条命令不会产生副作用。逐项保证：

| 对象 | 已存在时的行为 |
|---|---|
| 系统用户 | 跳过创建 |
| 安装目录 | `mkdir -p`，不删除已有内容 |
| 程序文件 | 覆盖（`data/`、`runs/`、`logs/`、`config/` 被排除） |
| `config/*.toml` | **已存在则保留**，只补缺失的（用户改动不会丢） |
| `data/web/secret.key` | 保留（重建会导致所有会话失效） |
| 管理员账号 | 用户表非空则跳过，不重置口令 |
| 服务 | 重写 unit 后 `restart` |
| 备份目录 | 每次新建时间戳目录，自动清理只保留最近 N 份 |

**验证过的场景**（见 §8）：安装 → 改配置 → `--upgrade` → 配置值不变、账号不重建。

---

## 5. 失败与回滚

### 5.1 自动回滚

安装过程中每个会改动系统的动作都登记进**回滚栈**（`push_rollback`）。
任何一步失败（`trap ERR`）会：

1. 打印失败行号与退出码；
2. **反向撤销**已完成的动作（删新建目录、删新建用户、恢复旧代码、恢复配置、删 unit、停进程）；
3. 输出日志尾部 20 行，退出码 **7**。

回滚本身失败不会中断后续回滚项，只会逐条报告"需人工检查"。

### 5.2 手动回滚

```bash
bash deploy/install.sh --rollback              # 用最近一次备份
bash deploy/install.sh --rollback /var/backups/scalpx/20261102-060242
```

备份目录内容：

| 文件 | 内容 |
|---|---|
| `code.tar.gz` | 升级前的程序文件（首次安装时不存在） |
| `config/` | 升级前的配置目录 |
| `unit.bak` | 升级前的 systemd unit |
| `manifest.txt` | 安装目录、端口、Python 路径、时间戳 |

回滚会停服务 → 恢复代码 → 恢复配置 → 恢复 unit → 重启（systemd）。

### 5.3 卸载

```bash
bash deploy/uninstall.sh                 # 交互，保留数据
bash deploy/uninstall.sh --purge -y      # 连数据一起删
```

停止服务（systemd/OpenRC/nohup 均支持，等待进程退出后再释放目录）、删除 unit 与 logrotate 配置。
目录删除带 **3 次重试**，避免"卸载成功但目录残留"。

---

## 6. 日志

| 位置 | 内容 |
|---|---|
| `/var/log/scalpx-web-install-<时间戳>.log`（无权限时 `$TMPDIR`） | 安装全过程，含每条实际执行的命令、回滚栈登记、耗时 |
| `<安装目录>/logs/web.log` | 服务运行日志（systemd 下由 `StandardOutput` 直接追加） |
| `/etc/logrotate.d/scalpx-web` | 每日轮转，保留 14 天，`copytruncate` |

终端输出与日志并行写入，互不阻塞。

---

## 7. 运维命令

```bash
# 健康探测（给 cron/监控用，退出码 0/1）
bash deploy/healthcheck.sh --port 8787

# 部署自检（不改系统）
bash deploy/install.sh --check
# 或直接调用
python3 -m scalpx.web check --root /opt/scalpx

# 服务管理
systemctl status scalpx-web
systemctl restart scalpx-web
journalctl -u scalpx-web -f

# 重置管理员口令
cd /opt/scalpx && python3 -m scalpx.web reset-password --username admin
```

`healthcheck.sh` 输出示例：

```
status=ok version=1.0.0 uptime=4s
  web_db      : ok
  static      : ok
  config      : ok
  trading_db  : FAIL      ← 新装机器还没跑过回测，属正常
```

建议把健康探测挂到 cron：

```cron
*/5 * * * * root bash /opt/scalpx/deploy/healthcheck.sh --quiet || systemctl restart scalpx-web
```

---

## 8. 验收记录

在 Git Bash（无 systemd、非 root）实跑，全部通过：

| 场景 | 命令 | 结果 |
|---|---|---|
| 全新安装 | `--no-service --skip-deps --dir /tmp/scalpx-v3 --port 8794 -y` | exit 0，8/8 步完成，健康检查通过 |
| 配置保留 | 改 `risk_per_trade=0.0011` 后 `--upgrade` | 值保持 `0.0011`，账号未被重建 |
| 自检 | `--check` | exit 0，`状态: OK (with warnings)` |
| 健康探针 | `healthcheck.sh --port 8794` | exit 0，四项 checks 输出 |
| 卸载 | `--uninstall --purge -y` | exit 0，目录已删除 |
| 回滚 | 破坏 `server.py` 后 `--rollback` | exit 0，文件从 `code.tar.gz` 复原 |
| 预演 | `--dry-run` | 只打印动作，未创建任何文件 |

真机 Linux（systemd 分支）未在当前环境验证，unit 内容可用 `systemctl cat scalpx-web` 复核。

---

## 9. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `需要 bash >= 4.0` | Alpine 默认 ash | `apk add bash` 后重跑 |
| 端口占用（退出码 3） | 已有实例或其它服务 | `--port` 换端口，或先停旧服务 |
| 依赖安装失败（退出码 4） | 无外网 / 源不可用 | 预装 Python ≥3.11 后用 `--skip-deps` |
| 健康检查超时（退出码 6） | 端口不通、目录权限错 | 脚本已打印 `systemctl status` 与日志尾部 |
| 目录删不掉 | 进程仍占用 | 脚本会重试 3 次；仍失败时手动 `rm -rf` |
| 卸载后账号还在 | 数据目录默认保留 | 加 `--purge` |

---

## 10. 文件清单

| 文件 | 作用 |
|---|---|
| `install.sh` | 安装器主程序（安装/升级/回滚/卸载/自检） |
| `uninstall.sh` | 卸载薄封装，保证默认保留数据 |
| `healthcheck.sh` | 外部健康探针（HTTP，退出码语义化） |
