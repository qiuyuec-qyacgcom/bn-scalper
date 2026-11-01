#!/usr/bin/env bash
# =============================================================================
#  scalpx-web 安装器 v2
#
#  目标环境
#    Linux（Debian/Ubuntu、RHEL/CentOS/Rocky/Alma、Alpine、Arch、openSUSE）
#    macOS（开发用途，服务以 nohup 方式运行）
#    需要 root 或 sudo 才能注册系统服务；非 root 会自动退化为 --no-service
#
#  前置条件
#    · bash >= 4.0（Alpine 请先 apk add bash）
#    · Python >= 3.11（缺失时脚本按包管理器自动安装）
#    · 端口未被占用；安装目录可写；磁盘 >= 512MB（不足仅告警）
#
#  设计要点
#    1. 每个会改动系统的动作都先登记到**回滚栈**；任何一步失败即反向撤销，
#       不留下"装了一半"的状态。
#    2. 所有步骤幂等：重跑只补缺失项，不重置密钥、不覆盖用户配置、不重建账号。
#    3. 支持 --dry-run 预演（打印将要执行的命令，不改系统）。
#    4. 全流程写日志文件，终端只输出结构化进度。
#
#  退出码
#    0 成功  1 未分类失败  2 参数错误  3 前置条件不满足
#    4 依赖安装失败  5 服务启动失败  6 健康检查超时  7 已回滚
# =============================================================================
set -Eeuo pipefail

# 保证 Python 输出与日志文件统一 UTF-8
export PYTHONIOENCODING="utf-8"
export PYTHONUTF8="1"

# =============================================================================
# §1 常量
# =============================================================================
readonly APP_NAME="scalpx-web"
readonly SERVICE_NAME="scalpx-web"
readonly SCRIPT_VERSION="2.0.0"
readonly MIN_PY_MINOR=11

readonly DEFAULT_PORT=8787
readonly DEFAULT_HOST="0.0.0.0"
readonly DEFAULT_DIR="/opt/scalpx"
readonly DEFAULT_USER="scalpx"
readonly MIN_DISK_MB=512
readonly MIN_MEM_MB=512
readonly HEALTH_RETRIES=40

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 运行期状态（可被参数覆盖）
INSTALL_DIR="${SCALPX_DIR:-$DEFAULT_DIR}"
RUN_USER="${SCALPX_USER:-$DEFAULT_USER}"
PORT="$DEFAULT_PORT"
HOST="$DEFAULT_HOST"
BACKUP_ROOT="${SCALPX_BACKUP_DIR:-/var/backups/scalpx}"
KEEP_BACKUPS=5

ACTION="install"
ASSUME_YES=0
FORCE=0
NO_SERVICE=0
SKIP_DEPS=0
DRY_RUN=0
VERBOSE=0
PURGE=0
RESTORE_FROM=""

IS_ROOT=0
SUDO=""
OS_ID=""; OS_LIKE=""; OS_PRETTY=""; PKG="none"; INIT_SYS="none"
PY_BIN=""
STEP_INDEX=0
STEP_TOTAL=8   # 环境检测 / 依赖 / 目录 / 同步 / 配置 / 服务注册 / 启动 / 健康校验
START_TS=$(date +%s)
BACKUP_DIR=""
LOG_FILE=""

