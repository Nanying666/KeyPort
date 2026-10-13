"""用真实 z.ai 账号包做端到端导入验证（真实 uvicorn + 假上游）。

上游用 MockTransport 拦截，验证「导入 → 出站打到正确的 Anthropic 端点 →
带 X-Device-Mid → 响应转回 OpenAI」全链路。
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from keyport.app import create_app  # noqa: E402
from keyport.config import Config  # noqa: E402
from keyport.security import hash_password  # noqa: E402

PORT = 8796
PW = "zai-import-pw-123"
BUNDLE = Path(r"c:/Users/Administrator/Desktop/iyzcfc.json")

seen: dict = {}


def anthropic_sse(text: str) -> bytes:
    events = [
        {"type": "message_start", "message": {
            "id": "msg_1", "role": "assistant", "model": "glm-4.6",
            "usage": {"input_tokens": 11, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    return "".join(
        f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events
    ).encode("utf-8")


def handler(request: httpx.Request) -> httpx.Response:
    seen["url"] = str(request.url)
    seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
    try:
        seen["body"] = json.loads(request.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        seen["body"] = {}
    return httpx.Response(200, content=anthropic_sse("来自 z.ai 的回复"),
                          headers={"content-type": "text/event-stream"})


def req(path, method="GET", body=None, headers=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        hdrs["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                               data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return (resp.status, resp.read().decode("utf-8", "replace"),
                    resp.headers.get("Set-Cookie", ""))
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read().decode("utf-8", "replace"),
                exc.headers.get("Set-Cookie", ""))


def main() -> int:
    if not BUNDLE.is_file():
        print(f"找不到账号包：{BUNDLE}")
        return 1

    cfg = Config(data_dir="./data/zai", auth_dir="./data/zai/auths",
                 static_dir="./frontend/out",
                 listen_host="127.0.0.1", listen_port=PORT)
    cfg.security.admin_password_hash = hash_password(PW)
    app = create_app(cfg)
    ctx = app.state.ctx

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(60):
        if req("/healthz")[0] in (200, 503):
            break
        time.sleep(0.2)

    checks = []
    st, _, sc = req("/api/login", "POST", {"username": "admin", "password": PW})
    cookie = next((p.strip() for p in sc.split(";") if p.strip().startswith("kp_session=")), "")
    checks.append(("登录", st == 200 and bool(cookie), str(st)))

    st, body, _ = req("/api/accounts/import", "POST",
                      {"text": BUNDLE.read_text(encoding="utf-8")}, cookie=cookie)
    parsed = json.loads(body)
    checks.append(("导入 z.ai 账号包", st == 200 and parsed["imported"] == 1,
                   f"{st} imported={parsed['imported']} failed={parsed['failed']}"))

    st, body, _ = req("/api/accounts", cookie=cookie)
    accounts = json.loads(body)["accounts"]
    acc = accounts[0] if accounts else {}
    checks.append(("账号已入池", st == 200 and acc.get("kind") == "apikey",
                   f"kind={acc.get('kind')} realm={acc.get('realm')}"))

    st, body, _ = req("/api/keys", "POST", {"name": "zai"}, cookie=cookie)
    key = json.loads(body).get("key", "") if st == 200 else ""
    checks.append(("创建密钥", bool(key), str(st)))

    st, body, _ = req("/v1/chat/completions", "POST",
                      {"model": "glm-4.6",
                       "messages": [{"role": "user", "content": "你好"}]},
                      headers={"Authorization": f"Bearer {key}"})
    ok = st == 200 and json.loads(body)["choices"][0]["message"]["content"] == "来自 z.ai 的回复"
    checks.append(("对话成功并转回 OpenAI", ok, f"{st} {body[:80]}"))

    checks.append(("出站打到 Anthropic 端点",
                   seen.get("url", "").endswith("/zcode-plan/anthropic/v1/messages"),
                   seen.get("url", "")[:78]))
    checks.append(("令牌走 Bearer",
                   seen.get("headers", {}).get("authorization", "").startswith("Bearer eyJ"),
                   seen.get("headers", {}).get("authorization", "")[:24]))
    checks.append(("携带 X-Device-Mid",
                   seen.get("headers", {}).get("x-device-mid")
                   == "78b921b4-ef0e-47cf-a333-555a14baa2f1",
                   str(seen.get("headers", {}).get("x-device-mid"))[:40]))
    checks.append(("出站体是 Anthropic 形态",
                   seen.get("body", {}).get("stream") is True
                   and "messages" in seen.get("body", {})
                   and "max_tokens" in seen.get("body", {}),
                   f"keys={sorted(seen.get('body', {}).keys())}"))

    st, body, _ = req("/api/usage?days=1", cookie=cookie)
    by_model = json.loads(body)["by_model"] if st == 200 else []
    total = sum(m["calls"] for m in by_model)
    checks.append(("用量已记账", total >= 1, f"calls={total}"))

    st, body, _ = req("/api/accounts/export", cookie=cookie)
    exported = json.loads(body)
    doc = exported["accounts"][0] if exported.get("accounts") else {}
    checks.append(("导出保留 protocol/base_url",
                   doc.get("protocol") == "anthropic" and "zcode-plan" in doc.get("base_url", ""),
                   f"protocol={doc.get('protocol')}"))

    st, body, _ = req("/api/accounts", cookie=cookie)
    uid = json.loads(body)["accounts"][0]["uid"]
    req(f"/api/accounts/{uid}", "DELETE", cookie=cookie)
    st, body, _ = req("/api/accounts/import", "POST", exported, cookie=cookie)
    checks.append(("导出文件可回灌", st == 200 and json.loads(body)["imported"] == 1,
                   f"imported={json.loads(body)['imported']}"))

    server.should_exit = True
    t.join(timeout=10)

    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:28} {detail}")
    fails = [n for n, ok, _ in checks if not ok]
    print("\n失败项：", fails or "无")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
