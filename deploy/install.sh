#!/usr/bin/env bash
# =============================================================================
#  scalpx-web 一键安装脚本
#
#  设计参考宝塔面板安装器的六段式：环境检测 → 依赖安装 → 目录与用户 →
#  配置生成 → 服务注册 → 健康校验。每一段都可单独重跑，失败即停并给出定位。
#
#  用法：
#    bash deploy/install.sh                    # 交互式安装（默认 8787 端口）
#    bash deploy/install.sh -y --port 9000     # 静默安装
#    bash deploy/install.sh --uninstall        # 卸载（保留数据需显式确认）
#    bash deploy/install.sh --help
#
#  支持：Debian/Ubuntu、RHEL/CentOS/Rocky/Alma、Alpine、Arch、openSUSE、macOS
# =============================================================================
set -Eeuo pipefail

# 保证 Python 输出与日志文件都是 UTF-8，避免非 UTF-8 终端下中文乱码
export PYTHONIOENCODING="utf-8"
export PYTHONUTF8="1"
export LC_ALL="${LC_ALL:-C.UTF-8}" 2>/dev/null || true

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NAME="scalpx-web"
SERVICE_NAME="scalpx-web"
DEFAULT_PORT=8787
DEFAULT_HOST="0.0.0.0"
MIN_PY_MINOR=11
LOG_DIR="/var/log"
START_TS=$(date +%s)
TOTAL_STEPS=6

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${SCALPX_DIR:-/opt/scalpx}"
RUN_USER="${SCALPX_USER:-scalpx}"
PORT="$DEFAULT_PORT"
HOST="$DEFAULT_HOST"
ASSUME_YES=0
FORCE=0
NO_SERVICE=0
SKIP_DEPS=0
ACTION="install"

# --------------------------------------------------------------------------- #
# 颜色与日志
# --------------------------------------------------------------------------- #
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[36m'; C_GRAY=$'\033[90m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_GRAY=""; C_BOLD=""; C_RESET=""
fi

LOG_FILE=""
if [ -w "$LOG_DIR" ] || [ "$(id -u)" = "0" ]; then
  LOG_FILE="$LOG_DIR/${APP_NAME}-install-$(date +%Y%m%d-%H%M%S).log"
else
  LOG_FILE="${TMPDIR:-/tmp}/${APP_NAME}-install-$(date +%Y%m%d-%H%M%S).log"
fi
: > "$LOG_FILE" 2>/dev/null || LOG_FILE="${TMPDIR:-/tmp}/${APP_NAME}-install.log"

_write_log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

log_info() { printf '  %s\n' "$*"; _write_log "[INFO ] $*"; }
log_ok()   { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; _write_log "[ OK  ] $*"; }
log_warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; _write_log "[WARN ] $*"; }
log_err()  { printf '  %s✘%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; _write_log "[ERROR] $*"; }
log_dim()  { printf '  %s%s%s\n' "$C_GRAY" "$*" "$C_RESET"; _write_log "[DEBUG] $*"; }

step() {
  local idx="$1"; shift
  printf '\n%s[%s/%s]%s %s%s%s\n' \
    "$C_BLUE" "$idx" "$TOTAL_STEPS" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"
  _write_log ""
  _write_log "==== [$idx/$TOTAL_STEPS] $* ===="
}

die() {
  log_err "$*"
  printf '\n%s安装失败%s  完整日志: %s\n' "$C_RED" "$C_RESET" "$LOG_FILE"
  if [ -s "$LOG_FILE" ]; then
    printf '%s最近 15 行日志：%s\n' "$C_GRAY" "$C_RESET"
    tail -n 15 "$LOG_FILE" | sed 's/^/    /'
  fi
  exit 1
}

_on_error() {
  local code=$1 line=$2
  log_err "第 ${line} 行执行失败（退出码 ${code}）"
  _write_log "[TRACE] 失败位置: ${BASH_SOURCE[0]}:${line}"
  printf '\n%s安装中断%s  完整日志: %s\n' "$C_RED" "$C_RESET" "$LOG_FILE"
  [ -s "$LOG_FILE" ] && tail -n 15 "$LOG_FILE" | sed 's/^/    /'
  exit "$code"
}
trap '_on_error $? $LINENO' ERR
trap 'printf "\n%s已取消%s\n" "$C_YELLOW" "$C_RESET"; exit 130' INT TERM

# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
usage() {
  cat <<'EOF'
scalpx-web 一键安装脚本

用法:
  bash deploy/install.sh [选项]

选项:
  -y, --yes             全部使用默认值，不交互
      --port <端口>     监听端口（默认 8787）
      --host <地址>     监听地址（默认 0.0.0.0）
      --dir <路径>      安装目录（默认 /opt/scalpx）
      --user <用户>     运行服务的系统用户（默认 scalpx）
      --no-service      只部署文件，不注册/启动系统服务
      --skip-deps       跳过依赖安装（离线或已自备环境）
      --force           强制覆盖代码（保留 data/ 数据）
      --uninstall       卸载服务（可选删除数据）
  -h, --help            显示帮助

示例:
  bash deploy/install.sh -y --port 9000
  bash deploy/install.sh --no-service --dir ~/scalpx
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --port) PORT="${2:?缺少端口值}"; shift ;;
    --host) HOST="${2:?缺少地址值}"; shift ;;
    --dir) INSTALL_DIR="${2:?缺少目录值}"; shift ;;
    --user) RUN_USER="${2:?缺少用户名}"; shift ;;
    --no-service) NO_SERVICE=1 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --force) FORCE=1 ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（使用 --help 查看用法）" ;;
  esac
  shift