# =============================================================================
# §2 日志与进度
# =============================================================================
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[36m'; C_GRAY=$'\033[90m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_GRAY=""; C_BOLD=""; C_RESET=""
fi

init_log() {
  local target_dir="/var/log"
  if [ ! -w "$target_dir" ] || [ "$(id -u)" != "0" ]; then
    target_dir="${TMPDIR:-/tmp}"
  fi
  LOG_FILE="${target_dir}/${APP_NAME}-install-$(date +%Y%m%d-%H%M%S).log"
  if ! : > "$LOG_FILE" 2>/dev/null; then
    LOG_FILE="${TMPDIR:-/tmp}/${APP_NAME}-install.log"
    : > "$LOG_FILE" 2>/dev/null || true
  fi
}

_log() {
  [ -n "$LOG_FILE" ] || return 0
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

log_info() { printf '  %s\n' "$*"; _log "[INFO ] $*"; }
log_ok()   { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; _log "[ OK  ] $*"; }
log_warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; _log "[WARN ] $*"; }
log_err()  { printf '  %s✘%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; _log "[ERROR] $*"; }
log_dim()  { printf '  %s%s%s\n' "$C_GRAY" "$*" "$C_RESET"; _log "[DEBUG] $*"; }
log_debug() { [ "$VERBOSE" = "1" ] && log_dim "$*" || _log "[TRACE] $*"; }

step() {
  local title="$1"
  STEP_INDEX=$((STEP_INDEX + 1))
  printf '\n%s[%d/%d]%s %s%s%s\n' \
    "$C_BLUE" "$STEP_INDEX" "$STEP_TOTAL" "$C_RESET" "$C_BOLD" "$title" "$C_RESET"
  _log ""
  _log "==== [$STEP_INDEX/$STEP_TOTAL] $title ===="
  STEP_TS=$(date +%s)
}

step_done() {
  local elapsed=$(( $(date +%s) - STEP_TS ))
  log_dim "完成，用时 ${elapsed}s"
}

die() {
  local code="${2:-1}"
  log_err "$1"
  _log "[FATAL] $1"
  exit "$code"
}

# =============================================================================
# §3 回滚栈
# =============================================================================
ROLLBACK_DESC=()
ROLLBACK_CMD=()

push_rollback() {
  ROLLBACK_DESC+=("$1")
  ROLLBACK_CMD+=("$2")
  _log "[ROLLBACK+] $1 :: $2"
}

run_rollback() {
  local total=${#ROLLBACK_CMD[@]}
  [ "$total" -eq 0 ] && return 0
  printf '\n%s正在回滚（%d 个动作）%s\n' "$C_YELLOW" "$total" "$C_RESET"
  local i
  for (( i = total - 1; i >= 0; i-- )); do
    log_warn "回滚：${ROLLBACK_DESC[$i]}"
    if bash -c "${ROLLBACK_CMD[$i]}" >>"$LOG_FILE" 2>&1; then
      log_dim "  已撤销"
    else
      log_err "  回滚失败（需人工检查）：${ROLLBACK_DESC[$i]}"
    fi
  done
  _log "[ROLLBACK] 完成"
}

NO_ROLLBACK=0   # 卸载/回滚过程中失败不应再触发安装回滚栈

_on_error() {
  local code=$1 line=$2
  _log "[TRACE] 失败位置 ${BASH_SOURCE[0]}:${line} code=${code}"
  log_err "第 ${line} 行执行失败（退出码 ${code}）"
  if [ "$DRY_RUN" = "1" ]; then
    die "预演中断（未对系统做任何修改）" "$code"
  fi
  if [ "$NO_ROLLBACK" = "1" ]; then
    printf '\n%s操作失败%s  完整日志: %s\n' "$C_RED" "$C_RESET" "$LOG_FILE"
    [ -s "$LOG_FILE" ] && tail -n 20 "$LOG_FILE" | sed 's/^/    /'
    exit "$code"
  fi
  run_rollback
  printf '\n%s安装失败，已回滚%s\n' "$C_RED" "$C_RESET"
  printf '  完整日志: %s\n' "$LOG_FILE"
  [ -s "$LOG_FILE" ] && tail -n 20 "$LOG_FILE" | sed 's/^/    /'
  exit 7
}
trap '_on_error $? $LINENO' ERR
trap 'printf "\n%s已取消%s\n" "$C_YELLOW" "$C_RESET"; exit 130' INT TERM

# =============================================================================
# §4 执行包装（统一 dry-run 与提权）
# =============================================================================
run_priv() {
  local desc="$1"; shift
  log_info "$desc"
  if [ "$DRY_RUN" = "1" ]; then
    log_dim "  [dry-run] ${SUDO:+$SUDO }$*"
    return 0
  fi
  _log "[EXEC] ${SUDO:+$SUDO }$*"
  if [ -n "$SUDO" ]; then "$SUDO" "$@"; else "$@"; fi
}

run_sh() {
  local desc="$1"; local script="$2"
  log_info "$desc"
  if [ "$DRY_RUN" = "1" ]; then
    log_dim "  [dry-run] $script"
    return 0
  fi
  _log "[EXEC] $script"
  bash -c "$script"
}

write_file_priv() {
  local path="$1"
  if [ "$DRY_RUN" = "1" ]; then
    log_dim "  [dry-run] 写入 $path"
    cat > /dev/null
    return 0
  fi
  run_privileged_tee "$path"
}

run_privileged_tee() {
  local path="$1"
  if [ -n "$SUDO" ]; then "$SUDO" tee "$path" > /dev/null; else tee "$path" > /dev/null; fi
  _log "[WRITE] $path"
}

# =============================================================================
# §5 参数
# =============================================================================
usage() {
  cat <<'EOF'
scalpx-web 安装器

用法:
  bash deploy/install.sh [选项]

动作:
  (默认)                安装或升级（幂等）
      --check           只做环境与部署自检，不改动系统
      --dry-run         预演：打印将执行的命令，不做任何修改
      --upgrade         升级：保留 data/ 与 config/，强制刷新程序文件
      --rollback [DIR]  回滚到最近一次安装前的状态（或指定备份目录）
      --uninstall       卸载服务（默认保留数据目录，--purge 一并删除）

参数:
      --dir <路径>      安装目录          默认 /opt/scalpx
      --user <用户>     运行服务的系统用户  默认 scalpx
      --port <端口>     监听端口          默认 8787
      --host <地址>     监听地址          默认 0.0.0.0
      --backup-dir <路径> 备份根目录       默认 /var/backups/scalpx
      --keep <N>        保留最近 N 份备份   默认 5
      --no-service      只部署文件，不注册系统服务
      --skip-deps       跳过依赖安装（离线/已自备环境）
  -y, --yes             全部使用默认值，不交互
  -v, --verbose         输出更详细的过程日志
  -h, --help            显示本帮助

示例:
  bash deploy/install.sh --check
  bash deploy/install.sh --dry-run
  bash deploy/install.sh -y --port 9000
  bash deploy/install.sh --upgrade -y
  bash deploy/install.sh --rollback
  bash deploy/install.sh --uninstall --purge

退出码:
  0 成功  1 失败  2 参数错误  3 前置条件不满足  4 依赖安装失败
  5 服务启动失败  6 健康检查超时  7 已回滚
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --check) ACTION="check" ;;
      --dry-run) DRY_RUN=1 ;;
      --upgrade) ACTION="upgrade"; FORCE=1 ;;
      --rollback)
        ACTION="rollback"
        if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then RESTORE_FROM="$2"; shift; fi
        ;;
      --uninstall) ACTION="uninstall" ;;
      --purge) PURGE=1 ;;
      -y|--yes) ASSUME_YES=1 ;;
      -v|--verbose) VERBOSE=1 ;;
      --dir) INSTALL_DIR="${2:?--dir 需要路径}"; shift ;;
      --user) RUN_USER="${2:?--user 需要用户名}"; shift ;;
      --port) PORT="${2:?--port 需要端口}"; shift ;;
      --host) HOST="${2:?--host 需要地址}"; shift ;;
      --backup-dir) BACKUP_ROOT="${2:?--backup-dir 需要路径}"; shift ;;
      --keep) KEEP_BACKUPS="${2:?--keep 需要数字}"; shift ;;
      --no-service) NO_SERVICE=1 ;;
      --skip-deps) SKIP_DEPS=1 ;;
      -h|--help) usage; exit 0 ;;
      *) usage; die "未知参数: $1" 2 ;;
    esac
    shift
  done

  case "$PORT" in
    ''|*[!0-9]*) die "端口必须是数字：$PORT" 2 ;;
  esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围：$PORT" 2
  case "$KEEP_BACKUPS" in
    ''|*[!0-9]*) die "--keep 必须是数字" 2 ;;
  esac
  if [ -n "$RESTORE_FROM" ] && [ ! -d "$RESTORE_FROM" ]; then
    die "备份目录不存在：$RESTORE_FROM" 2
  fi
}

