#!/usr/bin/env bash
# =============================================================================
#  把本地仓库发布到 GitHub：建仓 → 关联 remote → 推送
#
#  需要一个有 repo 权限的 GitHub Token（classic: repo 作用域；fine-grained: Contents + Administration 写）
#
#  用法：
#    GITHUB_TOKEN=ghp_xxx bash scripts/publish.sh
#    GITHUB_TOKEN=ghp_xxx bash scripts/publish.sh --name my-scalpx --private
#    bash scripts/publish.sh --token ghp_xxx --org my-org
#
#  幂等：仓库已存在则跳过创建，直接推送（可用于后续更新）。
# =============================================================================
set -Eeuo pipefail

REPO_NAME="scalpx"
VISIBILITY="public"
BRANCH="main"
OWNER=""
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
DESC="Binance USDⓈ-M perpetual ultra-short-term trading system: event-driven, backtest/live parity, zero core dependencies, built-in web console"

die() { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[32m✔ %s\033[0m\n' "$*"; }
info(){ printf '  %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --name) REPO_NAME="${2:?}"; shift ;;
    --private) VISIBILITY="private" ;;
    --public) VISIBILITY="public" ;;
    --branch) BRANCH="${2:?}"; shift ;;
    --org) OWNER="${2:?}"; shift ;;
    --token) TOKEN="${2:?}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
  shift
done

cd "$(dirname "${BASH_SOURCE[0]}")/.."

command -v git >/dev/null 2>&1 || die "未安装 git"
[ -n "$TOKEN" ] || die "缺少 Token：请设置 GITHUB_TOKEN 环境变量，或使用 --token"
[ -d .git ] || die "当前目录不是 git 仓库，请先执行：git init && git add . && git commit -m 'init'"
git rev-parse HEAD >/dev/null 2>&1 || die "还没有任何提交，请先 commit"

# 用 Python 做 HTTP，避免依赖 curl/jq
PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || die "需要 python3 来调用 GitHub API"

info "校验 Token…"
API_USER="$("$PY_BIN" - "$TOKEN" <<'PY'
import json, sys, urllib.request
token = sys.argv[1]
req = urllib.request.Request("https://api.github.com/user", headers={
    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
    "User-Agent": "scalpx-publish",
})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(json.loads(resp.read().decode())["login"])
except Exception as exc:
    print("ERROR:" + str(exc))
PY
)"
case "$API_USER" in
  ERROR:*) die "Token 无效或网络不可达（${API_USER#ERROR:}）" ;;
esac
ok "已认证为 $API_USER"
[ -n "$OWNER" ] || OWNER="$API_USER"

info "创建仓库 ${OWNER}/${REPO_NAME}（${VISIBILITY}）…"
CREATE_RESULT="$("$PY_BIN" - "$TOKEN" "$OWNER" "$REPO_NAME" "$VISIBILITY" "$DESC" <<'PY'
import json, sys, urllib.error, urllib.request

token, owner, name, visibility, desc = sys.argv[1:6]
me = None
req = urllib.request.Request(
    "https://api.github.com/user",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
             "User-Agent": "scalpx-publish"},
)
with urllib.request.urlopen(req, timeout=15) as resp:
    me = json.loads(resp.read().decode())["login"]

url = "https://api.github.com/user/repos" if owner == me else f"https://api.github.com/orgs/{owner}/repos"
payload = json.dumps({
    "name": name, "description": desc, "private": visibility == "private",
    "auto_init": False, "has_issues": True, "has_wiki": False,
}).encode()
req = urllib.request.Request(url, data=payload, method="POST", headers={
    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
    "Content-Type": "application/json", "User-Agent": "scalpx-publish",
})
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
        print("CREATED " + data["html_url"].replace("https://github.com/", "git@github.com:") + ".git")
except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    if exc.code == 422 and "already exists" in body:
        print(f"EXISTS {owner}/{name}")
    else:
        print("ERROR " + body[:300])
PY
)"

case "$CREATE_RESULT" in
  CREATED*) ok "仓库已创建" ;;
  EXISTS*)  ok "仓库已存在，继续推送" ;;
  *) die "创建失败: ${CREATE_RESULT#ERROR }" ;;
esac

REMOTE_URL="https://github.com/${OWNER}/${REPO_NAME}.git"
AUTH_URL="https://x-access-token:${TOKEN}@github.com/${OWNER}/${REPO_NAME}.git"

info "配置 remote origin…"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi

info "推送到 ${BRANCH}…"
# 用一次性带 token 的 URL 推送；成功后 remote 保持干净（不含 token）
git -c "url.${AUTH_URL}.insteadOf=${REMOTE_URL}" push -u origin "$BRANCH"
ok "推送完成"

printf '\n\033[32m仓库地址\033[0m  %s\n' "$REMOTE_URL"
printf '\033[32m页面地址\033[0m  https://github.com/%s/%s\n\n' "$OWNER" "$REPO_NAME"