done

case "$PORT" in
  ''|*[!0-9]*) die "端口必须是数字: $PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围: $PORT"

IS_ROOT=0
[ "$(id -u)" = "0" ] && IS_ROOT=1

SUDO=""
if [ "$IS_ROOT" = "0" ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

run_privileged() {
  if [ "$IS_ROOT" = "1" ]; then "$@"; else $SUDO "$@"; fi
}

# --------------------------------------------------------------------------- #
# 环境探测
# --------------------------------------------------------------------------- #
OS_ID=""; OS_LIKE=""; OS_VERSION=""; OS_PRETTY=""; PKG=""; INIT_SYS="none"
PY_BIN=""

detect_os() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
    OS_VERSION="${VERSION_ID:-}"
    OS_PRETTY="${PRETTY_NAME:-$OS_ID}"
  elif [ "$(uname -s)" = "Darwin" ]; then
    OS_ID="macos"; OS_PRETTY="macOS $(sw_vers -productVersion 2>/dev/null || echo '')"
  else
    OS_ID="$(uname -s | tr '[:upper:]' '[:lower:]')"; OS_PRETTY="$OS_ID"
  fi

  if command -v apt-get >/dev/null 2>&1; then PKG="apt"
  elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
  elif command -v yum >/dev/null 2>&1; then PKG="yum"
  elif command -v apk >/dev/null 2>&1; then PKG="apk"
  elif command -v pacman >/dev/null 2>&1; then PKG="pacman"
  elif command -v zypper >/dev/null 2>&1; then PKG="zypper"
  elif command -v brew >/dev/null 2>&1; then PKG="brew"
  else PKG="none"
  fi

  if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then INIT_SYS="systemd"
  elif command -v rc-service >/dev/null 2>&1; then INIT_SYS="openrc"
  elif [ "$OS_ID" = "macos" ]; then INIT_SYS="launchd"
  else INIT_SYS="none"
  fi
}

find_python() {
  local candidates="python3.13 python3.12 python3.11 python3 python"
  for c in $candidates; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
      then
        PY_BIN="$(command -v "$c")"
        return 0
      fi
    fi
  done
  return 1
}

