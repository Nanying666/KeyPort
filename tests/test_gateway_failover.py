"""网关故障转移：429 原地重试、风控隔离、apiKey 回退通道。

用 MockTransport 注入上游响应序列，验证真实调度行为（选号 → 重试 → 换号）
以及**账号状态不被误伤**（429 不冷却、验证码不惩罚、回退通道不洗风控）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.security import hash_api_key
from keyport.upstream.base import AccountAuth

ZCODE_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
FALLBACK_BASE = "https://api.z.ai/api/anthropic"

RISK_BODY = '{"code":3012,"msg":"unusual activity","logid":"abc"}'
CAPTCHA_BODY = '{"code":3007,"msg":"captcha verify failed"}'

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

# 对话端点后缀（脚本只对这些请求生效）
_CHAT_SUFFIXES = ("/v1/messages", "/chat/completions")


class _Scripted:
    """只对**对话端点**按序返回预设响应；其余基础设施端点返回良性响应。

    为什么必须分流：启动期会打 `client/configs` / `billing/balance` / 模型目录，
    风控换设备后还会后台补跑安装序。若不分流，预设脚本会被这些请求吃掉，
    断言就会错位到基础设施调用上。
    """

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
            # 基础设施端点：不消费脚本，返回良性 200
            return httpx.Response(200, json={"code": 0, "data": {}})
        status, body = self.script.pop(0) if self.script else (200, OK_SSE)
        if status >= 400:
            return httpx.Response(status, content=body.encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(status, content=body.encode(),
                              headers={"content-type": "text/event-stream"})

    @property
    def chat_calls(self) -> list[dict]:
        """只看对话端点调用（断言用）。"""
        return [c for c in self.calls if c["path"].endswith(_CHAT_SUFFIXES)]


def _build(tmp_path: Path, script: list[tuple[int, str]], *, fallback: bool = False):
    cfg = make_test_config(tmp_path)
    cfg.pool.retry_429_wait = 0          # 测试不真等待
    cfg.pool.retry_5xx_wait = 0
    cfg.install.enabled = False          # 不跑安装序，避免噪声调用
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="zai-1", realm="zai", nickname="acc1",
        access_token="PLAN_JWT", kind="apikey",
        device_token="dev-mid-1", base_url=ZCODE_BASE, protocol="anthropic",
        fallback_token="API_KEY" if fallback else "",
        fallback_base_url=FALLBACK_BASE if fallback else "",
    ))
    raw = "sk-kp-failover"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("fo", hash_api_key(raw), raw[:12]),
    )
    scripted = _Scripted(script)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(scripted))

    ctx.client.client = _mock  # type: ignore[method-assign]
    return app, ctx, scripted


AUTH = {"Authorization": "Bearer sk-kp-failover"}
MSG = {"model": "glm-5.3-flash", "max_tokens": 16,
       "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture()
def captcha_stub():
    """替掉验证码农场：每次发一个新参数（不启动真浏览器）。"""
    from keyport.config import CaptchaConfig

    class _Stub:
        def __init__(self):
            self.cfg = CaptchaConfig(region="cn")
            self.n = 0
            self.invalidated = 0

        async def acquire(self, jwt: str = "") -> str:
            self.n += 1
            return f"param-{self.n}"

        def invalidate(self) -> None:
            self.invalidated += 1

    return _Stub()


# ---- 429：原地重试，账号保持可用 ----


def test_429_retries_in_place_then_succeeds(tmp_path: Path):
    """429 不是账号故障：原地等待重试成功后账号不受任何惩罚。"""
    app, ctx, scripted = _build(tmp_path, [(429, '{"msg":"rate limited"}'), (200, OK_SSE)])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
        entry = ctx.pool.get("zai-1")
        # 账号保持可用、无冷却、无熔断
        assert entry.enabled is True
        assert entry.cooldown_until == 0
        assert entry.breaker_until == 0
    assert len(scripted.chat_calls) == 2
    assert scripted.chat_calls[0]["url"] == scripted.chat_calls[1]["url"]


def test_429_honours_retry_after_header(tmp_path: Path):
    """优先采信上游 Retry-After（此处仅验证不炸且最终成功）。"""
    app, ctx, scripted = _build(tmp_path, [(429, '{"msg":"slow down"}'), (200, OK_SSE)])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200
    assert len(scripted.chat_calls) == 2


def test_429_exhausted_switches_account_and_keeps_it_usable(tmp_path: Path):
    """重试预算耗尽 → 换号，且该账号保持可用（不冷却、不计连败）。"""
    app, ctx, scripted = _build(tmp_path, [(429, '{"msg":"rate limited"}')] * 10)
    ctx.cfg.pool.retry_429_times = 2
    ctx.cfg.pool.retry_429_wait = 0
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        # 只有 1 个账号且重试耗尽：429 原样透出给客户端（有效的退避信号）
        assert resp.status_code == 429
        entry = ctx.pool.get("zai-1")
        assert entry.enabled is True, "429 不应禁用账号"
        assert entry.cooldown_until == 0, "429 不应冷却账号"
        assert entry.consecutive_failures == 0, "429 不应计入连败"
    # 1 次初始 + 2 次重试 = 3 次上游调用
    assert len(scripted.chat_calls) == 3


# ---- 风控：指数退避，不原地重试 ----


def test_risk_cools_account_without_retrying_in_place(tmp_path: Path):
    """风控不原地重试（避免持续施压），账号进冷却但保持启用。"""
    app, ctx, scripted = _build(tmp_path, [(405, RISK_BODY)])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        entry = ctx.pool.get("zai-1")
        assert entry.cooldown_until > 0
        assert entry.risk_strikes == 1
        assert entry.enabled is True
        # 风控不是「方法不允许」：必须映射成服务端侧限制（502），
        # 否则客户端会误以为要改请求方法
        assert resp.status_code == 502, resp.text
    # 风控后立即换号（无第二个账号），不重复打上游
    assert len(scripted.chat_calls) == 1


def test_risk_disables_after_ban_strikes(tmp_path: Path):
    """累计到阈值升级禁用（人工确认恢复）。"""
    app, ctx, scripted = _build(tmp_path, [(405, RISK_BODY)] * 10)
    ctx.cfg.pool.risk_ban_strikes = 2
    ctx.cfg.pool.risk_cooldown_seconds = 900
    with TestClient(app) as tc:
        for _ in range(2):
            # 只清冷却、保留 strikes —— revive 会一并清零计数，这里刻意绕开
            ctx.pool.get("zai-1").cooldown_until = 0
            tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        entry = ctx.pool.get("zai-1")
        assert entry.enabled is False
        assert entry.disabled_reason == "risk_control"


# ---- apiKey 回退通道 ----


def test_risk_switches_to_fallback_channel(tmp_path: Path):
    """Plan 通道命中风控且有回退 Key → 切 api.z.ai 通道继续服务。"""
    app, ctx, scripted = _build(
        tmp_path, [(405, RISK_BODY), (200, OK_SSE)], fallback=True
    )
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
    assert len(scripted.chat_calls) == 2
    assert scripted.chat_calls[0]["url"].startswith("https://zcode.z.ai")
    # 回退通道打到 api.z.ai，且用回退令牌
    assert scripted.chat_calls[1]["url"] == FALLBACK_BASE + "/v1/messages"
    assert scripted.chat_calls[1]["headers"]["authorization"] == "Bearer API_KEY"


def test_429_exhausted_switches_to_fallback(tmp_path: Path):
    """Plan 通道 429 重试耗尽且有回退 Key → 切回退通道（归还重试预算）。"""
    app, ctx, scripted = _build(
        tmp_path,
        [(429, '{"msg":"rate limited"}'), (429, '{"msg":"rate limited"}'), (200, OK_SSE)],
        fallback=True,
    )
    ctx.cfg.pool.retry_429_times = 1
    ctx.cfg.pool.retry_429_wait = 0
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
    urls = [c["url"] for c in scripted.chat_calls]
    assert urls[0].startswith("https://zcode.z.ai")
    assert urls[-1] == FALLBACK_BASE + "/v1/messages"


def test_fallback_success_does_not_clear_risk_state(tmp_path: Path):
    """回退通道成功不得洗掉风控计数（Plan 通道仍未解除）。"""
    app, ctx, scripted = _build(
        tmp_path, [(405, RISK_BODY), (200, OK_SSE)], fallback=True
    )
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200
        entry = ctx.pool.get("zai-1")
        assert entry.risk_strikes == 1
        assert entry.cooldown_until > 0


def test_permanent_429_skips_waiting(tmp_path: Path):
    """1113「无余额/无资源包」是 Key 永久态：不原地等，直接切回退/换号。"""
    app, ctx, scripted = _build(
        tmp_path,
        [(429, '{"code":1113,"msg":"insufficient balance"}'), (200, OK_SSE)],
        fallback=True,
    )
    ctx.cfg.pool.retry_429_times = 5
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
    # 只 2 次调用：没有把 5 次重试预算耗在无意义的等待上
    assert len(scripted.chat_calls) == 2
    assert scripted.chat_calls[1]["url"] == FALLBACK_BASE + "/v1/messages"


# ---- 验证码挑战：同账号换码重试 ----


def test_captcha_challenge_retries_with_fresh_param(tmp_path: Path, captcha_stub):
    app, ctx, scripted = _build(tmp_path, [(400, CAPTCHA_BODY), (200, OK_SSE)])
    ctx.client.captcha = captcha_stub
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
    params = [c["headers"].get("x-aliyun-captcha-verify-param")
              for c in scripted.chat_calls]
    assert params[0] != params[1], "重试必须换新验证码参数"
    assert captcha_stub.invalidated >= 1


def test_captcha_exhausted_does_not_penalise_account(tmp_path: Path, captcha_stub):
    """验证码连续失败是端点级限制，账号不冷却不熔断。"""
    app, ctx, scripted = _build(tmp_path, [(400, CAPTCHA_BODY)] * 20)
    ctx.client.captcha = captcha_stub
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 502
        entry = ctx.pool.get("zai-1")
        assert entry.cooldown_until == 0
        assert entry.breaker_until == 0
        assert entry.consecutive_failures == 0


# ---- 并发槽不泄漏 ----


def test_inflight_slot_released_after_failure(tmp_path: Path):
    """失败路径必须归还并发槽，否则账号会被自己的槽位永久占满。"""
    app, ctx, scripted = _build(tmp_path, [(405, RISK_BODY)])
    with TestClient(app) as tc:
        tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        entry = ctx.pool.get("zai-1")
        assert entry.inflight == 0


def test_inflight_slot_released_after_success(tmp_path: Path):
    app, ctx, scripted = _build(tmp_path, [(200, OK_SSE)])
    with TestClient(app) as tc:
        tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        entry = ctx.pool.get("zai-1")
        assert entry.inflight == 0


def test_inflight_slot_released_after_429_retries(tmp_path: Path):
    app, ctx, scripted = _build(tmp_path, [(429, '{"msg":"rl"}'), (200, OK_SSE)])
    cfg = ctx.cfg
    cfg.pool.retry_429_wait = 0
    with TestClient(app) as tc:
        tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert ctx.pool.get("zai-1").inflight == 0


# ---- 5xx ----


def test_5xx_retries_then_cools(tmp_path: Path):
    """5xx 原地重试，耗尽后账号冷却换号；无号可用时透出上游真实错误。

    这里刻意**不**把 500 改写成 503：账号都被真实尝试过且上游确实 5xx 时，
    透出 500 比「无可用账号」更有诊断价值（后者只用于「从未选中任何账号」）。
    """
    app, ctx, scripted = _build(tmp_path, [(500, '{"msg":"boom"}')] * 10)
    ctx.cfg.pool.retry_5xx_times = 2
    ctx.cfg.pool.retry_5xx_wait = 0
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 500
        entry = ctx.pool.get("zai-1")
        # 重试耗尽后账号被冷却（与 429 的关键差异：5xx 是账号侧故障）
        assert entry.cooldown_until > 0
        assert entry.enabled is True
    # 1 次初始 + 2 次重试
    assert len(scripted.chat_calls) == 3


def test_no_account_in_realm_returns_503(tmp_path: Path):
    """从未选中任何账号（域内无号）→ 503 no_available_account。"""
    app, ctx, scripted = _build(tmp_path, [(200, OK_SSE)])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={**MSG, "model": "cn:glm-4.6"}, headers=AUTH)
        assert resp.status_code == 503
    assert len(scripted.chat_calls) == 0