# =============================================================================
# §6 环境探测
# =============================================================================
detect_platform() {
  IS_ROOT=0
  [ "$(id -u)" = "0" ] && IS_ROOT=1
  if [ "$IS_ROOT" = "0" ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
    OS_PRETTY="${PRETTY_NAME:-$OS_ID}"
  elif [ "$(uname -s)" = "Darwin" ]; then
    OS_ID="macos"; OS_PRETTY="macOS $(sw_vers -productVersion 2>/dev/null || true)"
  else
    OS_ID="$(uname -s | tr '[:upper:]' '[:lower:]')"; OS_PRETTY="$OS_ID"
  fi

  if   command -v apt-get >/dev/null 2>&1; then PKG="apt"
  elif command -v dnf     >/dev/null 2>&1; then PKG="dnf"
  elif command -v yum     >/dev/null 2>&1; then PKG="yum"
  elif command -v apk     >/dev/null 2>&1; then PKG="apk"
  elif command -v pacman  >/dev/null 2>&1; then PKG="pacman"
  elif command -v zypper  >/dev/null 2>&1; then PKG="zypper"
  elif command -v brew    >/dev/null 2>&1; then PKG="brew"
  else PKG="none"; fi

  if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then INIT_SYS="systemd"
  elif command -v rc-service >/dev/null 2>&1; then INIT_SYS="openrc"
  elif [ "$OS_ID" = "macos" ]; then INIT_SYS="launchd"
  else INIT_SYS="none"; fi
}

# =============================================================================
# §7 前置检查
# =============================================================================
check_bash_version() {
  if [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
    die "需要 bash >= 4.0（当前 ${BASH_VERSION}）。Alpine 请先执行 apk add bash" 3
  fi
}

check_port_available() {
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]${PORT}$"; then
      die "端口 ${PORT} 已被占用，请用 --port 指定其它端口" 3
    fi
  elif command -v netstat >/dev/null 2>&1; then
    if netstat -ltn 2>/dev/null | awk 'NR>2 {print $4}' | grep -qE "[:.]${PORT}$"; then
      die "端口 ${PORT} 已被占用，请用 --port 指定其它端口" 3
    fi
  else
    log_warn "未找到 ss/netstat，跳过端口占用检查"
  fi
}

check_resources() {
  if [ -r /proc/meminfo ]; then
    local mem_mb
    mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    if [ "${mem_mb:-0}" -lt "$MIN_MEM_MB" ]; then
      log_warn "可用内存 ${mem_mb}MB 偏低（建议 ≥ ${MIN_MEM_MB}MB）"
    else
      log_dim "内存 ${mem_mb}MB"
    fi
  fi

  local probe_dir
  probe_dir="$(dirname "$INSTALL_DIR")"
  [ -d "$probe_dir" ] || probe_dir="/"
  local disk_mb
  disk_mb=$(df -Pm "$probe_dir" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
  if [ "${disk_mb:-0}" -gt 0 ] && [ "$disk_mb" -lt "$MIN_DISK_MB" ]; then
    log_warn "剩余磁盘 ${disk_mb}MB 偏低（建议 ≥ ${MIN_DISK_MB}MB）"
  else
    log_dim "磁盘可用 ${disk_mb}MB"
  fi
}

check_source_tree() {
  [ -f "$SRC_DIR/scalpx/web/server.py" ] || die "源码不完整：缺少 scalpx/web/server.py（请在项目根目录执行）" 3
  [ -d "$SRC_DIR/config" ] || die "源码不完整：缺少 config/ 目录" 3
  [ -d "$SRC_DIR/scalpx/web/static" ] || die "源码不完整：缺少 web/static" 3
  log_ok "源码目录校验通过：$SRC_DIR"
}

preflight() {
  step "环境检测"
  detect_platform
  check_bash_version

  log_info "系统      : ${OS_PRETTY}"
  log_info "包管理器  : ${PKG}"
  log_info "服务管理  : ${INIT_SYS}"
  log_info "架构      : $(uname -m)"
  log_info "安装目录  : ${INSTALL_DIR}"
  log_info "监听      : ${HOST}:${PORT}"
  [ "$DRY_RUN" = "1" ] && log_warn "预演模式：不会修改系统"

  if [ "$IS_ROOT" = "0" ] && [ -z "$SUDO" ] && [ "$NO_SERVICE" = "0" ]; then
    log_warn "当前非 root 且无 sudo，自动切换为 --no-service"
    NO_SERVICE=1
  fi

  [ "$INSTALL_DIR" = "/" ] && die "安装目录不能是根目录" 3
  check_source_tree
  check_resources
  check_port_available

  if [ -f "$INSTALL_DIR/scalpx/web/server.py" ]; then
    log_warn "检测到已存在的安装（将保留 data/、runs/、logs/ 与 config/ 改动）"
  fi
  step_done
}

# =============================================================================
# §8 步骤 1：依赖
# =============================================================================
python_ok() {
  local bin="$1"
  "$bin" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
}

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$(command -v "$candidate")"; then
      PY_BIN="$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}

pkg_install() {
  local pkgs="$*"
  log_info "安装系统包：$pkgs"
  _log "[PKG] $PKG install $pkgs"
  case "$PKG" in
    apt)
      run_priv "刷新软件源" env DEBIAN_FRONTEND=noninteractive apt-get update -qq || log_warn "apt-get update 失败，尝试继续"
      run_priv "安装依赖" env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs
      ;;
    dnf)    run_priv "安装依赖" dnf install -y -q $pkgs ;;
    yum)    run_priv "安装依赖" yum install -y -q $pkgs ;;
    apk)    run_priv "安装依赖" apk add --no-cache $pkgs ;;
    pacman) run_priv "安装依赖" pacman -Sy --noconfirm --needed $pkgs ;;
    zypper) run_priv "安装依赖" zypper --non-interactive install $pkgs ;;
    brew)   brew install $pkgs ;;
    *) die "未知包管理器，无法自动安装：$pkgs" 4 ;;
  esac
}