pkg_install() {
  local pkgs="$*"
  log_info "安装系统包: $pkgs"
  case "$PKG" in
    apt)
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get update -qq || log_warn "apt-get update 失败，尝试继续"
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs
      ;;
    dnf) run_privileged dnf install -y -q $pkgs ;;
    yum) run_privileged yum install -y -q $pkgs ;;
    apk) run_privileged apk add --no-cache $pkgs ;;
    pacman) run_privileged pacman -Sy --noconfirm --needed $pkgs ;;
    zypper) run_privileged zypper --non-interactive install $pkgs ;;
    brew) brew install $pkgs ;;
    *) die "未知的包管理器，无法自动安装依赖：$pkgs" ;;
  esac
}

python_packages() {
  case "$PKG" in
    apt) echo "python3 python3-venv curl" ;;
    dnf|yum) echo "python3 python3-pip curl" ;;
    apk) echo "python3 py3-pip curl" ;;
    pacman) echo "python python-pip curl" ;;
    zypper) echo "python3 python3-pip curl" ;;
    brew) echo "python3" ;;
    *) echo "python3" ;;
  esac
}

# --------------------------------------------------------------------------- #
# 步骤 1：环境检测
# --------------------------------------------------------------------------- #
check_environment() {
  detect_os
  log_info "系统      : ${OS_PRETTY}"
  log_info "包管理器  : ${PKG}"
  log_info "服务管理  : ${INIT_SYS}"
  log_info "架构      : $(uname -m)"
  log_info "安装目录  : ${INSTALL_DIR}"

  if [ "$IS_ROOT" = "0" ] && [ -z "$SUDO" ]; then
    log_warn "当前非 root 且无 sudo：将使用 --no-service 模式部署到用户目录"
    NO_SERVICE=1
  fi

  # 内存与磁盘（不足只告警，不阻断）
  local mem_mb disk_mb
  if [ -r /proc/meminfo ]; then
    mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    [ "${mem_mb:-0}" -lt 512 ] && log_warn "可用内存 ${mem_mb}MB 偏低（建议 ≥ 512MB）"
  fi
  disk_mb=$(df -Pm "$(dirname "$INSTALL_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
  [ "${disk_mb:-0}" -gt 0 ] && [ "$disk_mb" -lt 512 ] && log_warn "剩余磁盘 ${disk_mb}MB 偏低（建议 ≥ 512MB）"

  # 端口占用检测
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
    die "端口 ${PORT} 已被占用，请用 --port 指定其它端口"
  fi

  # 重复安装检测
  if [ -f "$INSTALL_DIR/scalpx/web/server.py" ]; then
    log_warn "检测到已存在的安装：${INSTALL_DIR}（将保留 data/ 与 config/ 变更）"
  fi
  log_ok "环境检测通过"
}

# --------------------------------------------------------------------------- #
# 步骤 2：依赖安装
# --------------------------------------------------------------------------- #
install_dependencies() {
  if [ "$SKIP_DEPS" = "1" ]; then
    log_warn "已指定 --skip-deps，跳过依赖安装"
  fi

  if find_python; then
    log_ok "Python 已就绪: ${PY_BIN} ($("$PY_BIN" -V 2>&1))"
  else
    [ "$SKIP_DEPS" = "1" ] && die "未找到 Python ≥ 3.${MIN_PY_MINOR}，且已跳过依赖安装"
    log_warn "未找到 Python ≥ 3.${MIN_PY_MINOR}，开始自动安装"
    pkg_install "$(python_packages)"
    find_python || die "自动安装后仍未找到可用的 Python ≥ 3.${MIN_PY_MINOR}，请手动安装"
    log_ok "Python 已安装: ${PY_BIN} ($("$PY_BIN" -V 2>&1))"
  fi

  # 运行时自检：核心模块必须可导入（部署目录就绪后由步骤 5 再做一次完整校验）
  "$PY_BIN" - <<'PY' || die "Python 环境异常（无法导入标准库 sqlite3/hmac）"
import sqlite3, hmac, http.server  # noqa: F401
print("      stdlib 自检通过，SQLite", sqlite3.sqlite_version)
PY

  if command -v systemctl >/dev/null 2>&1 || command -v rc-service >/dev/null 2>&1; then
    log_ok "服务管理器可用"
  elif [ "$NO_SERVICE" = "0" ]; then
    log_warn "未检测到 systemd/openrc，将退化为 nohup 方式启动"
    NO_SERVICE=1
  fi
}

# --------------------------------------------------------------------------- #
# 步骤 3：目录与用户
# --------------------------------------------------------------------------- #
prepare_dirs() {
  if [ "$IS_ROOT" = "1" ] && [ "$NO_SERVICE" = "0" ]; then
    if ! id -u "$RUN_USER" >/dev/null 2>&1; then
      log_info "创建系统用户: $RUN_USER"
      if command -v useradd >/dev/null 2>&1; then
        useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" "$RUN_USER" 2>/dev/null \
          || useradd -r -s /sbin/nologin -d "$INSTALL_DIR" "$RUN_USER"
      elif command -v adduser >/dev/null 2>&1; then
        adduser -S -H -h "$INSTALL_DIR" -s /sbin/nologin "$RUN_USER"
      fi
    else
      log_dim "用户已存在: $RUN_USER"
    fi
  else
    RUN_USER="$(id -un)"
    log_dim "以当前用户运行: $RUN_USER"
  fi

  run_privileged mkdir -p "$INSTALL_DIR"
  run_privileged mkdir -p "$INSTALL_DIR/data/web" "$INSTALL_DIR/runs" "$INSTALL_DIR/logs"
  [ "$IS_ROOT" = "1" ] && [ "$RUN_USER" != "root" ] && chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true
  chmod 750 "$INSTALL_DIR/data/web" 2>/dev/null || true
  log_ok "目录就绪: $INSTALL_DIR"
}

copy_code() {
  local preserve="data runs logs .git .venv __pycache__"
  if [ "$FORCE" = "1" ]; then
    log_warn "--force：将用源码覆盖程序文件（保留 data/runs/logs）"
  fi

  if command -v rsync >/dev/null 2>&1; then
    local exclude_args=""
    for p in $preserve; do exclude_args="$exclude_args --exclude=$p"; done
    # shellcheck disable=SC2086
    run_privileged rsync -a $exclude_args --exclude='*.pyc' --exclude='*.db' \
      "$SRC_DIR/" "$INSTALL_DIR/"
  else
    # 无 rsync 时用 tar 管道，同样排除数据目录
    local excludes=""
    for p in $preserve; do excludes="$excludes --exclude=./$p"; done
    # shellcheck disable=SC2086
    ( cd "$SRC_DIR" && tar -cf - $excludes --exclude='*.pyc' --exclude='./data/*.db' . ) \
      | run_privileged tar -xf - -C "$INSTALL_DIR"
  fi
  [ "$IS_ROOT" = "1" ] && [ "$RUN_USER" != "root" ] && chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true
  log_ok "程序文件已同步"
}

# --------------------------------------------------------------------------- #
# 步骤 4：配置生成
# --------------------------------------------------------------------------- #
generate_config() {
  # 4.1 会话签名密钥（首次生成，之后保持不变，否则所有会话失效）
  local secret_file="$INSTALL_DIR/data/web/secret.key"
  if [ -f "$secret_file" ]; then
    log_dim "会话密钥已存在，保持不变"
  else
    "$PY_BIN" - "$secret_file" <<'PY'
import base64, os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "wb") as fh:
    fh.write(base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"="))
