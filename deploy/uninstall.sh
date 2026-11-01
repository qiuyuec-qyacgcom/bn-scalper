#!/usr/bin/env bash
# =============================================================================
#  scalpx-web 卸载脚本（install.sh --uninstall 的薄封装）
#
#  行为：
#    1) 停止并禁用系统服务
#    2) 删除 service 文件
#    3) 询问是否删除安装目录（默认保留数据）
#
#  用法：
#    bash deploy/uninstall.sh              # 交互确认
#    bash deploy/uninstall.sh -y           # 全自动（会删除数据目录，谨慎）
#    bash deploy/uninstall.sh --keep-data  # 全自动但保留数据
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ARGS=("--uninstall")
for arg in "$@"; do
  case "$arg" in
    --keep-data) ;;                 # 由 install.sh 的交互分支处理，这里不透传
    --dir=*) ARGS+=("--dir" "${arg#--dir=}") ;;
    -h|--help)
      echo "用法: bash deploy/uninstall.sh [-y] [--dir=路径]"
      exit 0 ;;
    *) ARGS+=("$arg") ;;
  esac
done

exec bash "${SCRIPT_DIR}/install.sh" "${ARGS[@]}"
