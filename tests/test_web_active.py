"""国际版每日活跃打卡（网页端云 agent 会话）。

移植自 ardeyouxipianyi/workbuddy2api-hub 的网页活跃链路。重点锁住三条
「不做就白跑」的约束：

1. **必须驱动 ACP 回合**，不能只建会话（否则会话永远停在 CREATING、零输出）
2. **必须显式钉模型**（`session/set_model`）：漏了会跑账号默认模型
   （网页端默认 Auto、按付费模型计费）—— 每天的活跃都在扣积分
3. **打卡成功与否以网页腿为准**：桌面腿成功不等于当天完成；网页腿失败时
   **不能**记「今日已完成」，否则当天后续巡检跳过该账号、积分静默丢掉
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import make_test_config

from keyport.app import build_context
from keyport.scheduler import _daily_chat_done
from keyport.upstream import webagent, webchat
from keyport.upstream.base import AccountAuth, UpstreamError
from keyport.upstream.client import UpstreamClient

SANDBOX = "https://sandbox.example/agent"
CONV_ID = "conv-abc"


def _auth(realm: str = "global") -> AccountAuth:
    return AccountAuth(uid="u1", realm=realm, access_token="T")


class _Upstream:
    """按 host + path 分发的假上游，并记录 ACP 的 JSON-RPC 调用序。"""

    def __init__(self) -> None:
        self.acp_methods: list[str] = []
        self.console_calls: list[tuple[str, str]] = []
        self.connect_id: str | None = "conn-1"
        self.create_code = 0
        self.create_msg = "OK"
        self.conversation_id = CONV_ID
        self.session: dict = {"link": SANDBOX, "token": "st", "sessionId": "sid",
                              "cwd": "/workspace"}
        self.status = "completed"
        self.acp_post_status = 202
        self.sse_lines: list[str] = []
        self.set_model_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host

        if host == "sandbox.example":
            return self._sandbox(request)

        self.console_calls.append((request.method, request.url.path))
        if request.method == "POST" and "/conversations/" in request.url.path:
            return httpx.Response(200, json={
                "code": self.create_code, "msg": self.create_msg,
                "data": {"id": self.conversation_id},
            })
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"code": 0, "data": self.session})
        # 桌面腿（chat/completions，流式）：只要 200
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, content=b"data: [DONE]\n\n")
        return httpx.Response(200, json={"code": 0, "data": {"status": self.status}})

    def _sandbox(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            headers = {}
            if self.connect_id is not None:
                headers["Acp-Connection-Id"] = self.connect_id
            body = "".join(f"data: {l}\n\n" for l in self.sse_lines).encode()
            return httpx.Response(200, headers=headers, content=body)
        payload = json.loads(request.content)
        method = payload.get("method", "")
        self.acp_methods.append(method)
        if method == "session/set_model" and self.set_model_status >= 400:
            return httpx.Response(self.set_model_status, json={})
        return httpx.Response(self.acp_post_status, json={"jsonrpc": "2.0"})


def _client(tmp_path: Path, up: _Upstream) -> UpstreamClient:
    """业务层客户端（与 test_growth.py 同注入方式）。"""
    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(up.handler))

    client.client = _mock  # type: ignore[method-assign]
    return client


def _raw_client(up: _Upstream) -> httpx.AsyncClient:
    """传输层用的裸 httpx 客户端（webagent 只认它）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(up.handler))


# ---- 出站头 ----


def test_web_origin_per_realm():
    assert webchat.web_origin("cn") == webchat.WEB_ORIGIN_CN
    assert webchat.web_origin("global") == webchat.WEB_ORIGIN_GLOBAL
    assert webchat.web_origin("") == webchat.WEB_ORIGIN_GLOBAL


def test_web_headers_are_web_identity_not_desktop():
    """网页端是**另一套身份**：不带桌面端指纹头（混用会两边都不像）。"""
    headers = webchat.web_headers(_auth(), webchat.WEB_ORIGIN_GLOBAL)
    assert headers["Authorization"] == "Bearer T"
    assert headers["X-User-Id"] == "u1"
    assert headers["Referer"].endswith("/app")
    assert "Mozilla/5.0" in headers["User-Agent"]
    for desktop_only in ("X-Machine-ID", "X-Session-ID", "X-Device-Token",
                         "X-CodeBuddy-Request", "X-Domain"):
        assert desktop_only not in headers


# ---- 建会话 ----


