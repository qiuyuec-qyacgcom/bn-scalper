#!/usr/bin/env bash
# =============================================================================
#  scalpx-web 外部健康探针（给 cron / systemd timer / 监控系统用）
#
#  与安装器解耦：只看 HTTP，不关心进程怎么起的。
#  退出码：0 健康 / 1 不健康 / 2 参数错误
#
#  用法：
#    bash deploy/healthcheck.sh                     # 默认 127.0.0.1:8787
#    bash deploy/healthcheck.sh --port 9000
#    bash deploy/healthcheck.sh --url http://10.0.0.5:8787 --timeout 5 --quiet
# =============================================================================
set -Eeuo pipefail

HOST="127.0.0.1"
PORT="8787"
URL=""
TIMEOUT="5"
QUIET=0

die() { printf '参数错误：%s\n' "$1" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:?--url 需要地址}"; shift ;;
    --host) HOST="${2:?--host 需要地址}"; shift ;;
    --port) PORT="${2:?--port 需要端口}"; shift ;;
    --timeout) TIMEOUT="${2:?--timeout 需要秒数}"; shift ;;
    --quiet) QUIET=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) die "未知参数 $1" ;;
  esac
  shift
done

[ -n "$URL" ] || URL="http://${HOST}:${PORT}/api/health"

PY_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY_BIN="$candidate"; break; fi
done
[ -n "$PY_BIN" ] || { echo "需要 python3"; exit 1; }

"$PY_BIN" - "$URL" "$TIMEOUT" "$QUIET" <<'PY'
import json, sys, urllib.error, urllib.request

url, timeout, quiet = sys.argv[1], float(sys.argv[2]), sys.argv[3] == "1"

def say(msg):
    if not quiet:
        print(msg)

try:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
except urllib.error.URLError as exc:
    print(f"UNHEALTHY 连接失败: {exc}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    print(f"UNHEALTHY 响应异常: {exc}", file=sys.stderr)
    sys.exit(1)

if not payload.get("ok"):
    print(f"UNHEALTHY 信封异常: {payload}", file=sys.stderr)
    sys.exit(1)

data = payload["data"]
checks = data.get("checks") or {}
status = data.get("status")

say(f"status={status} version={data.get('version')} uptime={data.get('uptime_s')}s")
for name, value in checks.items():
    say(f"  {name:<12}: {'ok' if value else 'FAIL'}")

if status != "ok":
    print(f"UNHEALTHY 状态为 {status}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PY
