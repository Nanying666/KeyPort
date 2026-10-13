#!/usr/bin/env bash
# login.sh — KeyPort OAuth 设备授权登录 → 落盘 auths/workbuddy-<uid>.json
#
# 用法:
#   ./login.sh                  # 交互式选域（默认 1/国内版 cn）
#   ./login.sh --realm=global   # 直达国际版
#   ./login.sh --realm=cn       # 直达国内版
#
# 流程（全程无 PKCE，state 由服务端签发）：
#   1. 选域
#   2. POST {base}/v2/plugin/auth/state?platform=CLI 取授权 URL
#   3. 你在浏览器完成登录
#   4. 回车后轮询 token → 拉取账号信息 → 落盘 auths/workbuddy-<uid>.json
#   5. 重启服务（或管理台点「扫描 auths/」）加载新账号
#
# 依赖：python3（标准库即可，无需第三方包）
set -euo pipefail

cd "$(dirname "$0")"
AUTH_DIR="${KP_AUTH_DIR:-./auths}"
CONTAINER="${KP_CONTAINER:-keyport}"
PORT="${KP_PORT:-8790}"

mkdir -p "$AUTH_DIR"

REALM="cn"
if [[ $# -gt 0 && "$1" == --realm=* ]]; then
    REALM="${1#--realm=}"
fi
if [[ -z "$REALM" ]]; then
    REALM="cn"
fi

echo "============================================================"
echo "  KeyPort OAuth 登录（域: $REALM）"
echo "============================================================"

AUTH_URL=$(python3 - "$REALM" <<'PYEOF'
import json
import sys
import urllib.request

realm = sys.argv[1] if len(sys.argv) > 1 else "cn"
BASES = {
    "cn": ("https://copilot.tencent.com", "https://www.codebuddy.cn"),
    "global": ("https://www.workbuddy.ai", "https://www.workbuddy.ai"),
}
base, origin = BASES.get(realm, BASES["cn"])
req = urllib.request.Request(
    base + "/v2/plugin/auth/state?platform=CLI",
    method="POST",
    data=b"{}",
    headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
    },
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode() or "{}")
except Exception as exc:  # noqa: BLE001
    print(f"ERROR:{exc}", file=sys.stderr)
    sys.exit(1)

state = data.get("state", "")
auth_url = data.get("authUrl") or data.get("auth_url") or ""
if not state or not auth_url:
    print("ERROR:state 端点未返回 state/authUrl", file=sys.stderr)
    sys.exit(1)

# state 落盘（0600），供 poll 阶段读回
import os
import tempfile

state_file = os.path.join(tempfile.gettempdir(), "keyport-login-state.json")
with open(state_file, "w", encoding="utf-8") as fh:
    json.dump({"state": state, "realm": realm}, fh)
print(auth_url)
PYEOF
) || { echo "❌ 获取授权链接失败"; exit 1; }

echo ""
echo "请在浏览器中打开以下链接完成登录："
echo ""
echo "  $AUTH_URL"
echo ""

if command -v xclip >/dev/null 2>&1; then
    echo -n "$AUTH_URL" | xclip -selection clipboard 2>/dev/null && echo "(已复制到剪贴板)"
elif command -v pbcopy >/dev/null 2>&1; then
    echo -n "$AUTH_URL" | pbcopy && echo "(已复制到剪贴板)"
fi

read -rp "完成登录后按回车继续: " _ans

echo ""
echo "正在获取 token..."

RESULT=$(python3 - "$REALM" "$AUTH_DIR" <<'PYEOF'
import json
import os
import sys
import time
import urllib.error
import urllib.request

realm, auth_dir = sys.argv[1], sys.argv[2]
BASES = {
    "cn": ("https://copilot.tencent.com", "https://www.codebuddy.cn"),
    "global": ("https://www.workbuddy.ai", "https://www.workbuddy.ai"),
}
base, origin = BASES.get(realm, BASES["cn"])
import tempfile

state_file = os.path.join(tempfile.gettempdir(), "keyport-login-state.json")
with open(state_file, encoding="utf-8") as fh:
    state = json.load(fh)["state"]


def headers(token=""):
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
    }
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def get(url, token=""):
    req = urllib.request.Request(url, headers=headers(token))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


try:
    tok = get(base + "/v2/plugin/auth/token?state=" + state)
except urllib.error.HTTPError as exc:
    print(f"ERROR:登录未完成（HTTP {exc.code}）", file=sys.stderr)
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    print(f"ERROR:{exc}", file=sys.stderr)
    sys.exit(1)

access = tok.get("accessToken") or tok.get("access_token") or ""
if not access:
    print("ERROR:登录未完成（未取到 accessToken）", file=sys.stderr)
    sys.exit(1)

refresh = tok.get("refreshToken") or tok.get("refresh_token") or ""
expires_in = int(tok.get("expiresIn") or tok.get("expires_in") or 0)
domain = tok.get("domain") or ""

acct = {}
try:
    acct = get(base + "/v2/plugin/login/account?state=" + state, access)
except Exception:  # noqa: BLE001
    acct = {}

uid = acct.get("uid") or acct.get("userId") or ""
if not uid:
    import hashlib

    uid = "oauth-" + hashlib.sha256(access.encode()).hexdigest()[:16]

expires_at = int(time.time()) + expires_in if expires_in else 0
doc = {
    "auth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_at,
        "domain": domain,
        "realm": realm,
    },
    "account": {
        "uid": uid,
        "enterpriseId": acct.get("enterpriseId") or "",
        "nickname": acct.get("nickname") or "",
    },
}

path = os.path.join(auth_dir, f"workbuddy-{uid}.json")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
os.remove(state_file)

print(json.dumps({"uid": uid, "nickname": doc["account"]["nickname"],
                  "realm": realm, "path": path}, ensure_ascii=False))
PYEOF
) || { echo "❌ 获取 token 失败（确认浏览器已完成登录）"; exit 1; }

echo ""
echo "============================================================"
echo "  登录完成"
echo "  $RESULT"
echo "============================================================"
echo ""
echo "下一步："
echo "  - 管理台「账号管理」页点击「扫描 auths/」即可纳管（无需重启）"
echo "  - 或重启服务：docker compose restart $CONTAINER"
