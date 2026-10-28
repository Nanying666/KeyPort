"""内容策略误报自愈：`Illegal API invocation from an unapproved channel`。

上游按**逐字精确匹配**审核 system 提示词，客户端（Claude Code / Codex 等 CLI）
注入的模板句会被误杀成 HTTP 400 + 非授权渠道文案。断言四件事：

1. 该文案分类为 `CONTENT_BLOCKED`（不是 AUTH / BAD_REQUEST），403 形态也一样；
2. 账号不被惩罚（不冷却、不禁用、不计连败），也不换号（换号照样被拦）；
3. passthrough / append 模式下同请求换 Degraded 中性提示词重试，第二次成功；
4. 降级态跨请求保持到次日 00:00 CST，后续请求不再先撞一次 400。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.security import hash_api_key
from keyport.upstream.base import (
    AccountAuth,
    ErrorKind,
    UpstreamError,
    classify_status,
)
from keyport.upstream.content_gate import CST, ContentBlockGate, next_midnight_cst
from keyport.upstream.prompt import DEGRADED_PROMPT

# 真实上游响应体形态（code 11-128 不是合法 JSON 数字，故保留原文不做解析）
BLOCKED_BODY = (
    '{"code":11-128,"msg":"Illegal API invocation from an unapproved channel",'
    '"requestId":"d1192be5-dd60-47c2-a3aa-d7376774e17c"}'
)

OK_SSE = (
    'data: {"id":"c1","model":"glm-5.3-flash","choices":'
    '[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
    'data: {"id":"c1","model":"glm-5.3-flash","choices":'
    '[{"index":0,"delta":{"content":"hello"}}]}\n\n'
    'data: {"id":"c1","model":"glm-5.3-flash","choices":'
    '[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)

CLIENT_SYSTEM = "You are Claude Code, Anthropic's official CLI tool for Claude."

MSG = {
    "model": "glm-5.3-flash",
    "max_tokens": 16,
    "messages": [
        {"role": "system", "content": CLIENT_SYSTEM},
        {"role": "user", "content": "hi"},
    ],
}

_CHAT_SUFFIXES = ("/chat/completions", "/v1/messages")


class _Scripted:
    """只对对话端点按序返回预设响应，其余基础设施端点返回良性 200。"""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({
            "url": str(request.url),
            "path": request.url.path,
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": request.content.decode("utf-8", "replace"),
        })
        if not request.url.path.endswith(_CHAT_SUFFIXES):
            return httpx.Response(200, json={"code": 0, "data": {}})
        status, body = self.script.pop(0) if self.script else (200, OK_SSE)
        if status >= 400:
            return httpx.Response(status, content=body.encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(status, content=body.encode(),
                              headers={"content-type": "text/event-stream"})

    @property
    def chat_calls(self) -> list[dict]:
        return [c for c in self.calls if c["path"].endswith(_CHAT_SUFFIXES)]


def _build(tmp_path: Path, script: list[tuple[int, str]], *,
           prompt_mode: str = "passthrough"):
    cfg = make_test_config(tmp_path)
    cfg.prompt.mode = prompt_mode
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="wb-1", realm="cn", nickname="acc1",
        access_token="TOKEN", kind="oauth", domain="www.codebuddy.cn",
    ))
    raw = "sk-kp-blocked"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("kb", hash_api_key(raw), raw[:12]),
    )
    scripted = _Scripted(script)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(scripted))

    ctx.client.client = _mock  # type: ignore[method-assign]
    return app, ctx, scripted


AUTH = {"Authorization": "Bearer sk-kp-blocked"}


# ---- 分类 ----


def test_unapproved_channel_is_content_blocked():
    assert classify_status(400, BLOCKED_BODY) == ErrorKind.CONTENT_BLOCKED


def test_blocked_by_security_policy_is_content_blocked():
    body = '{"code":11-128,"msg":"blocked by security policy"}'
    assert classify_status(400, body) == ErrorKind.CONTENT_BLOCKED


def test_403_content_marker_is_not_auth_failure():
    """403 且带审核文案 ≠ 账号鉴权失效：误判 AUTH 会直接禁用健康号。"""
    body = '{"code":11-128,"msg":"Illegal API invocation from an unapproved channel"}'
    assert classify_status(403, body) == ErrorKind.CONTENT_BLOCKED


def test_plain_400_stays_bad_request():
    assert classify_status(400, '{"error":{"message":"invalid model"}}') == (
        ErrorKind.BAD_REQUEST)


def test_content_blocked_does_not_failover():
    err = UpstreamError(ErrorKind.CONTENT_BLOCKED, 400, "unapproved channel")
    assert err.should_failover is False


# ---- 网关：降级重试 ----


def test_passthrough_blocked_retries_with_degraded_prompt(tmp_path: Path):
    app, ctx, scripted = _build(
        tmp_path, [(400, BLOCKED_BODY), (200, OK_SSE)])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
        entry = ctx.pool.get("wb-1")
        assert entry.enabled is True
        assert entry.cooldown_until == 0
        assert entry.breaker_until == 0
        assert entry.consecutive_failures == 0, "内容拦截不该计入连败"
        assert entry.inflight == 0
        assert ctx.gateway.content_gate.active() is True

    calls = scripted.chat_calls
    assert len(calls) == 2, "同账号换提示词重试一次，不该换号"
    assert calls[0]["url"] == calls[1]["url"]
    first = json.loads(calls[0]["body"])
    second = json.loads(calls[1]["body"])
    assert first["messages"][0]["content"] == CLIENT_SYSTEM, "首请求必须透传客户端 system"
    assert second["messages"][0]["content"] == DEGRADED_PROMPT
    # 用户消息逐字不动
    assert second["messages"][1] == {"role": "user", "content": "hi"}


def test_degraded_gate_skips_first_400(tmp_path: Path):
    """降级期内请求直达中性提示词，不再先撞一次 400。"""
    app, ctx, scripted = _build(tmp_path, [(200, OK_SSE)])
    ctx.gateway.content_gate.trigger()
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
    calls = scripted.chat_calls
    assert len(calls) == 1
    body = json.loads(calls[0]["body"])
    assert body["messages"][0]["content"] == DEGRADED_PROMPT


def test_second_block_returns_error_without_penalty(tmp_path: Path):
    """第二次仍被拦（用户内容本身触发审核）→ 如实返回 400，不罚账号、不再换号。"""
    app, ctx, scripted = _build(
        tmp_path, [(400, BLOCKED_BODY), (400, BLOCKED_BODY), (200, OK_SSE)])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 400, resp.text
        payload = resp.json()
        assert payload["error"]["type"] == "upstream_content_blocked"
        assert "unapproved channel" in payload["error"]["message"]
        entry = ctx.pool.get("wb-1")
        assert entry.enabled is True
        assert entry.cooldown_until == 0
        assert entry.consecutive_failures == 0
    # 初始 + 降级重试；不再消耗其他账号（本例只有 1 个号）
    assert len(scripted.chat_calls) == 2


def test_custom_mode_does_not_degrade_retry(tmp_path: Path):
    """custom 模式已用网关自有提示词替换 system，不存在 system 指纹误报。"""
    app, ctx, scripted = _build(
        tmp_path, [(400, BLOCKED_BODY), (200, OK_SSE)], prompt_mode="custom")
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 400
        assert ctx.gateway.content_gate.active() is False
    assert len(scripted.chat_calls) == 1


# ---- 闸门时间语义 ----


def test_next_midnight_cst_boundaries():
    late = datetime(2026, 10, 28, 23, 59, 0, tzinfo=CST)
    assert next_midnight_cst(late.timestamp()) == datetime(
        2026, 10, 29, 0, 0, 0, tzinfo=CST).timestamp()
    # 恰好 00:00 → 次日 00:00（不能返回自身，否则降级立刻过期）
    midnight = datetime(2026, 10, 29, 0, 0, 0, tzinfo=CST)
    assert next_midnight_cst(midnight.timestamp()) == datetime(
        2026, 10, 30, 0, 0, 0, tzinfo=CST).timestamp()


def test_gate_trigger_does_not_extend_within_window():
    gate = ContentBlockGate()
    now = datetime(2026, 10, 28, 10, 0, 0, tzinfo=CST).timestamp()
    first = gate.trigger(now)
    assert gate.active(now) is True
    assert gate.trigger(now + 3600) == first, "降级期内再触发不续期"
    assert gate.active(first) is False


# ---- global 域 chat 声明头（对齐官方国际版客户端） ----


def test_global_chat_headers_declare_personal_account():
    """个人版 global 账号必须显式声明「无企业 ID」+ 国际版域。

    缺省不发这两条，上游按「缺省/可疑」判定，可能直接回 unapproved channel。
    """
    from keyport.config import UpstreamConfig
    from keyport.upstream import headers as H
    from keyport.upstream.base import ChatMeta

    auth = AccountAuth(uid="g-1", realm="global", access_token="TOK")
    headers = H.chat_headers(UpstreamConfig(), auth, ChatMeta(conversation_id="c1"))
    assert headers["X-No-Enterprise-Id"] == "1"
    assert headers["X-Domain"] == "www.workbuddy.ai"
    assert headers["X-User-Id"] == "g-1"
    assert "X-Enterprise-Id" not in headers


def test_global_chat_headers_keep_enterprise_id_when_present():
    from keyport.config import UpstreamConfig
    from keyport.upstream import headers as H
    from keyport.upstream.base import ChatMeta

    auth = AccountAuth(uid="g-2", realm="global", access_token="TOK",
                       enterprise_id="ent-9")
    headers = H.chat_headers(UpstreamConfig(), auth, ChatMeta())
    assert headers["X-Enterprise-Id"] == "ent-9"
    assert "X-No-Enterprise-Id" not in headers