os.chmod(path, 0o600)
PY
    log_ok "会话密钥已生成"
  fi

  # 4.2 回测配置：首次安装才复制，避免覆盖用户改动
  if [ -f "$INSTALL_DIR/config/backtest.toml" ]; then
    log_dim "config/backtest.toml 已存在，保留用户配置"
  else
    if [ -d "$SRC_DIR/config" ]; then
      run_privileged mkdir -p "$INSTALL_DIR/config"
      run_privileged cp -f "$SRC_DIR"/config/*.toml "$INSTALL_DIR/config/" 2>/dev/null || true
      log_ok "配置文件已复制"
    else
      die "源码中缺少 config/ 目录"
    fi
  fi

  # 4.3 管理员账号：仅在用户表为空时创建，初始口令落盘（600）
  local cred_file="$INSTALL_DIR/data/web/initial_credentials.txt"
  local user_count
  user_count=$("$PY_BIN" - "$INSTALL_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from scalpx.web.repo import WebRepo
    repo = WebRepo(sys.argv[1] + "/data/web.db")
    print(repo.user_count())
    repo.close()
except Exception:
    print(0)
PY
)
  if [ "${user_count:-0}" -gt 0 ]; then
    log_dim "已存在 ${user_count} 个账号，跳过初始化"
  else
    local admin_pw
    admin_pw=$("$PY_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(12))
PY
)
    ( cd "$INSTALL_DIR" && "$PY_BIN" -m scalpx.web create-user --username admin --password "$admin_pw" --role admin >/dev/null )
    umask 077
    {
      echo "scalpx-web 初始管理员凭据（请登录后立即修改）"
      echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
      echo "用户名  : admin"
      echo "口令    : $admin_pw"
      echo "登录地址: http://<服务器IP>:${PORT}/admin"
    } > "$cred_file"
    umask 022
    chmod 600 "$cred_file" 2>/dev/null || true
    log_ok "管理员账号已创建，凭据写入 $cred_file"
  fi
}

# --------------------------------------------------------------------------- #
# 步骤 5：注册并启动服务
# --------------------------------------------------------------------------- #
write_systemd_unit() {
  local unit="/etc/systemd/system/${SERVICE_NAME}.service"
  run_privileged tee "$unit" >/dev/null <<EOF
[Unit]
Description=scalpx web console (quant trading control panel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=${PY_BIN} -m scalpx.web serve --root ${INSTALL_DIR} --host ${HOST} --port ${PORT}
Restart=always
RestartSec=3
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/data ${INSTALL_DIR}/runs ${INSTALL_DIR}/logs
StandardOutput=append:${INSTALL_DIR}/logs/web.log
StandardError=append:${INSTALL_DIR}/logs/web.log

[Install]
WantedBy=multi-user.target
EOF
  run_privileged systemctl daemon-reload
  run_privileged systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  log_ok "systemd 服务已注册: $unit"
}

write_openrc_script() {
  local svc="/etc/init.d/${SERVICE_NAME}"
  run_privileged tee "$svc" >/dev/null <<EOF
#!/sbin/openrc-run
name="${SERVICE_NAME}"
description="scalpx web console"
command="${PY_BIN}"
command_args="-m scalpx.web serve --root ${INSTALL_DIR} --host ${HOST} --port ${PORT}"
command_user="${RUN_USER}"
directory="${INSTALL_DIR}"
output_log="${INSTALL_DIR}/logs/web.log"
error_log="${INSTALL_DIR}/logs/web.log"
supervisor="supervise-daemon"
respawn_delay=3
EOF
  run_privileged chmod +x "$svc"
  run_privileged rc-update add "$SERVICE_NAME" default >/dev/null 2>&1 || true
  log_ok "OpenRC 服务已注册: $svc"
}

start_service() {
  if [ "$NO_SERVICE" = "1" ]; then
    log_warn "跳过服务注册（--no-service），请手动启动："
    log_dim "cd ${INSTALL_DIR} && ${PY_BIN} -m scalpx.web serve --host ${HOST} --port ${PORT}"
    return 0
  fi

  if [ "$INIT_SYS" = "systemd" ]; then
    run_privileged systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    write_systemd_unit
    run_privileged systemctl restart "$SERVICE_NAME" || die "服务启动失败，查看：systemctl status ${SERVICE_NAME}"
  elif [ "$INIT_SYS" = "openrc" ]; then
    write_openrc_script
    run_privileged rc-service "$SERVICE_NAME" restart >/dev/null 2>&1 || die "服务启动失败"
  else
    log_warn "无可用服务管理器，使用 nohup 启动"
    local pid_file="$INSTALL_DIR/logs/${SERVICE_NAME}.pid"
    [ -f "$pid_file" ] && kill "$(cat "$pid_file")" 2>/dev/null || true
    ( cd "$INSTALL_DIR" && nohup "$PY_BIN" -m scalpx.web serve --root "$INSTALL_DIR" --host "$HOST" --port "$PORT" \
        >> "$INSTALL_DIR/logs/web.log" 2>&1 & echo $! > "$pid_file" )
    sleep 1
  fi
  log_ok "服务已启动"
}

# --------------------------------------------------------------------------- #
# 步骤 6：健康校验
# --------------------------------------------------------------------------- #
health_check() {
  if [ "$NO_SERVICE" = "1" ]; then
    log_warn "未启动服务（--no-service），跳过健康检查"
    return 0
  fi
  local url="http://127.0.0.1:${PORT}/api/health"
  local tries=30 i=1
  log_info "等待服务就绪（最多 ${tries} 秒）…"
  while [ "$i" -le "$tries" ]; do
    if "$PY_BIN" - "$url" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=2) as resp:
    payload = json.loads(resp.read().decode())
assert payload.get("ok") is True
assert payload["data"]["status"] == "ok"
PY
    then
      log_ok "健康检查通过: ${url}"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done

  log_err "健康检查超时"
  if [ "$INIT_SYS" = "systemd" ]; then
    run_privileged systemctl --no-pager status "$SERVICE_NAME" 2>&1 | head -n 20 | sed 's/^/    /' || true
  fi
  [ -f "$INSTALL_DIR/logs/web.log" ] && tail -n 20 "$INSTALL_DIR/logs/web.log" | sed 's/^/    /' || true
  die "服务未能正常启动，请检查上面的日志"
}

self_check() {
  ( cd "$INSTALL_DIR" && "$PY_BIN" -m scalpx.web check --root "$INSTALL_DIR" ) | sed 's/^/    /'
  log_ok "部署自检通过"
}

# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #
do_uninstall() {
  printf '\n%s卸载 scalpx-web%s\n' "$C_BOLD" "$C_RESET"
  if [ "$INIT_SYS" = "systemd" ]; then
    run_privileged systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    run_privileged systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    run_privileged rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    run_privileged systemctl daemon-reload || true
    log_ok "systemd 服务已移除"
  elif [ "$INIT_SYS" = "openrc" ]; then
    run_privileged rc-service "$SERVICE_NAME" stop >/dev/null 2>&1 || true
    run_privileged rc-update del "$SERVICE_NAME" >/dev/null 2>&1 || true
    run_privileged rm -f "/etc/init.d/${SERVICE_NAME}"
    log_ok "OpenRC 服务已移除"
  fi

  local keep=1
  if [ "$ASSUME_YES" = "1" ]; then
    keep=0
  else
    printf '  是否同时删除数据目录 %s ？[y/N] ' "$INSTALL_DIR"
    read -r ans || true
    case "${ans:-N}" in y|Y|yes|YES) keep=0 ;; *) keep=1 ;; esac
  fi

  if [ "$keep" = "0" ]; then
    run_privileged rm -rf "$INSTALL_DIR"
    log_ok "已删除 $INSTALL_DIR"
  else
    log_warn "已保留数据目录：$INSTALL_DIR（可手动删除）"
  fi
  printf '\n%s卸载完成%s\n\n' "$C_GREEN" "$C_RESET"
}

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
banner() {
  printf '\n%s' "$C_BLUE"
  cat <<'EOF'
   ____   ____    _    _     ____  __  __    __        _____ _____
  / ___| / ___|  / \  | |   |  _ \ \ \/ /    \ \      / / __| __|
  \___ \| |     / _ \ | |   | |_) | \  /      \ \ /\ / /| _|| |_
   ___) | |___ / ___ \| |___|  __/  /  \       \ V  V / | |__| _|
  |____/ \____/_/   \_\_____|_|    /_/\_\       \_/\_/  |___|_|
EOF
  printf '%s  scalpx 量化交易控制台 · 一键部署%s\n' "$C_GRAY" "$C_RESET"
  printf '%s  日志: %s%s\n' "$C_GRAY" "$LOG_FILE" "$C_RESET"
}

summary() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
  [ -z "$ip" ] && ip="127.0.0.1"
  local cred="$INSTALL_DIR/data/web/initial_credentials.txt"
  local elapsed=$(( $(date +%s) - START_TS ))

  printf '\n%s════════════════════════════════════════════════════════════%s\n' "$C_GREEN" "$C_RESET"
  printf '%s  部署完成%s  用时 %s 秒\n' "$C_GREEN$C_BOLD" "$C_RESET" "$elapsed"
  printf '%s════════════════════════════════════════════════════════════%s\n' "$C_GREEN" "$C_RESET"
  printf '  安装目录 : %s\n' "$INSTALL_DIR"
  printf '  运行用户 : %s\n' "$RUN_USER"
  printf '  监听地址 : %s:%s\n' "$HOST" "$PORT"
  printf '\n  前台入口 : %shttp://%s:%s/%s\n' "$C_BLUE" "$ip" "$PORT" "$C_RESET"
  printf '  后台入口 : %shttp://%s:%s/admin%s\n' "$C_BLUE" "$ip" "$PORT" "$C_RESET"

  if [ -f "$cred" ]; then
    printf '\n  %s初始管理员凭据%s（请登录后立即修改）\n' "$C_YELLOW" "$C_RESET"
    sed 's/^/    /' "$cred"
    printf '    凭据文件: %s\n' "$cred"
  else
    printf '\n  管理员账号已存在，未重置。忘记口令可执行：\n'
    printf '    cd %s && %s -m scalpx.web reset-password --username admin\n' "$INSTALL_DIR" "$PY_BIN"
  fi

  printf '\n  %s常用命令%s\n' "$C_BOLD" "$C_RESET"
  if [ "$INIT_SYS" = "systemd" ] && [ "$NO_SERVICE" = "0" ]; then
    printf '    systemctl status %s\n' "$SERVICE_NAME"
    printf '    systemctl restart %s\n' "$SERVICE_NAME"
    printf '    journalctl -u %s -f        # 或 tail -f %s/logs/web.log\n' "$SERVICE_NAME" "$INSTALL_DIR"
  else
    printf '    cd %s && %s -m scalpx.web serve --port %s\n' "$INSTALL_DIR" "$PY_BIN" "$PORT"
  fi
  printf '    bash %s/deploy/install.sh --uninstall\n' "$SRC_DIR"
  printf '\n  %s安全提醒%s：首次登录后请立即改口令；公网部署建议前置 Nginx + HTTPS。\n\n' \
    "$C_YELLOW" "$C_RESET"
}

# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
main() {
  banner
  if [ "$ACTION" = "uninstall" ]; then
    detect_os
    do_uninstall
    exit 0
  fi

  step 1 "环境检测"
  check_environment

  step 2 "依赖检测与安装"
  install_dependencies

  step 3 "目录与运行用户"
  prepare_dirs
  copy_code

  step 4 "生成配置与管理员账号"
  generate_config

  step 5 "注册并启动服务"
  start_service

  step 6 "健康校验与自检"
  health_check
  self_check

  summary
}

main "$@"