@pytest.mark.asyncio
async def test_create_conversation_posts_expected_shape(tmp_path: Path):
    up = _Upstream()
    client = _client(tmp_path, up)
    conversation = await webchat.create_conversation(
        client, _auth(), prompt="Hi", model="m1")
    assert conversation == CONV_ID
    # 结尾斜杠必须有（缺了会被上游 301 掉）
    assert up.console_calls[0] == ("POST", "/console/as/conversations/")


@pytest.mark.asyncio
async def test_create_conversation_body_keeps_web_fields(tmp_path: Path):
    """请求体保留网页端抓包所得的两项固定字段，不裁剪成最小形态。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"id": CONV_ID}})

    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    await webchat.create_conversation(client, _auth(), prompt="Hi", model="m1")

    assert seen["prompt"] == "Hi"
    assert seen["model"] == "m1"
    assert seen["conversationOrigin"] == "workbuddy-app"
    assert isinstance(seen["plugins"], list) and seen["plugins"]


@pytest.mark.asyncio
async def test_create_conversation_business_code_raises(tmp_path: Path):
    up = _Upstream()
    up.create_code = 403
    up.create_msg = "forbidden"
    client = _client(tmp_path, up)
    with pytest.raises(UpstreamError) as exc:
        await webchat.create_conversation(client, _auth())
    assert "403" in str(exc.value.message)


@pytest.mark.asyncio
async def test_create_conversation_missing_id_raises(tmp_path: Path):
    up = _Upstream()
    up.conversation_id = ""
    client = _client(tmp_path, up)
    with pytest.raises(UpstreamError, match="会话 id"):
        await webchat.create_conversation(client, _auth())


@pytest.mark.asyncio
async def test_conversation_get_quotes_id(tmp_path: Path):
    """会话 id 走 URL 路径段，必须转义（id 可能含 `/` `+`）。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"code": 0, "data": {"status": "completed"}})

    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    await webchat.conversation_get(client, _auth(), "a/b+c", "/session")

    assert paths and "%2F" in paths[0] and "%2B" in paths[0]


# ---- 网页腿 ----


@pytest.mark.asyncio
async def test_web_leg_drives_the_turn_not_just_creates(tmp_path: Path):
    """核心约束一：只建会话不算数 —— 必须把 ACP 回合驱动起来。"""
    up = _Upstream()
    client = _client(tmp_path, up)
    result = await webchat.run_web_leg(client, _auth())

    assert result["ok"] is True
    assert result["status"] == "completed"
    # 四个 JSON-RPC 请求都发了（只建会话的话这里是空的）
    assert up.acp_methods == ["initialize", "session/load",
                              "session/set_model", "session/prompt"]


@pytest.mark.asyncio
async def test_web_leg_pins_model_before_prompt(tmp_path: Path):
    """核心约束二：必须在 session/prompt 之前显式钉模型。

    漏了它，会话跑账号默认模型（网页端默认 Auto、按付费模型计费）——
    每天的活跃都在扣积分，比不跑更糟。
    """
    up = _Upstream()
    client = _client(tmp_path, up)
    await webchat.run_web_leg(client, _auth(), model="free-model")

    assert "session/set_model" in up.acp_methods
    assert (up.acp_methods.index("session/set_model")
            < up.acp_methods.index("session/prompt"))


@pytest.mark.asyncio
async def test_web_leg_sandbox_not_ready_is_failure(tmp_path: Path):
    """沙箱没就绪 → 如实报失败，不能当「跑完了」。"""
    up = _Upstream()
    up.session = {"token": ""}
    client = _client(tmp_path, up)
    result = await webchat.run_web_leg(client, _auth())

    assert result["ok"] is False
    assert "沙箱" in result["error"]
    assert result["conversation"] == CONV_ID


@pytest.mark.asyncio
async def test_web_leg_reports_unfinished_when_timeout(tmp_path: Path):
    """会话没跑到 completed → 失败（带状态，便于排查停在哪）。"""
    up = _Upstream()
    up.status = "CREATING"
    client = _client(tmp_path, up)
    result = await webchat.run_web_leg(client, _auth(), turn_timeout=1.0)

    assert result["ok"] is False
    assert "没有跑完" in result["error"]


@pytest.mark.asyncio
async def test_web_leg_failed_status_stops_early(tmp_path: Path):
    up = _Upstream()
    up.status = "failed"
    client = _client(tmp_path, up)
    result = await webchat.run_web_leg(client, _auth())

    assert result["ok"] is False
    assert "failed" in result["error"]


