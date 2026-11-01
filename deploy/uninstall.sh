#!/usr/bin/env bash
# =============================================================================
#  scalpx-web 卸载脚本（install.sh --uninstall 的薄封装）
#
#  行为：
#    1) 停止并禁用系统服务（systemd / OpenRC / nohup）
#    2) 删除 unit、init 脚本与日志轮转配置
#    3) 询问是否删除安装目录 —— **默认保留数据**
#
#  用法：
#    bash deploy/uninstall.sh                # 交互确认，保留数据
#    bash deploy/uninstall.sh -y             # 不交互，仍保留数据
#    bash deploy/uninstall.sh --purge -y     # 连数据目录一起删除（不可恢复）
#    bash deploy/uninstall.sh --dir /srv/scalpx
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ARGS=(--uninstall)
for arg in "$@"; do
  case "$arg" in
    --keep-data) ;;                 # 保留数据是默认行为，无需透传
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *) ARGS+=("$arg") ;;
  esac
done

exec bash "${SCRIPT_DIR}/install.sh" "${ARGS[@]}"
