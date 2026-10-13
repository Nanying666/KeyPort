"""模型别名映射、密钥自查端点、护栏的端到端集成。

三件事都是「下游体验」层面的补齐（参考 workbuddy-manager / workbuddy2api-hub）：

- **别名映射**：客户端硬编码的模型名（`gpt-4o`）不必改，网关在选号前换掉。
- **`GET /v1/usage`**：密钥自查已用/剩余配额，**不产生上游调用**。
- **护栏集成**：被护栏挡住的账号在真实请求路径上被跳过，且不误罚账号。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.config import ModelsConfig
from keyport.security import hash_api_key, hash_password
from keyport.upstream.base import AccountAuth

ZCODE_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"

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

RAW_KEY = "sk-kp-guard-test"


# ---- 别名映射（纯函数） ----


def test_alias_resolves_known_name():
    cfg = ModelsConfig(aliases={"gpt-4o": "hy4-preview-f"})
    assert cfg.resolve("gpt-4o") == "hy4-preview-f"


def test_alias_is_case_insensitive():
    cfg = ModelsConfig(aliases={"GPT-4o": "hy4-preview-f"})
    assert cfg.resolve("gpt-4o") == "hy4-preview-f"
    assert cfg.resolve("GPT-4O") == "hy4-preview-f"


def test_alias_passthrough_when_unmapped():
    cfg = ModelsConfig(aliases={"gpt-4o": "hy4-preview-f"})
    assert cfg.resolve("glm-5.3") == "glm-5.3"


def test_alias_handles_empty_input():
    cfg = ModelsConfig(aliases={"a": "b"})
    assert cfg.resolve("") == ""
    assert ModelsConfig().resolve("anything") == "anything"


def test_alias_strips_whitespace_on_both_sides():
    cfg = ModelsConfig(aliases={"  gpt-4o  ": "  hy4-preview-f  "})
    assert cfg.resolve("gpt-4o") == "hy4-preview-f"


def test_alias_can_target_realm_prefixed_model():
    """别名可以指向带域前缀的目标 —— 映射必须发生在解析域前缀之前。"""
    from keyport.api.gateway import resolve_model_with_alias

    cfg = make_cfg_stub(ModelsConfig(aliases={"fast": "zai:glm-5.3-flash"}))
    realm, bare = resolve_model_with_alias("fast", cfg)
    assert realm == "zai"
    assert bare == "glm-5.3-flash"


def make_cfg_stub(models: ModelsConfig):
    from keyport.config import Config

    cfg = Config()
    cfg.models = models
    return cfg


def test_alias_is_single_hop_only():
    """一层映射：别名指向别名时不再二次解析（避免环与歧义）。"""
    from keyport.api.gateway import resolve_model_with_alias

    cfg = make_cfg_stub(ModelsConfig(aliases={"a": "b", "b": "c"}))
    _realm, bare = resolve_model_with_alias("a", cfg)
    assert bare == "b"


# ---- 测试夹具 ----


class _Scripted:
    def __init__(self) -> None:
        self.chat_calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/v1/messages", "/chat/completions")):
            self.chat_calls.append({
                "url": str(request.url),
                "body": request.content.decode("utf-8", "replace"),
            })
            return httpx.Response(200, content=OK_SSE.encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"code": 0, "data": {}})


def _build(tmp_path: Path, *, guards=None, aliases=None, accounts=1):
    cfg = make_test_config(tmp_path)
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    if guards is not None:
        cfg.guards = guards
    if aliases is not None:
        cfg.models = ModelsConfig(aliases=aliases)
    app = create_app(cfg)
    ctx = app.state.ctx
    for i in range(accounts):
        ctx.pool.upsert_account(AccountAuth(
            uid=f"zai-{i}", realm="zai", nickname=f"acc{i}",
            access_token=f"JWT-{i}", kind="apikey",
            device_token=f"dev-{i}", base_url=ZCODE_BASE, protocol="anthropic",
        ))
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("g", hash_api_key(RAW_KEY), RAW_KEY[:12]),
    )
    scripted = _Scripted()

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(scripted))

    ctx.client.client = _mock  # type: ignore[method-assign]
    return app, ctx, scripted


AUTH = {"Authorization": f"Bearer {RAW_KEY}"}
MSG = {"model": "glm-5.3-flash", "max_tokens": 16,
       "messages": [{"role": "user", "content": "hi"}]}


# ---- 别名在真实请求路径上生效 ----


def test_alias_rewrites_model_sent_upstream(tmp_path: Path):
    app, ctx, scripted = _build(tmp_path, aliases={"gpt-4o": "glm-5.3-flash"})
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={**MSG, "model": "gpt-4o"}, headers=AUTH)
        assert resp.status_code == 200, resp.text
    sent = json.loads(scripted.chat_calls[0]["body"])
    assert sent["model"] == "glm-5.3-flash"


def test_alias_recorded_as_upstream_model_in_log(tmp_path: Path):
    """日志记的是**实际上游模型**（排障时不该看到一个不存在的名字）。"""
    app, ctx, scripted = _build(tmp_path, aliases={"gpt-4o": "glm-5.3-flash"})
    with TestClient(app) as tc:
        tc.post("/v1/chat/completions", json={**MSG, "model": "gpt-4o"}, headers=AUTH)
        # 必须在 lifespan 内查询：TestClient 退出时会关掉 DB 连接
        row = ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")[0]
        assert row["model"] == "glm-5.3-flash"


# ---- GET /v1/usage ----


def test_key_usage_reports_quota(tmp_path: Path):
    app, ctx, scripted = _build(tmp_path)
    ctx.db.execute("UPDATE api_keys SET quota_tokens=1000, used_tokens=250, "
                   "quota_credits=10, used_credits=2.5 WHERE id=1")
    with TestClient(app) as tc:
        resp = tc.get("/v1/usage", headers=AUTH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
    assert body["tokens"]["quota"] == 1000
    assert body["tokens"]["used"] == 250
    assert body["tokens"]["remaining"] == 750
    assert body["tokens"]["exhausted"] is False
    assert body["credits"]["remaining"] == pytest.approx(7.5)
    assert body["key"]["name"] == "g"


def test_key_usage_unlimited_quota_reports_null_remaining(tmp_path: Path):
    """quota=0 表示不限：remaining 必须是 null，不能编造一个数字。"""
    app, ctx, scripted = _build(tmp_path)
    with TestClient(app) as tc:
        body = tc.get("/v1/usage", headers=AUTH).json()
    assert body["tokens"]["quota"] == 0
    assert body["tokens"]["remaining"] is None
    assert body["credits"]["remaining"] is None
    assert body["tokens"]["exhausted"] is False


def test_exhausted_key_rejected_before_usage(tmp_path: Path):
    """配额用尽的密钥在鉴权阶段就被拒（429），与其余 /v1 端点一致。

    自查端点的用途是**用尽前**做预算监控；真用尽时 429 的文案已说明原因，
    不需要也不应该为它开一个绕过配额门的口子。
    """
    app, ctx, scripted = _build(tmp_path)
    ctx.db.execute("UPDATE api_keys SET quota_tokens=100, used_tokens=100 WHERE id=1")
    with TestClient(app) as tc:
        resp = tc.get("/v1/usage", headers=AUTH)
        assert resp.status_code == 429
        assert "配额" in resp.json()["error"]["message"]


def test_key_usage_lists_model_restriction(tmp_path: Path):
    app, ctx, scripted = _build(tmp_path)
    ctx.db.execute("UPDATE api_keys SET model_allow=? WHERE id=1",
                   (json.dumps(["glm-5.3"]),))
    with TestClient(app) as tc:
        body = tc.get("/v1/usage", headers=AUTH).json()
    assert body["models"]["allow"] == ["glm-5.3"]
    assert body["models"]["restricted"] is True


def test_key_usage_requires_key(tmp_path: Path):
    app, ctx, scripted = _build(tmp_path)
    with TestClient(app) as tc:
        assert tc.get("/v1/usage").status_code == 401
        assert tc.get("/v1/usage",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_key_usage_makes_no_upstream_call(tmp_path: Path):
    """核心承诺：自查端点**不产生任何上游调用**。"""
    app, ctx, scripted = _build(tmp_path)
    with TestClient(app) as tc:
        tc.get("/v1/usage", headers=AUTH)
    assert scripted.chat_calls == []


# ---- 护栏在请求路径上生效 ----


def test_guard_blocks_account_and_returns_503(tmp_path: Path):
    """唯一的账号被护栏挡住 → 无可用账号（503），且账号不被误罚。"""
    from keyport.config import GuardConfig

    app, ctx, scripted = _build(
        tmp_path, guards=GuardConfig(reserve_credits=9999))
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 503
        entry = ctx.pool.get("zai-0")
        assert entry.enabled is True
        assert entry.cooldown_until == 0
        assert entry.consecutive_failures == 0
    assert scripted.chat_calls == []


def test_guard_rotates_to_another_account(tmp_path: Path):
    """有多个账号时，被挡的那个跳过、请求落到另一个。"""
    from keyport.config import GuardConfig

    app, ctx, scripted = _build(tmp_path, guards=GuardConfig(reserve_credits=100),
                                accounts=2)
    # zai-0 余额不足，zai-1 健康（update_credits 会落库，护栏据此判定）
    ctx.pool.update_credits("zai-0", 50)
    ctx.pool.update_credits("zai-1", 5000)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text
        row = ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")[0]
        assert row["account_uid"] == "zai-1"


def test_guard_off_lets_everything_through(tmp_path: Path):
    """默认（全 0）行为与装护栏前完全一致。"""
    from keyport.config import GuardConfig

    app, ctx, scripted = _build(tmp_path, guards=GuardConfig())
    ctx.pool.update_credits("zai-0", 0)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert resp.status_code == 200, resp.text


def test_guard_skips_only_offending_model(tmp_path: Path):
    """按模型护栏：被挡模型换号，其他模型同账号照常。

    计数由 `refresh_guards` 从 `request_logs` 重算，所以必须**写真实日志行**
    而不是直接改内存计数 —— 后者会被下一次刷新覆盖。
    """
    from keyport.config import GuardConfig

    app, ctx, scripted = _build(
        tmp_path, guards=GuardConfig(model_daily_token_limit=100))
    now = int(time.time())
    ctx.db.execute(
        "INSERT INTO request_logs(ts, account_uid, model, total_tokens) VALUES(?,?,?,?)",
        (now, "zai-0", "glm-5.3-flash", 500))

    # 全程保持同一个 lifespan：TestClient 退出会关掉 DB 连接
    with TestClient(app) as tc:
        blocked = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert blocked.status_code == 503, blocked.text

        # 把那 500 token 挪到别的模型名下 → 同一账号对 glm-5.3-flash 重新可用
        ctx.db.execute(
            "UPDATE request_logs SET model='other-model' WHERE account_uid='zai-0'")
        ctx.pool._usage_cache.invalidate()
        allowed = tc.post("/v1/chat/completions", json=MSG, headers=AUTH)
        assert allowed.status_code == 200, allowed.text


def test_guard_refresh_skips_db_when_all_off(tmp_path: Path):
    """护栏全关时不查库（默认配置下这条路径零开销）。"""
    from keyport.config import GuardConfig

    app, ctx, scripted = _build(tmp_path, guards=GuardConfig())
    calls: list[int] = []
    inner = ctx.pool._usage_cache

    class _Spy:
        def get(self, db, *, refresh=False):
            calls.append(1)
            return inner.get(db, refresh=refresh)

    ctx.pool._usage_cache = _Spy()
    ctx.pool.refresh_guards(force=True)
    assert calls == []


# ---- 管理台设置热改 ----


@pytest.fixture()
def admin(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="zai-1", realm="zai", nickname="acc1", access_token="JWT",
        kind="apikey", device_token="dev", base_url=ZCODE_BASE,
        protocol="anthropic"))

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(_scripted_admin))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        resp = tc.post("/api/login",
                       json={"username": "admin", "password": "pw-12345678"})
        assert resp.status_code == 200
        tc.headers.update({"x-kp-session": tc.cookies.get("kp_session", "")})
        yield tc


def _scripted_admin(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": {}})


def test_settings_hot_update_guards(admin: TestClient):
    resp = admin.patch("/api/settings", json={"guards": {
        "reserve_credits": 50, "daily_token_limit": 100000,
        "daily_credit_limit": 20, "model_daily_token_limit": 5000,
    }})
    assert resp.status_code == 200, resp.text
    applied = resp.json()["applied"]
    for key in ("reserve_credits", "daily_token_limit",
                "daily_credit_limit", "model_daily_token_limit"):
        assert f"guards.{key}" in applied
    cfg = admin.app.state.ctx.cfg.guards
    assert cfg.reserve_credits == 50
    assert cfg.daily_token_limit == 100000
    assert cfg.model_daily_token_limit == 5000


def test_settings_rejects_negative_guard(admin: TestClient):
    resp = admin.patch("/api/settings", json={"guards": {"reserve_credits": -1}})
    assert resp.status_code == 400


def test_settings_rejects_bad_guard_value(admin: TestClient):
    resp = admin.patch("/api/settings", json={"guards": {"reserve_credits": "abc"}})
    assert resp.status_code == 400


def test_settings_hot_update_realm_overrides(admin: TestClient):
    resp = admin.patch("/api/settings", json={"guards": {
        "daily_token_limit": 1000,
        "realm_overrides": {"zai": {"daily_token_limit": 2000}},
    }})
    assert resp.status_code == 200, resp.text
    assert "guards.realm_overrides" in resp.json()["applied"]
    overrides = admin.app.state.ctx.cfg.guards.realm_overrides
    assert overrides["zai"]["daily_token_limit"] == 2000


def test_settings_hot_update_model_aliases(admin: TestClient):
    resp = admin.patch("/api/settings",
                       json={"models": {"aliases": {"gpt-4o": "glm-5.3"}}})
    assert resp.status_code == 200, resp.text
    assert "models.aliases" in resp.json()["applied"]
    assert admin.app.state.ctx.cfg.models.resolve("gpt-4o") == "glm-5.3"


def test_settings_rejects_bad_aliases_shape(admin: TestClient):
    resp = admin.patch("/api/settings", json={"models": {"aliases": "nope"}})
    assert resp.status_code == 400


def test_guards_status_endpoint(admin: TestClient):
    admin.patch("/api/settings", json={"guards": {"reserve_credits": 50}})
    resp = admin.get("/api/guards")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limits"]["reserve_credits"] == 50
    assert body["pool"]["enabled"] is True
    assert body["pool"]["accounts"][0]["uid"] == "zai-1"


def test_guards_status_requires_admin(tmp_path: Path):
    """未登录必须 401（用一个没有登录态的独立 client，避免继承 fixture 的会话头）。"""
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    app = create_app(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(_scripted_admin))

    app.state.ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        assert tc.get("/api/guards").status_code == 401


def test_settings_exposes_guards_and_models(admin: TestClient):
    cfg = admin.get("/api/settings").json()["config"]
    assert "guards" in cfg and "models" in cfg
    assert "reserve_credits" in cfg["guards"]


def test_guard_settings_do_not_touch_real_config(admin: TestClient):
    real = Path(__file__).resolve().parent.parent / "config.json"
    before = real.read_bytes() if real.is_file() else None
    admin.patch("/api/settings", json={
        "guards": {"reserve_credits": 77},
        "models": {"aliases": {"x": "y"}},
    })
    after = real.read_bytes() if real.is_file() else None
    assert after == before, "测试写坏了项目根的 config.json"