# ---- ACP 传输层（裸 httpx，故显式传 poll_status）----


@pytest.mark.asyncio
async def test_acp_requires_connection_id():
    up = _Upstream()
    up.connect_id = None
    async with _raw_client(up) as http:
        result = await webagent.run_turn(
            http, link=SANDBOX, token="t", session_id="s", prompt="Hi",
            poll_status=lambda: _done())
    assert result["ok"] is False
    assert "Acp-Connection-Id" in result["error"]


async def _done() -> str:
    return "completed"


@pytest.mark.asyncio
async def test_acp_counts_updates_and_chunks():
    up = _Upstream()
    up.sse_lines = [
        json.dumps({"method": "session/update",
                    "params": {"update": {"sessionUpdate": "agent_message_chunk"}}}),
        json.dumps({"method": "session/update",
                    "params": {"update": {"sessionUpdate": "agent_message_chunk"}}}),
        json.dumps({"method": "session/update",
                    "params": {"update": {"sessionUpdate": "tool_call"}}}),
    ]
    async with _raw_client(up) as http:
        result = await webagent.run_turn(
            http, link=SANDBOX, token="t", session_id="s", prompt="Hi",
            poll_status=_done)

    assert result["ok"] is True
    assert result["chunks"] == 2
    assert result["events"] == 3


@pytest.mark.asyncio
async def test_acp_reports_http_error_on_request():
    up = _Upstream()
    up.acp_post_status = 500
    async with _raw_client(up) as http:
        result = await webagent.run_turn(
            http, link=SANDBOX, token="t", session_id="s", prompt="Hi",
            poll_status=_done)

    assert result["ok"] is False
    assert "HTTP 500" in result["error"]


@pytest.mark.asyncio
async def test_acp_set_model_failure_is_not_fatal():
    """钉模型失败不当致命错：这一轮还是要跑完，只是日志留一句。"""
    up = _Upstream()
    up.set_model_status = 500
    logged: list[str] = []
    async with _raw_client(up) as http:
        result = await webagent.run_turn(
            http, link=SANDBOX, token="t", session_id="s", prompt="Hi",
            poll_status=_done, model="m",
            log=lambda fmt, *a: logged.append(fmt % a if a else fmt))

    assert result["ok"] is True
    assert any("设置模型" in m for m in logged)


@pytest.mark.asyncio
async def test_channel_close_swallows_cancelled_error():
    """`close()` 取消读取任务后必须吞掉 `CancelledError`。

    `CancelledError` **不是** `Exception` 的子类 —— 只 `suppress(Exception)`
    会让取消抛穿到调用方，把一次正常的收尾变成异常。
    """
    up = _Upstream()
    async with _raw_client(up) as http:
        channel = webagent.AcpChannel(http, SANDBOX, "t", "")

        async def _never() -> None:
            await asyncio.sleep(3600)

        channel._reader = asyncio.create_task(_never())
        await channel.close(grace=0.01)  # 不该抛
        assert channel._reader is None


@pytest.mark.asyncio
async def test_run_turn_requires_poll_status():
    """`poll_status` 是必填的：没有它永远无法认定「跑完了」，实现只能一律报超时。

    用签名断言（而非调用断言）—— 这是接口约束，不是运行时行为。
    """
    import inspect

    params = inspect.signature(webagent.run_turn).parameters
    assert params["poll_status"].default is inspect.Parameter.empty
    assert params["poll_status"].kind is inspect.Parameter.KEYWORD_ONLY


# ---- 打卡总入口 ----


@pytest.mark.asyncio
async def test_daily_chat_rejects_non_global(tmp_path: Path):
    up = _Upstream()
    client = _client(tmp_path, up)
    result = await webchat.daily_chat(client, _auth("cn"))
    assert result["ok"] is False
    assert "国际版" in result["error"]


@pytest.mark.asyncio
async def test_daily_chat_web_leg_decides_success(tmp_path: Path):
    """核心约束三：ok 以网页腿为准。"""
    up = _Upstream()
    client = _client(tmp_path, up)
    result = await webchat.daily_chat(client, _auth(), desktop_leg=False)
    assert result["ok"] is True
    assert result["web"]["status"] == "completed"


