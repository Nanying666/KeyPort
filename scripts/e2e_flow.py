"""端到端流程验证：真实 uvicorn + 假上游，跑完「登录 → 建密钥 → 三种协议调用」。

覆盖 TestClient 覆盖不到的部分：真实 ASGI 下的 SSE 流式传输、Cookie 会话、静态托管。

用法：python scripts/e2e_flow.py
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

PORT = 8798
PASSWORD = "e2e-password-123"


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _upstream_stream(text: str) -> bytes:
    return _sse(
        {"id": "c1", "model": "glm-4.6", "created": 1,
         "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"id": "c1", "model": "glm-4.6",
         "choices": [{"index": 0, "delta": {"content": text}}]},
        {"id": "c1", "model": "glm-4.6",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}},
    )


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/chat/completions"):
        return httpx.Response(
            200, content=_upstream_stream("流式回复内容"),
            headers={"content-type": "text/event-stream"},
        )
    if "models" in request.url.path:
        return httpx.Response(200, json={"data": [
            {"id": "glm-4.6", "displayName": "GLM-4.6", "contextWindow": 200000},
        ]})
    return httpx.Response(404, json={"code": 404})


def req(path: str, *, method: str = "GET", body: dict | None = None,
        headers: dict | None = None, cookie: str = "") -> tuple[int, str, str]:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        hdrs["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                              data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return (resp.status, resp.read().decode("utf-8", errors="replace"),
                    resp.headers.get("Set-Cookie", ""))
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read().decode("utf-8", errors="replace"),
                exc.headers.get("Set-Cookie", ""))


def main() -> int:
    cfg = Config(
        data_dir="./data/e2e", auth_dir="./auths", static_dir="./frontend/out",
        listen_host="127.0.0.1", listen_port=PORT,
    )
    cfg.security.admin_password_hash = hash_password(PASSWORD)
    app = create_app(cfg)
    ctx = app.state.ctx

    from keyport.upstream.base import AccountAuth

    ctx.pool.upsert_account(AccountAuth(
        uid="e2e-uid", realm="cn", nickname="e2e", access_token="tok",
        refresh_token="r", expires_at=0,
    ))

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        status, _, _ = req("/healthz")
        if status in (200, 503):
            break
        time.sleep(0.2)

    checks: list[tuple[str, bool, str]] = []

    # 1. 登录
    status, body, set_cookie = req("/api/login", method="POST",
                                   body={"username": "admin", "password": PASSWORD})
    cookie = ""
    for part in set_cookie.split(";"):
        if part.strip().startswith("kp_session="):
            cookie = part.strip()
    checks.append(("管理台登录", status == 200 and bool(cookie),
                   f"{status} cookie={'有' if cookie else '无'}"))

    # 2. 建密钥
    status, body, _ = req("/api/keys", method="POST",
                          body={"name": "e2e", "model_allow": ["glm-4.6"]}, cookie=cookie)
    raw_key = json.loads(body).get("key", "") if status == 200 else ""
    checks.append(("创建密钥（明文仅一次）", status == 200 and raw_key.startswith("sk-kp"),
                   f"{status} key={'有' if raw_key else '无'}"))

    auth = {"Authorization": f"Bearer {raw_key}"}

    # 3. OpenAI 非流式
    status, body, _ = req("/v1/chat/completions", method="POST",
                          body={"model": "glm-4.6",
                                "messages": [{"role": "user", "content": "hi"}]},
                          headers=auth)
    content = json.loads(body)["choices"][0]["message"]["content"] if status == 200 else ""
    checks.append(("OpenAI 非流式", status == 200 and content == "流式回复内容",
                   f"{status} content={content!r}"))

    # 4. OpenAI 流式（真实 ASGI SSE）
    status, body, _ = req("/v1/chat/completions", method="POST",
                          body={"model": "glm-4.6", "stream": True,
                                "messages": [{"role": "user", "content": "hi"}]},
                          headers=auth)
    checks.append(("OpenAI 流式 SSE", status == 200 and "[DONE]" in body and "data:" in body,
                   f"{status} 帧数={body.count('data:')}"))

    # 5. Anthropic 流式
    status, body, _ = req("/v1/messages", method="POST",
                          body={"model": "glm-4.6", "stream": True, "max_tokens": 64,
                                "messages": [{"role": "user", "content": "hi"}]},
                          headers={"x-api-key": raw_key})
    checks.append(("Anthropic 流式", status == 200 and "message_stop" in body
                   and "content_block_delta" in body, str(status)))

    # 6. Anthropic 非流式
    status, body, _ = req("/v1/messages", method="POST",
                          body={"model": "glm-4.6", "max_tokens": 64,
                                "messages": [{"role": "user", "content": "hi"}]},
                          headers={"x-api-key": raw_key})
    ok = status == 200 and json.loads(body)["type"] == "message"
    checks.append(("Anthropic 非流式", ok, str(status)))

    # 7. Responses 流式
    status, body, _ = req("/v1/responses", method="POST",
                          body={"model": "glm-4.6", "input": "hi", "stream": True},
                          headers=auth)
    checks.append(("Responses 流式", status == 200 and "response.completed" in body, str(status)))

    # 8. Responses 非流式
    status, body, _ = req("/v1/responses", method="POST",
                          body={"model": "glm-4.6", "input": "hi"}, headers=auth)
    ok = status == 200 and json.loads(body)["object"] == "response"
    checks.append(("Responses 非流式", ok, str(status)))

    # 9. 模型列表
    status, body, _ = req("/v1/models", headers=auth)
    ok = status == 200 and json.loads(body)["data"][0]["id"] == "glm-4.6"
    checks.append(("模型列表", ok, str(status)))

    # 10. 模型白名单拦截
    status, _, _ = req("/v1/chat/completions", method="POST",
                       body={"model": "other-model",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=auth)
    checks.append(("模型白名单拦截", status == 400, str(status)))

    # 11. 用量已落库
    status, body, _ = req("/api/usage?days=1", cookie=cookie)
    by_model = json.loads(body)["by_model"] if status == 200 else []
    total_calls = sum(m["calls"] for m in by_model)
    checks.append(("用量统计已记账", status == 200 and total_calls >= 5, f"calls={total_calls}"))

    # 12. 日志含 key=
    status, body, _ = req("/api/logs?limit=20", cookie=cookie)
    logs = json.loads(body)["logs"] if status == 200 else []
    has_key = any(log["key_name"] == "e2e" for log in logs)
    checks.append(("调用日志含调用密钥", status == 200 and has_key, f"logs={len(logs)}"))

    # 13. 静态托管
    status, body, _ = req("/")
    checks.append(("管理台静态托管", status == 200 and "<!doctype html" in body.lower(),
                   f"{status} len={len(body)}"))

    server.should_exit = True
    thread.join(timeout=10)

    print(json.dumps([{"check": c, "ok": ok, "detail": d} for c, ok, d in checks],
                     ensure_ascii=False, indent=2))
    failures = [c for c, ok, _ in checks if not ok]
    print("\n失败项：", failures or "无")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