python_packages() {
  case "$PKG" in
    apt)    echo "python3 python3-venv ca-certificates" ;;
    dnf|yum) echo "python3 python3-pip ca-certificates" ;;
    apk)    echo "python3 py3-pip ca-certificates" ;;
    pacman) echo "python ca-certificates" ;;
    zypper) echo "python3 python3-pip ca-certificates" ;;
    brew)   echo "python3" ;;
    *)      echo "" ;;
  esac
}

install_deps() {
  step "依赖检测与安装"

  if find_python; then
    log_ok "Python 已满足：${PY_BIN} ($("$PY_BIN" -V 2>&1))"
  else
    if [ "$SKIP_DEPS" = "1" ]; then
      die "未找到 Python ≥ 3.${MIN_PY_MINOR}，且已指定 --skip-deps" 4
    fi
    log_warn "未找到 Python ≥ 3.${MIN_PY_MINOR}，开始自动安装"
    pkg_install "$(python_packages)"
    find_python || die "自动安装后仍无可用 Python ≥ 3.${MIN_PY_MINOR}" 4
    log_ok "Python 已安装：${PY_BIN} ($("$PY_BIN" -V 2>&1))"
  fi

  if [ "$DRY_RUN" = "0" ]; then
    "$PY_BIN" - <<'PY' || die "Python 运行环境异常（sqlite3/http.server 不可用）" 4
import http.server, hmac, sqlite3, json, ssl  # noqa: F401
print(f"      stdlib 自检通过，SQLite {sqlite3.sqlite_version}")
PY
  fi

  if [ "$NO_SERVICE" = "0" ]; then
    case "$INIT_SYS" in
      systemd|openrc) log_ok "服务管理器：${INIT_SYS}" ;;
      *)
        log_warn "未检测到 systemd/openrc，退化为 nohup 方式"
        NO_SERVICE=1
        ;;
    esac
  fi
  step_done
}

# =============================================================================
# §9 步骤 2：目录与用户
# =============================================================================
prepare_dirs() {
  step "目录与运行用户"

  INSTALL_DIR="${INSTALL_DIR%/}"
  local existed=0
  [ -d "$INSTALL_DIR" ] && existed=1

  if [ "$IS_ROOT" = "1" ] && [ "$NO_SERVICE" = "0" ]; then
    if id -u "$RUN_USER" >/dev/null 2>&1; then
      log_dim "系统用户已存在：$RUN_USER"
    else
      run_priv "创建系统用户 $RUN_USER" useradd --system --shell /usr/sbin/nologin \
        --home-dir "$INSTALL_DIR" "$RUN_USER" 2>/dev/null \
        || run_priv "创建系统用户（兼容模式）" useradd -r -s /sbin/nologin -d "$INSTALL_DIR" "$RUN_USER"
      push_rollback "删除系统用户 $RUN_USER" "userdel '$RUN_USER' 2>/dev/null || true"
    fi
  else
    RUN_USER="$(id -un)"
    log_dim "以当前用户运行：$RUN_USER"
  fi

  run_priv "创建目录结构" mkdir -p \
    "$INSTALL_DIR/data/web" "$INSTALL_DIR/runs" "$INSTALL_DIR/logs"
  if [ "$existed" = "0" ]; then
    push_rollback "删除新建的安装目录 $INSTALL_DIR" "rm -rf '$INSTALL_DIR'"
  fi

  if [ "$IS_ROOT" = "1" ] && [ "$RUN_USER" != "root" ] && [ "$DRY_RUN" = "0" ]; then
    chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR" 2>/dev/null || log_warn "chown 失败（可能已是非 root 用户）"
  fi
  if [ "$DRY_RUN" = "0" ]; then
    chmod 750 "$INSTALL_DIR/data/web" 2>/dev/null || true
  fi
  log_ok "目录就绪：$INSTALL_DIR"
  step_done
}

# =============================================================================
# §10 步骤 3：备份与同步代码
# =============================================================================
init_backup() {
  local base="$BACKUP_ROOT"
  if [ "$DRY_RUN" = "1" ]; then
    BACKUP_DIR="$base/$(date +%Y%m%d-%H%M%S)"
    log_dim "  [dry-run] 备份目录 $BACKUP_DIR"
    return 0
  fi

  if [ "$IS_ROOT" = "1" ] && [ -n "$SUDO" ]; then
    "$SUDO" mkdir -p "$base" 2>/dev/null || true
  else
    mkdir -p "$base" 2>/dev/null || true
  fi
  if [ ! -w "$base" ]; then
    base="$INSTALL_DIR/.backup"
    mkdir -p "$base" 2>/dev/null || true
    log_dim "备份根目录不可写，改用 $base"
  fi

  BACKUP_DIR="$base/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR" || die "无法创建备份目录：$BACKUP_DIR" 1
  {
    echo "app=${APP_NAME}"
    echo "version=${SCRIPT_VERSION}"
    echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
    echo "install_dir=${INSTALL_DIR}"
    echo "port=${PORT}"
    echo "python=${PY_BIN}"
  } > "$BACKUP_DIR/manifest.txt"
  _log "[BACKUP] $BACKUP_DIR"
  log_ok "备份目录：$BACKUP_DIR"
}

prune_backups() {
  [ "$DRY_RUN" = "1" ] && return 0
  local base
  base="$(dirname "$BACKUP_DIR")"
  [ -d "$base" ] || return 0
  local count
  count=$(find "$base" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | wc -l)
  if [ "$count" -gt "$KEEP_BACKUPS" ]; then
    log_dim "清理旧备份（保留 ${KEEP_BACKUPS} 份）"
    find "$base" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null \
      | sort | head -n "$((count - KEEP_BACKUPS))" | while read -r old; do
          rm -rf "$old" && _log "[PRUNE] $old"
        done
  fi
}