@pytest.mark.asyncio
async def test_desktop_leg_uses_chat_headers_and_succeeds(tmp_path: Path):
    """桌面腿必须能真正发出（回归：曾误用 `client.cfg` 而必然 AttributeError）。"""
    up = _Upstream()
    client = _client(tmp_path, up)
    got = await webchat._desktop_leg(client, _auth(), model="m1")

    assert got["ok"] is True
    assert any(path.endswith("/chat/completions") for _m, path in up.console_calls)


@pytest.mark.asyncio
async def test_daily_chat_fails_when_web_leg_fails_even_if_desktop_ok(tmp_path: Path):
    """桌面腿成功**不能**把整次算成功 —— 计入官方日活的只有网页腿。"""
    up = _Upstream()
    up.session = {}  # 沙箱不就绪 → 网页腿必失败
    client = _client(tmp_path, up)
    result = await webchat.daily_chat(client, _auth(), desktop_leg=True)

    assert result["desktop"]["ok"] is True
    assert result["web"]["ok"] is False
    assert result["ok"] is False
    assert "网页通道失败" in result["error"]


# ---- 日期去重 ----


def test_daily_chat_done_same_day_only():
    class E:
        last_daily_chat = ""

    e = E()
    assert _daily_chat_done(e) is False
    e.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
    assert _daily_chat_done(e) is True
    # 昨天的不算今天
    e.last_daily_chat = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    assert _daily_chat_done(e) is False


# ---- 调度门控与记账 ----


def test_gate_web_active_is_global_only(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="g1", realm="global", access_token="T"))
    ctx.pool.upsert_account(AccountAuth(uid="c1", realm="cn", access_token="T"))

    sched = ctx.scheduler
    assert sched._gate_reason("web_active", ctx.pool.get("g1")) == ""
    assert "国内版" in sched._gate_reason("web_active", ctx.pool.get("c1"))


def test_gate_web_active_skips_when_done_today(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="g1", realm="global", access_token="T"))
    entry = ctx.pool.get("g1")
    ctx.pool.mark_daily_chat("g1")
    assert "今日打卡已完成" in ctx.scheduler._gate_reason("web_active", entry)


def test_mark_daily_chat_persists(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="g1", realm="global", access_token="T"))
    ctx.pool.mark_daily_chat("g1")
    stamp = ctx.pool.get("g1").last_daily_chat
    assert stamp
    # 落库（重启不丢）
    row = ctx.db.query_one("SELECT last_daily_chat FROM accounts WHERE uid=?", ("g1",))
    assert row["last_daily_chat"] == stamp
    # 落 state.json
    assert ctx.pool.get("g1").to_state()["last_daily_chat"] == stamp


@pytest.mark.asyncio
async def test_scheduler_marks_only_on_web_success(tmp_path: Path, monkeypatch):
    """网页腿失败时**不能**记「今日已完成」。"""
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="g1", realm="global", access_token="T"))
    entry = ctx.pool.get("g1")

    from keyport.upstream import webchat as W

    async def _fail(upstream, auth, **kw):
        return {"ok": False, "error": "网页通道失败：x", "web": {"ok": False}}

    monkeypatch.setattr(W, "daily_chat", _fail)
    outcome = await ctx.scheduler._web_active(entry, ctx.pool.auth_for(entry))
    assert outcome["ok"] is False
    assert ctx.pool.get("g1").last_daily_chat == ""

    async def _ok(upstream, auth, **kw):
        return {"ok": True, "msg": "完成",
                "web": {"ok": True, "status": "completed", "chunks": 3}}

    monkeypatch.setattr(W, "daily_chat", _ok)
    outcome = await ctx.scheduler._web_active(entry, ctx.pool.auth_for(entry))
    assert outcome["ok"] is True
    assert ctx.pool.get("g1").last_daily_chat != ""
    assert "chunks=3" in outcome["detail"]


# ---- 配置默认值 ----


def test_web_active_disabled_by_default():
    """有副作用的上游动作，默认关闭。"""
    from keyport.config import ScheduleConfig

    cfg = ScheduleConfig()
    assert cfg.web_active_enabled is False
    assert cfg.web_active_model == "deepseek-v4.1-flash"
    assert cfg.web_active_desktop_leg is True


def test_web_active_registered_in_scheduler_mapping():
    """任务必须在 tick() 的映射表里，否则排程永远不会触发它。"""
    import inspect

    from keyport import scheduler as S

    source = inspect.getsource(S.Scheduler.tick)
    assert '"web_active"' in source
    assert "web_active" in S.TASKS
