#!/usr/bin/env bash
# =============================================================================
#  安装器内部逻辑单测（不需要 root、不需要 systemd）
#
#  安装器最难的几段是"切身份执行"和"属主回收"——线上出过的
#  PermissionError: secret.key 就是这两处没覆盖。这里用函数级断言把它们钉住。
#
#  用法：bash tests/test_installer.sh
#  退出码：0 全通过 / 1 有失败
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$HERE/../deploy/install.sh"

[ -f "$INSTALLER" ] || { echo "找不到安装器：$INSTALLER" >&2; exit 1; }

# shellcheck disable=SC1090
source "$INSTALLER"
set +e
trap - ERR
LOG_FILE=""

PASS=0
FAIL=0

check() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf '  ok   %s\n' "$desc"
    PASS=$((PASS + 1))
  else
    printf '  FAIL %s\n       期望: %s\n       实际: %s\n' "$desc" "$expected" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

echo "[as_run_user]"
CURRENT="$(id -un)"
RUN_USER="$CURRENT"
check "同用户时直接执行，不切换身份" "direct" "$(as_run_user echo direct)"

# 只有 root 安装才需要切身份（非 root 时 SUDO 路径已覆盖权限）
IS_ROOT=1
runuser() { shift 3; echo "switched:$*"; }
RUN_USER="scalpx"
if [ "$CURRENT" != "scalpx" ]; then
  check "root 安装时经 runuser 切到服务用户" "switched:echo hi" "$(as_run_user echo hi)"
else
  echo "  skip 当前用户就是 scalpx，跳过切换用例"
fi
unset -f runuser
IS_ROOT=0

echo "[fix_ownership]"
IS_ROOT=0
RUN_USER="scalpx"
fix_ownership
check "非 root 安装时不做 chown" "0" "$?"

IS_ROOT=1
DRY_RUN=1
fix_ownership
check "预演模式不做 chown" "0" "$?"

IS_ROOT=1
DRY_RUN=0
RUN_USER="root"
fix_ownership
check "运行用户是 root 时跳过" "0" "$?"

echo "[parse_args]"
IS_ROOT=0
DRY_RUN=0
( parse_args --port abc ) >/dev/null 2>&1
check "非法端口退出码 2" "2" "$?"

( parse_args --port 70000 ) >/dev/null 2>&1
check "端口越界退出码 2" "2" "$?"

( parse_args --unknown-flag ) >/dev/null 2>&1
check "未知参数退出码 2" "2" "$?"

(
  parse_args --port 9100 --dir /tmp/x --dry-run --no-service -y
  echo "$PORT|$INSTALL_DIR|$DRY_RUN|$NO_SERVICE|$ASSUME_YES"
) > /tmp/scalpx-parse.out 2>/dev/null
check "合法参数被正确解析" "9100|/tmp/x|1|1|1" "$(cat /tmp/scalpx-parse.out)"
rm -f /tmp/scalpx-parse.out

echo "[rollback stack]"
ROLLBACK_DESC=()
ROLLBACK_CMD=()
push_rollback "动作A" "echo A"
push_rollback "动作B" "echo B"
check "回滚栈记录两条" "2" "${#ROLLBACK_CMD[@]}"
check "按登记顺序入栈（执行时反向遍历）" "动作B" "${ROLLBACK_DESC[1]}"

echo
if [ "$FAIL" -gt 0 ]; then
  printf '安装器单测失败：%d 通过 / %d 失败\n' "$PASS" "$FAIL"
  exit 1
fi
printf '安装器单测通过：%d 项\n' "$PASS"