sync_code() {
  step "备份与同步程序文件"
  init_backup

  local had_code=0
  [ -f "$INSTALL_DIR/scalpx/web/server.py" ] && had_code=1

  if [ "$had_code" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    log_info "备份现有程序文件"
    tar -czf "$BACKUP_DIR/code.tar.gz" -C "$INSTALL_DIR" \
      --exclude=./data --exclude=./runs --exclude=./logs --exclude=./.backup . 2>/dev/null \
      || log_warn "备份失败，回滚将无法恢复旧版本"
    if [ -f "$BACKUP_DIR/code.tar.gz" ]; then
      push_rollback "恢复旧程序文件" \
        "tar -xzf '$BACKUP_DIR/code.tar.gz' -C '$INSTALL_DIR'"
      log_ok "已备份：$BACKUP_DIR/code.tar.gz"
    fi
  fi

  # 配置目录单独备份（复制代码时不会覆盖，但升级时用户可能已改）
  if [ -d "$INSTALL_DIR/config" ] && [ "$DRY_RUN" = "0" ]; then
    cp -a "$INSTALL_DIR/config" "$BACKUP_DIR/config" 2>/dev/null && \
      push_rollback "恢复配置文件" "rm -rf '$INSTALL_DIR/config'; cp -a '$BACKUP_DIR/config' '$INSTALL_DIR/config'"
  fi

  # config/ 必须排除：用户可能在后台改过参数，同步代码时不能覆盖。
  # 缺失的配置由 §11 ensure_configs 按文件逐个补齐。
  local excludes=(--exclude=data --exclude=runs --exclude=logs --exclude=config
                  --exclude=.git --exclude=.venv --exclude=__pycache__
                  --exclude='*.pyc' --exclude=.backup)
  if command -v rsync >/dev/null 2>&1; then
    run_priv "同步程序文件（rsync）" rsync -a "${excludes[@]}" "$SRC_DIR/" "$INSTALL_DIR/"
  else
    log_dim "未找到 rsync，使用 tar 管道"
    local tar_excludes=""
    local e
    for e in data runs logs config .git .venv __pycache__ .backup; do
      tar_excludes="$tar_excludes --exclude=./$e"
    done
    run_sh "同步程序文件（tar）" \
      "cd '$SRC_DIR' && tar -cf - $tar_excludes --exclude='*.pyc' . | tar -xf - -C '$INSTALL_DIR'"
  fi

  if [ "$DRY_RUN" = "0" ]; then
    [ -f "$INSTALL_DIR/scalpx/web/server.py" ] || die "同步后仍缺少 server.py，源码可能被排除规则误伤" 1
    [ -f "$INSTALL_DIR/scalpx/web/static/index.html" ] || die "同步后缺少前端静态资源" 1
  fi

  if [ "$IS_ROOT" = "1" ] && [ "$RUN_USER" != "root" ] && [ "$DRY_RUN" = "0" ]; then
    chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true
  fi
  log_ok "程序文件已同步"
  step_done
}

# =============================================================================
# §11 步骤 4：配置与账号
# =============================================================================
ensure_secret() {
  local secret_file="$INSTALL_DIR/data/web/secret.key"
  if [ -f "$secret_file" ]; then
    log_dim "会话密钥已存在，保持不变（重置会导致所有会话失效）"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    log_dim "  [dry-run] 生成 $secret_file"
    return 0
  fi
  "$PY_BIN" - "$secret_file" <<'PY'
import base64, os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "wb") as fh:
    fh.write(base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"="))
os.chmod(path, 0o600)
PY
  push_rollback "删除新建的会话密钥" "rm -f '$secret_file'"
  log_ok "会话密钥已生成"
}

