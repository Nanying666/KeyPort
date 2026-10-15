"""跨域兜底选号：global 的 0 倍率免费模型先跑，打不动再用 cn 同名模型兜底。

背景（真实部署撞到的三个问题）：

1. `models_free_only` 是按域的**展示**策略，却被误当成路由可用性判据 ——
   cn 那份付费同名模型被过滤后，该模型只剩 global 一个归属域，
   `route_by_model` 把请求锁死在 global，global 全被该模型限流时下游直接吃 429，
   cn 明明能跑却永远不会被尝试。
2. 跨域重名模型归属歧义 → 不限域 → 三因子权重看「积分余额」，而 cn 账号积分
   远高于 global，于是免费额度被打不动的同时还把请求送给付费域。
3. 某些模型上游目录接口不下发、但实际可调用（如 `hy4-preview-f`）：既不会出现在
   `/v1/models`，又会因「归属未知」不限域而随机落到别的域。

修复：`_raw`（未过滤目录，路由用）与 `_cache`（过滤后目录，展示用）分离；
`pool.realm_priority` 给出「优先 → 兜底」域链；`realms.<域>.extra_models` 补录漏发模型。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import build_context, create_app
from keyport.security import hash_api_key
from keyport.upstream import constants as C
from keyport.upstream.base import AccountAuth

MODEL = "deepseek-v4.1-flash"

# 上游「该模型用量超限」：不是账号故障，只冷却该模型
MODEL_LIMIT_BODY = (
    '{"code":6004,"msg":"usage exceeds frequency limit, '
    'your usage will reset at 2026-10-16 00:00:00 UTC+8"}'
)

OK_SSE = (
    'data: {"id":"c1","model":"deepseek-v4.1-flash","choices":'
    '[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
    'data: {"id":"c1","model":"deepseek-v4.1-flash","choices":'
    '[{"index":0,"delta":{"content":"ok"}}]}\n\n'
    'data: {"id":"c1","model":"deepseek-v4.1-flash","choices":'
    '[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)

CHAT_SUFFIX = "/chat/completions"

GLOBAL_PAYLOAD = {"data": {"models": [
    {"id": "hy3", "credits": "x0.00"},
    {"id": MODEL, "credits": "x0.00"},
]}}
CN_PAYLOAD = {"data": {"models": [
    {"id": MODEL, "credits": "x0.11"},
]}}


# ---------------------------------------------------------------- 目录与归属


@pytest.fixture()
def ctx(tmp_path: Path):
    """global 提供 hy3 + deepseek-v4.1-flash（免费），cn 只提供同名带倍率版本。"""
    cfg = make_test_config(tmp_path)
    c = build_context(cfg)
    c.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))
    c.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))

    def handler(request: httpx.Request) -> httpx.Response:
        # 必须按**主机**分流：两个域的模型目录端点路径相同（/v3/config），
        # 按路径分流会让 cn 拿到 global 的目录，测试就假通过了。
        host = request.url.host or ""
        if "workbuddy" in host:
            return httpx.Response(200, json=GLOBAL_PAYLOAD)
        if "copilot" in host or "codebuddy" in host:
            return httpx.Response(200, json=CN_PAYLOAD)
        return httpx.Response(200, json={"data": {"models": []}})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    c.client.client = _mock  # type: ignore[method-assign]
    yield c


async def test_free_only_filters_listing_but_not_routing(ctx):
    """核心回归：cn 的付费同名模型从展示里滤掉，但路由上仍算「cn 能提供」。"""
    ctx.cfg.realms["global"].models_free_only = True
    ctx.cfg.realms["cn"].models_free_only = True
    await ctx.models.list_models("", refresh=True)

    # 展示目录：cn 只剩 0 倍率的没有 -> 空；global 保留免费档
    assert [m["id"] for m in ctx.models.cached()["cn"]] == []
    assert MODEL in [m["id"] for m in ctx.models.cached()["global"]]

    # 路由目录：两边都还在 —— 否则 cn 连兜底资格都没有了（这就是线上 429 的根因）
    assert sorted(ctx.models.realms_for_model(MODEL)) == ["cn", "global"]


async def test_extra_models_injected_and_listed_as_free(ctx):
    """上游目录不下发但可调用的模型：补录后进展示目录，且归属唯一确定。"""
    ctx.cfg.realms["global"].models_free_only = True
    ctx.cfg.realms["global"].extra_models = ["hy4-preview-f"]
    await ctx.models.list_models("", refresh=True)

    listed = next(m for m in ctx.models.cached()["global"] if m["id"] == "hy4-preview-f")
    assert listed["credit_factor"] == 0        # 字符串形式按免费处理
    assert ctx.models.realms_for_model("hy4-preview-f") == ["global"]


async def test_extra_models_accepts_explicit_factor(ctx):
    """对象形式可显式声明倍率，且不被 models_free_only 放行。"""
    ctx.cfg.realms["cn"].models_free_only = True
    ctx.cfg.realms["cn"].extra_models = [{"id": "hy4-preview", "credit_factor": 0.29}]
    await ctx.models.list_models("cn", refresh=True)
    assert "hy4-preview" not in [m["id"] for m in ctx.models.cached()["cn"]]
    assert "hy4-preview" in [m["id"] for m in ctx.models.raw_cached()["cn"]]


async def test_extra_models_without_factor_is_hidden_but_routable(ctx):
    """对象形式省略倍率 = 倍率未知 → 不进下游清单，但保留路由归属。

    这正是 `hy4-preview-f` 的处置：它实测会扣积分（非 0 倍率），不该广告给下游；
    但也不能从目录里彻底删掉 —— 归属一旦未知，调用会被不限域随机丢到别的域
    （cn 上同一模型更贵）。所以「隐藏可见性」与「保留归属」必须分开。
    """
    ctx.cfg.realms["global"].models_free_only = True
    ctx.cfg.realms["global"].extra_models = [{"id": "hy4-preview-f"}]
    await ctx.models.list_models("", refresh=True)

    assert "hy4-preview-f" not in [m["id"] for m in ctx.models.cached()["global"]]
    assert "hy4-preview-f" in [m["id"] for m in ctx.models.raw_cached()["global"]]

    ctx.cfg.pool.realm_priority = ["global", "cn"]
    assert ctx.gateway.realm_chain("hy4-preview-f") == ["global"]


# ---------------------------------------------------------------- 域链


async def test_realm_chain_follows_priority(ctx):
    ctx.cfg.pool.realm_priority = ["global", "cn", "zai"]
    await ctx.models.list_models("", refresh=True)
    assert ctx.gateway.realm_chain(MODEL) == ["global", "cn"]
    # hy3 只有 global 提供：没有 cn 兜底可言
    assert ctx.gateway.realm_chain("hy3") == ["global"]


async def test_realm_chain_explicit_prefix_is_not_downgraded(ctx):
    """显式域前缀是调用方的明确指定，不得被兜底改写。"""
    ctx.cfg.pool.realm_priority = ["global", "cn", "zai"]
    await ctx.models.list_models("", refresh=True)
    assert ctx.gateway.realm_chain(MODEL, explicit="cn") == ["cn"]


async def test_realm_chain_empty_priority_keeps_legacy_behaviour(ctx):
    """realm_priority 留空 = 关闭特性，保持「唯一归属才限定域」的旧口径。"""
    await ctx.models.list_models("", refresh=True)
    assert ctx.cfg.pool.realm_priority == []
    assert ctx.gateway.realm_chain(MODEL) == [""]      # 跨域重名 -> 不限域
    assert ctx.gateway.realm_chain("hy3") == ["global"]  # 唯一归属 -> 限定


# ---------------------------------------------------------------- 端到端兜底


def _build_e2e(tmp_path: Path, *, priority, global_accounts: int, global_ok: bool = False):
    """global 上游对该模型恒 429（6004），cn 上游正常。"""
    cfg = make_test_config(tmp_path)
    cfg.pool.retry_429_wait = 0
    cfg.pool.retry_429_times = 0          # 不在原地等，直接换号
    cfg.pool.retry_5xx_wait = 0
    cfg.pool.realm_priority = list(priority)
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx

    for i in range(global_accounts):
        ctx.pool.upsert_account(AccountAuth(
            uid=f"g{i}", realm="global", access_token=f"G{i}", kind="oauth"))
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))

    raw = "sk-kp-fallback"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("fb", hash_api_key(raw), raw[:12]),
    )

    # 复刻线上场景：cn 的付费同名模型被免费过滤掉，只在路由目录里保留
    ctx.models._raw["global"] = (0.0, [{"id": MODEL, "credit_factor": 0.0}])
    ctx.models._raw["cn"] = (0.0, [{"id": MODEL, "credit_factor": 0.11}])
    ctx.models._cache["global"] = (0.0, [{"id": MODEL, "credit_factor": 0.0}])
    ctx.models._cache["cn"] = (0.0, [])

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith(CHAT_SUFFIX):
            return httpx.Response(200, json={"code": 0, "data": {}})
        calls.append(request.url.host)
        if "workbuddy" in request.url.host and not global_ok:
            return httpx.Response(429, content=MODEL_LIMIT_BODY.encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=OK_SSE.encode(),
                              headers={"content-type": "text/event-stream"})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    return app, ctx, calls


BODY = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
AUTH = {"Authorization": "Bearer sk-kp-fallback"}


def test_global_model_limit_falls_back_to_cn(tmp_path: Path):
    """global 的单账号对该模型限流 → 自动降级到 cn 同名模型，而非给下游 429。"""
    app, _, calls = _build_e2e(tmp_path, priority=["global", "cn"], global_accounts=1)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=BODY, headers=AUTH)
        assert resp.status_code == 200, resp.text
    assert calls[0] == "www.workbuddy.ai"          # 先打优先级最高的 global
    assert "copilot.tencent.com" in calls          # 再由 cn 兜底成功


def test_realm_priority_prefers_global_when_both_usable(tmp_path: Path):
    """global 能用时只走 global，不该去动 cn 的付费额度。"""
    app, _, calls = _build_e2e(
        tmp_path, priority=["global", "cn"], global_accounts=1, global_ok=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=BODY, headers=AUTH)
        assert resp.status_code == 200, resp.text
    assert calls == ["www.workbuddy.ai"]


def test_realm_budget_exhausted_still_reaches_fallback(tmp_path: Path):
    """global 账号多于单域尝试预算时，预算耗尽也必须降级到 cn，不能就此放弃。"""
    n = C.MAX_ACCOUNT_ATTEMPTS + 2                 # 账号数多于预算，确保走预算耗尽的路径
    app, _, calls = _build_e2e(tmp_path, priority=["global", "cn"], global_accounts=n)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=BODY, headers=AUTH)
        assert resp.status_code == 200, resp.text
    # 单域最多 attempts + 1 次尝试（attempts = max_retries），随后切域
    assert calls.count("www.workbuddy.ai") == C.MAX_ACCOUNT_ATTEMPTS + 1
    assert calls[-1] == "copilot.tencent.com"


def test_no_priority_means_no_cross_realm_fallback(tmp_path: Path):
    """未开 realm_priority 时保持旧行为：限定 global 后失败，不会去找 cn 兜底。"""
    app, ctx, calls = _build_e2e(tmp_path, priority=[], global_accounts=1)
    ctx.models._raw["cn"] = (0.0, [])      # 该模型只有 global 提供
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=BODY, headers=AUTH)
        assert resp.status_code == 429
    assert calls and all(h == "www.workbuddy.ai" for h in calls)


def test_fallback_success_is_accounted_as_success(tmp_path: Path):
    """换号/跨域兜底后成功，必须按「成功」记账，不能被残留错误态污染。

    回归：`_note_error` 会在失败尝试里写 self.status / self.error，而成功路径
    原先不重置它们 —— 结果兜底成功的请求被记成 429，且 `not self.error` 不成立
    导致 `record_success` 被跳过（调用数 / 成本账本 / 积分全部漏记）。
    """
    app, ctx, _ = _build_e2e(tmp_path, priority=["global", "cn"], global_accounts=1)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=BODY, headers=AUTH)
        assert resp.status_code == 200, resp.text

        # 断言必须在 lifespan 内：退出 with 块会关闭数据库连接
        row = ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")[0]
        assert row["status"] == 200, f"兜底成功却记成 {row['status']}: {row['error']}"
        assert row["error"] == ""
        assert row["realm"] == "cn"        # 实际服务该请求的是 cn 账号
        # 成功必须回写账号计数（record_success 真的跑了）
        assert ctx.pool.get("c1").total_calls == 1
        assert ctx.pool.get("g0").total_calls == 0