ensure_configs() {
  # config/ 被同步步骤排除，这里必须显式创建目录
  run_priv "创建配置目录" mkdir -p "$INSTALL_DIR/config"
  local copied=0
  local f
  for f in "$SRC_DIR"/config/*.toml; do
    [ -f "$f" ] || continue
    local target="$INSTALL_DIR/config/$(basename "$f")"
    if [ -f "$target" ]; then
      log_dim "保留已存在的配置：$(basename "$f")"
    else
      run_priv "复制配置 $(basename "$f")" cp -f "$f" "$target"
      copied=$((copied + 1))
    fi
  done
  [ "$copied" -gt 0 ] && log_ok "已复制 ${copied} 个配置文件"
  return 0
}

ensure_admin() {
  local cred_file="$INSTALL_DIR/data/web/initial_credentials.txt"
  local count
  count=$("$PY_BIN" - "$INSTALL_DIR" <<'PY'
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
  if [ "${count:-0}" -gt 0 ]; then
    log_dim "已存在 ${count} 个账号，跳过初始化"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    log_dim "  [dry-run] 创建管理员账号并写入 $cred_file"
    return 0
  fi

  local admin_pw
  admin_pw=$("$PY_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(12))
PY
)
  ( cd "$INSTALL_DIR" && "$PY_BIN" -m scalpx.web create-user \
      --username admin --password "$admin_pw" --role admin >/dev/null )
  push_rollback "移除初始管理员账号" \
    "cd '$INSTALL_DIR' && '$PY_BIN' -c \"from scalpx.web.repo import WebRepo; r=WebRepo('data/web.db'); r._conn.execute('DELETE FROM users WHERE username=?',('admin',)); r._conn.commit(); r.close()\" 2>/dev/null || true"

  umask 077
  {
    echo "scalpx-web 初始管理员凭据（请登录后立即修改）"
    echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "用户名  : admin"
    echo "口令    : ${admin_pw}"
    echo "登录地址: http://<服务器IP>:${PORT}/admin"
  } > "$cred_file"
  umask 022
  chmod 600 "$cred_file" 2>/dev/null || true
  log_ok "管理员账号已创建，凭据：$cred_file"
}

configure() {
  step "生成配置与账号"
  ensure_secret
  ensure_configs
  ensure_admin
  step_done
}

# =============================================================================
# §12 步骤 5：服务注册
# =============================================================================
unit_path() { echo "/etc/systemd/system/${SERVICE_NAME}.service"; }
openrc_path() { echo "/etc/init.d/${SERVICE_NAME}"; }

install_systemd_unit() {
  local unit
  unit="$(unit_path)"
  if [ -f "$unit" ] && [ "$DRY_RUN" = "0" ]; then
    cp -a "$unit" "$BACKUP_DIR/unit.bak" 2>/dev/null || true
    push_rollback "恢复旧的 systemd unit" "cp -a '$BACKUP_DIR/unit.bak' '$unit' 2>/dev/null || rm -f '$unit'"
  else
    push_rollback "删除 systemd unit" "rm -f '$unit'"
  fi

  write_file_priv "$unit" <<EOF
[Unit]
Description=scalpx web console (quant trading control panel)
Documentation=https://github.com/qiuyuec-qyacgcom/bn-scalper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=${PY_BIN} -m scalpx.web serve --root ${INSTALL_DIR} --host ${HOST} --port ${PORT}
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=3
TimeoutStopSec=20
KillSignal=SIGTERM
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
  run_priv "重载 systemd 配置" systemctl daemon-reload
  run_priv "设置开机自启" systemctl enable "$SERVICE_NAME"
  log_ok "systemd 服务已注册：$unit"
}

install_openrc_script() {
  local svc
  svc="$(openrc_path)"
  push_rollback "删除 OpenRC 脚本" "rm -f '$svc'"
  write_file_priv "$svc" <<EOF
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
  run_priv "赋予执行权限" chmod +x "$svc"
  run_priv "加入默认运行级" rc-update add "$SERVICE_NAME" default
  log_ok "OpenRC 服务已注册：$svc"
}

install_logrotate() {
  command -v logrotate >/dev/null 2>&1 || { log_dim "未安装 logrotate，跳过日志轮转"; return 0; }
  write_file_priv "/etc/logrotate.d/${SERVICE_NAME}" <<EOF
${INSTALL_DIR}/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
  log_ok "日志轮转已配置（保留 14 天）"
}

register_service() {
  step "注册系统服务"

  if [ "$NO_SERVICE" = "1" ]; then
    log_warn "跳过服务注册（--no-service）"
    log_dim "手动启动：cd ${INSTALL_DIR} && ${PY_BIN} -m scalpx.web serve --host ${HOST} --port ${PORT}"
    step_done
    return 0
  fi

  case "$INIT_SYS" in
    systemd) install_systemd_unit ;;
    openrc)  install_openrc_script ;;
    *)       log_warn "无可用服务管理器" ;;
  esac
  install_logrotate
  step_done
}

# =============================================================================
# §13 步骤 6：启动与健康校验
# =============================================================================
stop_nohup() {
  local pid_file="$1"
  [ -f "$pid_file" ] || return 0
  local pid
  pid=$(cat "$pid_file" 2>/dev/null || true)
  [ -n "${pid:-}" ] || { rm -f "$pid_file"; return 0; }
  if kill -0 "$pid" 2>/dev/null; then
    log_info "停止既有进程 $pid"
    kill "$pid" 2>/dev/null || true
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 10 ]; do
      sleep 1
      i=$((i + 1))
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  _log "[STOP] pid=$pid"
  # 进程退出后句柄释放有延迟（Windows 尤其明显），留一点缓冲
  sleep 1
}

# 删除目录：占用时重试，避免"卸载成功但目录还在"
remove_dir_retry() {
  local dir="$1"
  local attempt=1
  while [ "$attempt" -le 3 ]; do
    if rm -rf "$dir" 2>/dev/null && [ ! -d "$dir" ]; then
      return 0
    fi
    if [ "$attempt" -lt 3 ]; then
      log_dim "目录仍被占用，2 秒后重试（${attempt}/3）"
      sleep 2
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

start_service() {
  step "启动服务"

  if [ "$NO_SERVICE" = "1" ]; then
    if [ "$DRY_RUN" = "0" ]; then
      local pid_file="$INSTALL_DIR/logs/${SERVICE_NAME}.pid"
      stop_nohup "$pid_file"

      # 直接在当前 shell 里后台启动，$! 才是真正的 python 进程号
      # （放进子 shell 会把 $! 记成子 shell，后续 stop 时杀不掉）
      local old_pwd="$PWD"
      cd "$INSTALL_DIR" || die "无法进入安装目录" 1
      nohup "$PY_BIN" -m scalpx.web serve --root "$INSTALL_DIR" \
        --host "$HOST" --port "$PORT" >> "$INSTALL_DIR/logs/web.log" 2>&1 &
      local pid=$!
      echo "$pid" > "$pid_file"
      cd "$old_pwd" || true

      push_rollback "停止 nohup 进程 $pid" "kill $pid 2>/dev/null || true"
      log_ok "已以 nohup 启动（pid $pid）"
    fi
    step_done
    return 0
  fi

  case "$INIT_SYS" in
    systemd)
      run_priv "停止既有服务" systemctl stop "$SERVICE_NAME" || true
      run_priv "启动服务" systemctl start "$SERVICE_NAME" || die "systemd 启动失败" 5
      ;;
    openrc)
      run_priv "重启服务" rc-service "$SERVICE_NAME" restart || die "OpenRC 启动失败" 5
      ;;
  esac
  log_ok "服务已启动"
  step_done
}

health_check() {
  step "健康校验"
  local url="http://127.0.0.1:${PORT}/api/health"
  local i=1
  log_info "等待服务就绪（最多 ${HEALTH_RETRIES}s）…"

  if [ "$DRY_RUN" = "1" ]; then
    log_dim "  [dry-run] 轮询 $url"
    step_done
    return 0
  fi

  while [ "$i" -le "$HEALTH_RETRIES" ]; do
    if "$PY_BIN" - "$url" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=2) as resp:
    payload = json.loads(resp.read().decode())
assert payload.get("ok") is True
data = payload["data"]
assert data.get("status") == "ok", data.get("status")
checks = data.get("checks") or {}
assert checks.get("web_db") and checks.get("static") and checks.get("config"), checks
PY
    then
      log_ok "健康检查通过：${url}"
      break
    fi
    sleep 1
    i=$((i + 1))
    # 每 5 秒打一个点，避免自动化环境误以为卡死
    if [ $((i % 5)) -eq 0 ]; then printf '%s.%s' "$C_GRAY" "$C_RESET"; fi
    if [ "$i" -gt "$HEALTH_RETRIES" ]; then
      log_err "健康检查超时"
      if [ "$INIT_SYS" = "systemd" ]; then
        systemctl --no-pager status "$SERVICE_NAME" 2>&1 | head -n 15 | sed 's/^/    /' || true
      fi
      [ -f "$INSTALL_DIR/logs/web.log" ] && tail -n 20 "$INSTALL_DIR/logs/web.log" | sed 's/^/    /' || true
      die "服务未能在 ${HEALTH_RETRIES}s 内就绪" 6
    fi
  done

  log_info "部署自检："
  ( cd "$INSTALL_DIR" && "$PY_BIN" -m scalpx.web check --root "$INSTALL_DIR" ) \
    | sed 's/^/    /' || log_warn "自检有告警（不影响启动）"
  step_done
}

# =============================================================================
# §14 收尾
# =============================================================================
prune_backups_after_success() {
  [ -n "$BACKUP_DIR" ] || return 0
  prune_backups
}

show_summary() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  [ -z "$ip" ] && ip="127.0.0.1"
  local elapsed=$(( $(date +%s) - START_TS ))
  local cred="$INSTALL_DIR/data/web/initial_credentials.txt"

  printf '\n%s══════════════════════════════════════════════════════════════%s\n' "$C_GREEN" "$C_RESET"
  printf '%s  %s v%s 完成%s   用时 %s 秒\n' \
    "$C_GREEN$C_BOLD" "$APP_NAME" "$SCRIPT_VERSION" "$C_RESET" "$elapsed"
  printf '%s══════════════════════════════════════════════════════════════%s\n' "$C_GREEN" "$C_RESET"
  printf '  安装目录 : %s\n' "$INSTALL_DIR"
  printf '  运行用户 : %s\n' "$RUN_USER"
  printf '  监听地址 : %s:%s\n' "$HOST" "$PORT"
  printf '  服务管理 : %s\n' "$([ "$NO_SERVICE" = "1" ] && echo "nohup（未注册）" || echo "$INIT_SYS")"
  printf '  备份目录 : %s\n' "${BACKUP_DIR:-—}"
  printf '\n  前台入口 : %shttp://%s:%s/%s\n' "$C_BLUE" "$ip" "$PORT" "$C_RESET"
  printf '  后台入口 : %shttp://%s:%s/admin%s\n' "$C_BLUE" "$ip" "$PORT" "$C_RESET"

  if [ "$DRY_RUN" = "1" ]; then
    printf '\n  %s预演模式%s：以上均为计划动作，未对系统做任何修改。\n' "$C_YELLOW" "$C_RESET"
  elif [ -f "$cred" ]; then
    printf '\n  %s初始管理员凭据%s（登录后请立即修改）\n' "$C_YELLOW" "$C_RESET"
    sed 's/^/    /' "$cred"
  else
    printf '\n  管理员账号已存在，未重置。忘记口令：\n'
    printf '    cd %s && %s -m scalpx.web reset-password --username admin\n' "$INSTALL_DIR" "$PY_BIN"
  fi

  printf '\n  %s常用命令%s\n' "$C_BOLD" "$C_RESET"
  if [ "$INIT_SYS" = "systemd" ] && [ "$NO_SERVICE" = "0" ]; then
    printf '    systemctl status %s\n' "$SERVICE_NAME"
    printf '    systemctl restart %s\n' "$SERVICE_NAME"
    printf '    journalctl -u %s -f\n' "$SERVICE_NAME"
  fi
  printf '    健康探测 : bash %s/healthcheck.sh --port %s\n' "$SCRIPT_DIR" "$PORT"
  printf '    升级     : bash %s/install.sh --upgrade -y\n' "$SCRIPT_DIR"
  printf '    回滚     : bash %s/install.sh --rollback\n' "$SCRIPT_DIR"
  printf '\n  %s日志%s %s\n\n' "$C_GRAY" "$C_RESET" "$LOG_FILE"
}

# =============================================================================
# §15 卸载
# =============================================================================
do_uninstall() {
  detect_platform
  step "卸载"
  log_info "目标目录：$INSTALL_DIR"

  if [ "$INIT_SYS" = "systemd" ]; then
    run_priv "停止服务" systemctl stop "$SERVICE_NAME" || true
    run_priv "取消开机自启" systemctl disable "$SERVICE_NAME" || true
    run_priv "删除 unit 文件" rm -f "$(unit_path)"
    run_priv "重载 systemd" systemctl daemon-reload || true
  elif [ "$INIT_SYS" = "openrc" ]; then
    run_priv "停止服务" rc-service "$SERVICE_NAME" stop || true
    run_priv "移除运行级" rc-update del "$SERVICE_NAME" default || true
    run_priv "删除 init 脚本" rm -f "$(openrc_path)"
  else
    stop_nohup "$INSTALL_DIR/logs/${SERVICE_NAME}.pid"
  fi
  run_priv "删除日志轮转配置" rm -f "/etc/logrotate.d/${SERVICE_NAME}"
  log_ok "服务已移除"

  # 只有显式 --purge 才删数据；-y 仅表示"不再交互"，默认保留数据
  local remove=0
  if [ "$PURGE" = "1" ]; then
    remove=1
  elif [ "$ASSUME_YES" = "1" ]; then
    remove=0
  else
    printf '  是否删除数据目录 %s ？[y/N] ' "$INSTALL_DIR"
    read -r ans || true
    case "${ans:-N}" in y|Y|yes|YES) remove=1 ;; *) remove=0 ;; esac
  fi

  if [ "$remove" = "1" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      log_dim "  [dry-run] rm -rf $INSTALL_DIR"
    elif remove_dir_retry "$INSTALL_DIR"; then
      log_ok "已删除 $INSTALL_DIR"
    else
      log_warn "目录删除失败（可能有进程占用），请手动执行：rm -rf $INSTALL_DIR"
    fi
  else
    log_warn "已保留数据目录：$INSTALL_DIR（可用 --purge 一并删除）"
  fi

  if [ "$IS_ROOT" = "1" ] && id -u "$RUN_USER" >/dev/null 2>&1; then
    if [ "$remove" = "1" ] && [ "$ASSUME_YES" = "1" ]; then
      run_priv "删除系统用户 $RUN_USER" userdel "$RUN_USER" 2>/dev/null || true
    fi
  fi
  printf '\n%s卸载完成%s\n\n' "$C_GREEN" "$C_RESET"
}

# =============================================================================
# §16 回滚（从备份恢复）
# =============================================================================
do_rollback() {
  NO_ROLLBACK=1
  detect_platform
  step "回滚"

  local backup="$RESTORE_FROM"
  if [ -z "$backup" ]; then
    local base="$BACKUP_ROOT"
    [ -d "$base" ] || base="$INSTALL_DIR/.backup"
    backup=$(find "$base" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | sort | tail -n 1 || true)
  fi
  [ -n "$backup" ] && [ -d "$backup" ] || die "未找到可用的备份目录" 3
  log_info "使用备份：$backup"
  [ -f "$backup/manifest.txt" ] && sed 's/^/    /' "$backup/manifest.txt" || true

  if [ "$INIT_SYS" = "systemd" ]; then
    run_priv "停止服务" systemctl stop "$SERVICE_NAME" || true
  fi

  if [ -f "$backup/code.tar.gz" ]; then
    run_priv "恢复程序文件" tar -xzf "$backup/code.tar.gz" -C "$INSTALL_DIR"
    log_ok "程序文件已恢复"
  else
    log_warn "备份中没有 code.tar.gz（首次安装时不会有旧版本）"
  fi

  if [ -d "$backup/config" ]; then
    run_priv "恢复配置文件" bash -c "rm -rf '$INSTALL_DIR/config' && cp -a '$backup/config' '$INSTALL_DIR/config'"
    log_ok "配置已恢复"
  fi

  if [ -f "$backup/unit.bak" ]; then
    run_priv "恢复 systemd unit" cp -a "$backup/unit.bak" "$(unit_path)"
    run_priv "重载 systemd" systemctl daemon-reload || true
  fi

  if [ "$NO_SERVICE" = "0" ] && [ "$INIT_SYS" = "systemd" ]; then
    run_priv "重启服务" systemctl restart "$SERVICE_NAME" || log_warn "重启失败，请手动检查"
  fi
  printf '\n%s回滚完成%s\n\n' "$C_GREEN" "$C_RESET"
}

# =============================================================================
# §17 自检模式
# =============================================================================
do_check() {
  detect_platform
  find_python || die "未找到 Python ≥ 3.${MIN_PY_MINOR}" 3
  step "环境与部署自检"
  log_info "系统      : ${OS_PRETTY}"
  log_info "包管理器  : ${PKG}"
  log_info "服务管理  : ${INIT_SYS}"
  log_info "Python    : ${PY_BIN}"
  check_port_available

  if [ ! -d "$INSTALL_DIR" ]; then
    log_warn "安装目录不存在：$INSTALL_DIR（尚未安装）"
    return 3
  fi
  local output rc=0
  output=$( cd "$INSTALL_DIR" && "$PY_BIN" -m scalpx.web check --root "$INSTALL_DIR" 2>&1 ) || rc=$?
  printf '%s\n' "$output" | sed 's/^/  /'
  step_done
  return "$rc"
}

# =============================================================================
# §18 主流程
# =============================================================================
banner() {
  printf '\n%s' "$C_BLUE"
  cat <<'EOF'
   ____   ____    _    _     ____  __  __    __        _____ _____
  / ___| / ___|  / \  | |   |  _ \ \ \/ /    \ \      / / __| __|
  \___ \| |     / _ \ | |   | |_) | \  /      \ \ /\ / /| _|| |_
   ___) | |___ / ___ \| |___|  __/  /  \       \ V  V / | |__| _|
  |____/ \____/_/   \_\_____|_|    /_/\_\       \_/\_/  |___|_|
EOF
  printf '%s  scalpx 量化交易控制台 · 安装器 v%s%s\n' "$C_GRAY" "$SCRIPT_VERSION" "$C_RESET"
}

main() {
  parse_args "$@"
  init_log
  banner
  printf '%s  模式: %s%s\n' "$C_GRAY" "$ACTION$([ "$DRY_RUN" = "1" ] && echo ' (dry-run)')" "$C_RESET"
  printf '%s  日志: %s%s\n' "$C_GRAY" "$LOG_FILE" "$C_RESET"

  case "$ACTION" in
    uninstall) do_uninstall; exit 0 ;;
    rollback)  do_rollback;  exit 0 ;;
    check)
      local check_rc=0
      do_check || check_rc=$?
      exit "$check_rc"
      ;;
  esac

  detect_platform
  preflight
  install_deps
  prepare_dirs
  sync_code
  configure
  register_service
  start_service
  health_check

  prune_backups_after_success
  show_summary
  printf '%s安装流程结束（退出码 0）%s\n\n' "$C_GREEN" "$C_RESET"
}

main "$@"
