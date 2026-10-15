"""网关核心：选号 → 改写 → 上游调用 → 故障转移 → 用量落库。

协议转换由 keyport.protocol 负责；本模块只处理「OpenAI chat 协议」的
上游交互与账号池治理。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

from ..config import Config
from ..pool.manager import AccountPool
from ..upstream import constants as C
from ..upstream.base import AccountAuth, ChatMeta, ErrorKind, UpstreamError
from ..upstream.client import UpstreamClient, is_zcode_plan
from ..upstream.payload import prepare_body
from ..upstream.prompt import load_prompt_text
from ..upstream.thinking import supported_efforts

if TYPE_CHECKING:
    from ..auth.refresh import TokenRefresher

# 模型名前缀 → 强制 realm（双域独立路由）
REALM_PREFIXES = ("cn:", "global:", "zai:")

# Codex 长上下文压缩阈值（token 估算）
COMPRESS_TOKENS = 200_000

# 验证码挑战同账号原地重试上限（端点级限制，换号同样被拦）
MAX_CAPTCHA_RETRIES = C.MAX_CAPTCHA_RETRIES


def _is_permanent_429(exc: UpstreamError) -> bool:
    """429 是否承载「Key 无资源包」的永久态（原地等待重试无意义）。

    实测 api.z.ai 对无余额的 Key 秒回 429 + code 1113。等待重试只会把客户端
    吊死在 `retry_429_times × wait` 里，应快速换号 / 换通道。
    """
    text = (exc.message or "") + (exc.body or "")
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in C.PERMANENT_429_MARKERS)


def resolve_model(model: str, default_realm: str = "") -> tuple[str, str]:
    """解析 `cn:glm-4.6` 形式的域前缀；返回 (realm, 裸模型名)。"""
    raw = (model or "").strip()
    for prefix in REALM_PREFIXES:
        if raw.lower().startswith(prefix):
            return prefix[:-1], raw[len(prefix):].strip()
    return default_realm, raw


def resolve_model_with_alias(model: str, cfg: Config, default_realm: str = "") -> tuple[str, str]:
    """先按别名映射，再解析域前缀。

    顺序很重要：别名可以指向带域前缀的目标（`"fast": "zai:glm-5.3-flash"`），
    所以映射必须在解析之前完成。
    """
    models_cfg = getattr(cfg, "models", None)
    mapped = models_cfg.resolve(model) if models_cfg is not None else model
    return resolve_model(mapped, default_realm)


async def _decode_lines(source: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """把字节行流转成文本行流（Anthropic 转换器产出 bytes，下游按 str 消费）。"""
    async for raw in source:
        if isinstance(raw, bytes):
            yield raw.decode("utf-8", errors="replace")
        else:
            yield raw


@dataclass
class ChatParams:
    """一次 chat 请求的入参。"""

    body: dict[str, Any]
    model: str = ""
    realm: str = ""
    protocol: str = "chat"          # chat / responses / messages
    client_ip: str = ""
    conversation_id: str = ""
    stream: bool = True
    # 单请求内换号上限（超过即 503）。默认取上游常量收口值，
    # 调用方可覆盖（测试常调小以免用例被多轮重试拖慢）。
    max_retries: int = C.MAX_ACCOUNT_ATTEMPTS
    key_id: int = 0
    key_name: str = ""


class ChatSession:
    """已建立的上游流式会话（含故障转移与用量统计）。"""

    def __init__(self, gateway: "Gateway", params: ChatParams):
        self.gateway = gateway
        self.params = params
        self.resp: httpx.Response | None = None
        self.auth: AccountAuth | None = None
        self.uid: str = ""
        self.retries = 0
        self.started_at = time.time()
        self.ttfb_ms = 0
        self.first_byte_at = 0.0
        self.error: str = ""
        self.status = 200
        self.usage: dict[str, Any] = {}
        self._tried: set[str] = set()
        self._holding = False
        # 原始入站请求体快照：每次换号都要从它重新改写（改写会就地覆写
        # self.params.body，若不复位则重试时会在已改写结果上二次改写）。
        self._origin_body: dict[str, Any] = dict(params.body)
        # Anthropic 原生上游：下游协议不是 anthropic 时需要把上游事件转回 OpenAI chunk
        self.anthropic_upstream = False

    # ---- 建立 ----

    def _build_anthropic_body(self, prompt_text: str) -> bytes:
        """构造 Anthropic Messages 请求体（上游为 Anthropic 原生时使用）。

        入站已统一为 OpenAI chat 形态，这里转成 Anthropic 形态；system 提示词
        替换与指纹脱敏仍在网关侧生效。

        历史 bug：这里曾调用 `request_to_openai`（Anthropic→OpenAI 方向），
        方向反了 —— 工具名全变空串、assistant.tool_calls 丢失、tool 消息丢
        tool_call_id，上游直接 400。必须用 `openai_to_anthropic_request`。
        """
        from ..protocol import anthropic as anthropic_proto

        body = dict(self.params.body)
        body.setdefault("max_tokens", 8192)  # Anthropic 必填
        payload = anthropic_proto.openai_to_anthropic_request(
            body, model_override=self.params.model)
        payload["stream"] = True  # 上游 Anthropic 端点必须流式，非流式由网关侧聚合
        messages = payload.get("messages") or []

        # Anthropic 的 system 是顶层字段：替换模式下用配置文本，否则保留原 system
        from ..upstream.prompt import MODE_CUSTOM

        mode = self.gateway.cfg.prompt.mode
        if mode == MODE_CUSTOM and prompt_text:
            payload["system"] = prompt_text
        elif mode == "append" and prompt_text:
            existing = payload.get("system") or ""
            payload["system"] = f"{existing}\n\n{prompt_text}" if existing else prompt_text

        payload["messages"] = messages
        if self.gateway.cfg.features.sanitize_blacklist_fingerprints:
            from ..upstream.payload import sanitize_fingerprints

            sanitize_fingerprints(payload)
        self.params.body = payload
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def start(self) -> "ChatSession":
        """选号 + 改写 + 建连；失败按错误语义重试 / 换号，直到成功或候选耗尽。

        错误处置（参数见 config.pool，均可用环境变量调整）：

        - **验证码挑战（CAPTCHA）**：同账号原地换码重试，最多
          `MAX_CAPTCHA_RETRIES` 次；不冷却账号（端点级限制，换号同样被拦）。
        - **429 频控**：账号**不冷却**，按上游 Retry-After（封顶
          `retry_429_wait_max`）原地等待重试；等待期间释放并发槽。耗尽后换号且
          账号保持可用。Plan 通道耗尽且同账号有回退 Key 时切回退通道。
        - **风控（3012 / unusual activity）**：指数退避冷却（累计升级禁用），
          不原地重试（避免对风控账号持续施压）；有回退 Key 时切回退通道。
        - **5xx 等一般错误**：原地重试 `retry_5xx_times` 次；耗尽后账号冷却
          `cooling_seconds` 并换号。
        - **额度耗尽 / 鉴权失效**：换号；有回退 Key 时先试回退通道。
        - **跨域兜底**：`pool.realm_priority` 非空时，先只在优先级最高的域里选号；
          该域账号全部不可服务（冷却 / 模型级限流 / 本域尝试预算耗尽）才降级到
          下一个域兜底。显式域前缀（`cn:xxx`）不参与兜底。
        """
        gw = self.gateway
        cfg = gw.cfg
        pool_cfg = cfg.pool
        realm, bare_model = resolve_model_with_alias(
            self.params.model, cfg, self.params.realm)
        self.params.model = bare_model
        # 选号域链（优先 → 兜底）。无域前缀时：模型只在一个域提供就限定该域，
        # 多个域都能提供则按 pool.realm_priority 给出「先用哪个域、打不动再降级到哪」
        chain = gw.realm_chain(bare_model, explicit=realm)
        self.params.realm = chain[0]

        prompt_text = load_prompt_text(
            cfg.prompt.mode, cfg.prompt.file, cfg.prompt.default_text
        )
        compress_max = COMPRESS_TOKENS if (
            cfg.features.compress_long_context and self.params.protocol == "responses"
        ) else 0

        last_err: UpstreamError | None = None
        attempts = max(1, self.params.max_retries)
        chain_idx = 0
        realm_spent = 0

        def _next_realm() -> bool:
            """当前域打不动了 → 降级到链上的下一个域兜底；无下一个域返回 False。"""
            nonlocal chain_idx, realm_spent
            if chain_idx + 1 >= len(chain):
                return False
            chain_idx += 1
            realm_spent = 0
            self.params.realm = chain[chain_idx]
            # 换域换了一套账号：旧域试过的 uid 对本域没有意义，清掉让本域完整重试
            self._tried.clear()
            return True

        while True:
            # 本域尝试预算耗尽（典型场景：该模型把本域账号全打限流了）→ 降级兜底
            if realm_spent > attempts:
                if not _next_realm():
                    break
                continue
            entry = gw.pool.pick(
                realm=chain[chain_idx], model=self.params.model,
                conversation_id=self.params.conversation_id, tried=self._tried,
            )
            if entry is None:
                # 本域已无健康/有容量的账号：换下一个域兜底，没有则结束
                if not _next_realm():
                    break
                continue
            self._tried.add(entry.uid)
            # 并发槽由 _attempt 全权持有与释放（内部会因等待类重试反复让出 / 取回），
            # 这里不参与，避免「未持槽却释放」把别人的槽位算漏。
            if not gw.pool.acquire(entry.uid):
                continue  # 满并发：不计入重试轮数，直接换下一个
            try:
                auth = await gw.refresher.ensure_valid(gw.pool.auth_for(entry))
            except UpstreamError as exc:
                gw.pool.release(entry.uid)
                last_err = exc
                gw.pool.record_error(entry.uid, exc, self.params.conversation_id)
                self._note_error(exc)
                if not exc.should_failover:
                    break
                self.retries += 1
                realm_spent += 1
                continue

            try:
                await self._attempt(entry, auth, prompt_text, compress_max, pool_cfg)
                return self
            except UpstreamError as exc:
                last_err = exc
                self._note_error(exc)
                if not exc.should_failover:
                    break
                self.retries += 1
                realm_spent += 1

        if last_err is not None:
            raise last_err
        raise UpstreamError(ErrorKind.UNKNOWN, 503, "no available account")

    def _note_error(self, exc: UpstreamError) -> None:
        self.error = f"{exc.kind.value}: {exc.message}"
        self.status = exc.status or 502

    async def _attempt(
        self, entry: Any, auth: AccountAuth, prompt_text: str, compress_max: int,
        pool_cfg: Any,
    ) -> None:
        """用单个账号尝试建立上游流（含通道级重试与回退切换）。

        **并发槽生命周期由本方法独占**：进入时已持有 1 个槽；等待类重试会先让出
        再取回；无论成功、失败还是异常，退出时槽位恰好归零或转交本会话
        （成功时 `_holding=True`，由 `close()` 释放）。
        """
        gw = self.gateway
        captcha_retries = 0
        retries_429 = 0
        retries_5xx = 0
        use_fallback = False          # 请求级通道决策（不改账号状态）
        needs_captcha = is_zcode_plan(auth)
        holding = True

        try:
            while True:
                current = auth.as_fallback() if (use_fallback and auth.has_fallback()) else auth
                self.anthropic_upstream = current.is_anthropic()
                body_bytes = self._rewrite_body(prompt_text, entry.realm, compress_max)
                meta = ChatMeta(
                    conversation_id=self.params.conversation_id,
                    client_ip=self.params.client_ip,
                )
                try:
                    resp = await gw.client.open_chat(
                        auth, body_bytes, meta, force_fallback=use_fallback
                    )
                except UpstreamError as exc:
                    (
                        captcha_retries, retries_429, retries_5xx, use_fallback,
                        action, holding,
                    ) = await self._handle_error(
                        entry, auth, exc, captcha_retries, retries_429, retries_5xx,
                        use_fallback, needs_captcha, pool_cfg, holding,
                    )
                    if action == "retry":
                        continue
                    raise

                self.resp = resp
                self.auth = current
                self.uid = entry.uid
                # 之前几次换号的失败只是**过程**：本次已经拿到上游流。
                # 必须清掉残留的错误态，否则 finish() 会拿旧错误收尾 ——
                # 状态码记成 429、error 留着旧文案，并因 `not self.error` 不成立
                # 而**跳过 record_success**（调用数 / 账本 / 积分全部漏记）。
                # 跨域兜底与普通换号都会走到这里，所以这不是边缘情况。
                self.error = ""
                self.status = int(getattr(resp, "status_code", 200) or 200)
                self._holding = True   # 槽位转交本会话，由 close() 释放
                holding = False
                gw.pool.record_success_meta(entry.uid, fallback=use_fallback)
                return
        finally:
            if holding:
                gw.pool.release(entry.uid)

    async def _handle_error(
        self, entry: Any, auth: AccountAuth, exc: UpstreamError,
        captcha_retries: int, retries_429: int, retries_5xx: int,
        use_fallback: bool, needs_captcha: bool, pool_cfg: Any, holding: bool,
    ) -> tuple[int, int, int, bool, str, bool]:
        """按错误语义决定「原地重试」还是「上抛换号」。

        返回 `(captcha_retries, retries_429, retries_5xx, use_fallback, action, holding)`。
        `action == "retry"` 表示同账号继续尝试；否则由调用方上抛换号。
        `holding` 回传等待类重试后的槽位归属（取回失败则已让出）。
        """
        gw = self.gateway
        pool = gw.pool
        kind = exc.kind

        def _rate_limited_failover() -> tuple[int, int, int, bool, str, bool]:
            """429 换号：只留痕，不冷却、不计连败（账号保持可用）。"""
            pool.record_rate_limited(entry.uid, exc)
            return captcha_retries, retries_429, retries_5xx, use_fallback, "failover", holding

        def _failover() -> tuple[int, int, int, bool, str, bool]:
            pool.record_error(entry.uid, exc, self.params.conversation_id)
            return captcha_retries, retries_429, retries_5xx, use_fallback, "failover", holding

        # 风控 / 鉴权失效 / 额度耗尽：同账号有回退 Key 时先切回退通道
        if kind in (ErrorKind.RISK, ErrorKind.AUTH, ErrorKind.PAYMENT) and \
                needs_captcha and not use_fallback and auth.has_fallback():
            action = pool.record_error(entry.uid, exc, self.params.conversation_id)
            if kind == ErrorKind.RISK:
                self._maybe_rotate_on_risk(entry, action)
            return captcha_retries, 0, retries_5xx, True, "retry", holding

        # 验证码挑战：同账号换码重建请求（不改账号状态）
        if kind == ErrorKind.CAPTCHA and needs_captcha and not use_fallback:
            captcha_retries += 1
            # 被拒的参数必须出池：留在池里只会被下一个请求取到、再换回一次 3007
            captcha = gw.client.captcha
            if captcha is not None:
                try:
                    captcha.invalidate()
                except Exception:  # noqa: BLE001 - 清池失败不该影响重试
                    pass
            if captcha_retries < MAX_CAPTCHA_RETRIES:
                pool.record_error(entry.uid, exc, self.params.conversation_id)
                return captcha_retries, retries_429, retries_5xx, use_fallback, "retry", holding

        # 429 频控：不冷却账号，按 Retry-After 原地等待重试；耗尽后换号
        if kind == ErrorKind.RATE_LIMIT:
            if _is_permanent_429(exc):
                # 1113「无余额/无资源包」是 Key 的永久状态，等待重试毫无意义
                if needs_captcha and not use_fallback and auth.has_fallback():
                    pool.record_rate_limited(entry.uid, exc)
                    return captcha_retries, 0, retries_5xx, True, "retry", holding
                return _rate_limited_failover()
            if retries_429 < max(0, pool_cfg.retry_429_times):
                wait = min(
                    int(exc.retry_after) or pool_cfg.retry_429_wait,
                    pool_cfg.retry_429_wait_max,
                )
                retries_429 += 1
                # 等待期间让出并发槽：流式请求可占槽数分钟，占着等会冻住账号
                pool.release(entry.uid)
                holding = False
                await asyncio.sleep(max(0, wait))
                holding = pool.acquire(entry.uid)
                if not holding:
                    return _rate_limited_failover()
                return captcha_retries, retries_429, retries_5xx, use_fallback, "retry", holding
            if needs_captcha and not use_fallback and auth.has_fallback():
                pool.record_rate_limited(entry.uid, exc)
                return captcha_retries, 0, retries_5xx, True, "retry", holding
            return _rate_limited_failover()

        # 5xx / 网络：原地重试，耗尽后冷却换号
        if kind in (ErrorKind.SERVER, ErrorKind.NETWORK):
            if retries_5xx < max(0, pool_cfg.retry_5xx_times):
                retries_5xx += 1
                pool.release(entry.uid)
                holding = False
                await asyncio.sleep(max(0, pool_cfg.retry_5xx_wait))
                holding = pool.acquire(entry.uid)
                if not holding:
                    return _failover()
                return captcha_retries, retries_429, retries_5xx, use_fallback, "retry", holding
            # 重试耗尽：5xx 属账号侧故障 → 冷却换号（与 429 的关键差异）
            pool.cool_down(
                entry.uid, pool_cfg.cooling_seconds,
                f"上游 HTTP {exc.status} 重试 {retries_5xx} 次耗尽，冷却",
            )
            return captcha_retries, retries_429, retries_5xx, use_fallback, "failover", holding

        # 其余（含重试耗尽、风控无回退、额度耗尽）：按语义落状态并换号
        action = pool.record_error(entry.uid, exc, self.params.conversation_id)
        self._maybe_rotate_on_risk(entry, action)
        return captcha_retries, retries_429, retries_5xx, use_fallback, "failover", holding

    def _maybe_rotate_on_risk(self, entry: Any, action: str) -> None:
        """风控升级禁用时自动换发设备指纹（新 SKU + 新 device_mid）并补跑安装序。

        「风控后换设备重生」语义：账号重新启用时已是全新身份，避免继续用
        被标记的设备 ID 打上游。
        """
        if not action.startswith("disabled:risk_control"):
            return
        cfg = self.gateway.cfg
        if not cfg.pool.risk_auto_rotate:
            return
        profile = self.gateway.pool.rotate_fingerprint(entry.uid)
        if profile is None:
            return
        entry.installed_at = 0
        self._schedule_install(entry)

    def _schedule_install(self, entry: Any) -> None:
        """后台补跑该账号的安装序（幂等；不阻塞请求路径）。"""
        if not self.gateway.cfg.install.enabled:
            return
        try:
            from ..upstream.install import schedule_install

            auth = self.gateway.pool.auth_for(entry)
            auth.installed_at = 0
            schedule_install(
                self.gateway.client, auth,
                on_done=lambda uid, at: self.gateway.pool.mark_installed(uid, at),
            )
        except Exception:  # noqa: BLE001 - 安装序失败不影响请求
            pass

    def _rewrite_body(self, prompt_text: str, account_realm: str,
                      compress_max: int) -> bytes:
        """按实际账号域改写请求体（每次换号都重新改写，域随账号变化）。

        从原始入站快照出发，避免在已改写结果上二次改写（换号重试时）。
        """
        cfg = self.gateway.cfg
        self.params.body = dict(self._origin_body)
        if self.anthropic_upstream:
            return self._build_anthropic_body(prompt_text)
        return prepare_body(
            json.dumps(self.params.body, ensure_ascii=False).encode("utf-8"),
            sanitize=cfg.features.sanitize_blacklist_fingerprints,
            prompt_mode=cfg.prompt.mode,
            prompt_text=prompt_text,
            efforts=supported_efforts(self.params.model),
            is_global=(account_realm == "global"),
            compress_max_tokens=compress_max,
        )

    # ---- 读取 ----

    async def iter_lines(self) -> AsyncIterator[str]:
        """产出上游 SSE 行，并记录首字延迟（TTFB）与 usage。

        首字 = 从发起上游请求到收到第一个含正文的 delta，反映上游响应快慢。
        usage 在此统一捕获，使三种协议（chat / responses / messages）的流式路径
        都能落库用量，无需各自重复解析。
        """
        assert self.resp is not None
        source: AsyncIterator[str] = self.resp.aiter_lines()
        if self.anthropic_upstream:
            # 上游是 Anthropic 原生：转成 OpenAI chunk 流，下游两条路径无需感知差异
            from ..protocol.anthropic import openai_chunks_from_anthropic

            source = _decode_lines(openai_chunks_from_anthropic(
                self.resp.aiter_lines(), self.params.model, self._completion_id()
            ))
        try:
            async for line in source:
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload and payload != "[DONE]":
                        if not self.first_byte_at:
                            self.first_byte_at = time.time()
                            self.ttfb_ms = int((self.first_byte_at - self.started_at) * 1000)
                        if '"usage"' in payload:
                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                chunk = None
                            if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                                self.usage = chunk["usage"]
                yield line
        except httpx.HTTPError as exc:
            self.error = f"stream_error: {exc}"
            if self.uid:
                self.gateway.pool.record_error(
                    self.uid,
                    UpstreamError(ErrorKind.NETWORK, 0, str(exc)),
                    self.params.conversation_id,
                )
        except RuntimeError as exc:
            # 上游在流中途报错（如 Anthropic error 事件）
            self.error = f"upstream_error: {exc}"

    def _completion_id(self) -> str:
        return f"chatcmpl-{int(self.started_at * 1000)}"

    async def close(self) -> None:
        if self.resp is not None:
            await self.resp.aclose()
            self.resp = None
        if self._holding:
            self.gateway.pool.release(self.uid)
            self._holding = False

    # ---- 收尾 ----

    def total_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)

    def finish(self, usage: dict[str, Any] | None, credits: float = 0.0) -> None:
        """成功收尾：回写池计数 + 用量落库。

        usage 为 None/空时回落到流式读取期间捕获的 self.usage，
        保证三种协议的流式路径都能正确记账。
        """
        u = usage or self.usage or {}
        tokens = int(u.get("total_tokens") or 0)
        if not credits:
            credits = extract_credits(u)
        if self.auth is not None and not self.error:
            self.gateway.pool.record_success(
                self.uid, credits_used=credits, realm=self.params.realm,
                model=self.params.model, tokens=tokens,
            )
        self.gateway.log_request(
            params=self.params, uid=self.uid, status=self.status,
            ttfb_ms=self.ttfb_ms, total_ms=self.total_ms(), usage=u,
            credits=credits, retries=self.retries, error=self.error,
            realm_override=self._log_realm(),
        )

    def _log_realm(self) -> str:
        """日志里的域取「实际服务该请求的账号所属域」。

        请求域可能为空（密钥不限域）或带前缀；用账号真实域才能在看板里
        按域正确聚合。
        """
        if self.uid:
            entry = self.gateway.pool.get(self.uid)
            if entry is not None and entry.realm:
                return entry.realm
        return self.params.realm

    def fail(self, status: int, error: str, usage: dict[str, Any] | None = None) -> None:
        self.status = status
        self.error = error
        self.gateway.log_request(
            params=self.params, uid=self.uid, status=status,
            ttfb_ms=self.ttfb_ms, total_ms=self.total_ms(), usage=usage or {},
            credits=0.0, retries=self.retries, error=error,
            realm_override=self._log_realm(),
        )


class Gateway:
    """网关门面：持有配置、账号池、上游客户端、密钥库与日志器。"""

    def __init__(
        self,
        cfg: Config,
        pool: AccountPool,
        client: UpstreamClient,
        refresher: "TokenRefresher",
        key_store: Any,
        request_logger: Any,
        models: Any = None,
    ):
        self.cfg = cfg
        self.pool = pool
        self.client = client
        self.refresher = refresher
        self.key_store = key_store
        self.request_logger = request_logger
        # 模型注册表：用于无域前缀请求按模型归属自动路由（可为 None，测试常见）
        self.models = models

    def realm_chain(self, model_id: str, explicit: str = "") -> list[str]:
        """选号域链（优先 → 兜底）；元素为空串表示「不限域」。

        - **显式域前缀**（`cn:xxx` / `global:xxx`）：只用该域，不跨域兜底 ——
          调用方已明确指定，擅自改写会违背其意图。
        - **`pool.realm_priority` 为空**（默认）：保持旧口径 —— 模型唯一归属时
          限定该域，跨域重名或未知则不限域。
        - **非空**：所有能提供该模型的域按优先级排序；靠前域的账号全部不可服务
          时才降级到下一个域兜底。典型用法 `["global", "cn"]` —— global 的 0 积分
          免费模型先跑，都打不动了再用 cn 的同名模型（有倍率、要花积分）兜底，
          而不是直接对下游报 429。

        目录里没有任何域提供该模型（归属未知）→ 返回 `[""]`，不擅自猜测。
        """
        if explicit:
            return [explicit]
        if not self.cfg.pool.route_by_model or self.models is None:
            return [""]
        try:
            owners = self.models.realms_for_model(model_id)
        except Exception:  # noqa: BLE001 - 路由提示失败不应影响请求
            return [""]

        priority = [r for r in (self.cfg.pool.realm_priority or []) if r]
        if not priority:
            return [owners[0] if len(owners) == 1 else ""]
        if not owners:
            return [""]

        enabled = set(self.cfg.enabled_realms())
        ordered = [r for r in priority if r in owners and r in enabled]
        # 能提供该模型、但没写进优先级的域，接在末尾兜底
        ordered += [r for r in owners if r not in ordered and r in enabled]
        return ordered or [""]

    def realm_for_model(self, model_id: str) -> str:
        """模型唯一归属域；未知/跨域重名/未启用该特性时返回空串（不限域）。"""
        if not self.cfg.pool.route_by_model or self.models is None:
            return ""
        try:
            return self.models.realm_for_model(model_id) or ""
        except Exception:  # noqa: BLE001 - 路由提示失败不应影响请求
            return ""

    def session(self, params: ChatParams) -> ChatSession:
        return ChatSession(self, params)

    def log_request(
        self, *, params: ChatParams, uid: str, status: int, ttfb_ms: int,
        total_ms: int, usage: dict[str, Any], credits: float, retries: int, error: str,
        realm_override: str = "",
    ) -> None:
        from .logging import RequestRecord

        u = usage or {}
        self.request_logger.log(RequestRecord(
            key_id=params.key_id, key_name=params.key_name, account_uid=uid,
            realm=realm_override or params.realm, protocol=params.protocol, model=params.model,
            stream=params.stream, status=status, ttfb_ms=ttfb_ms, total_ms=total_ms,
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            completion_tokens=int(u.get("completion_tokens") or 0),
            total_tokens=int(u.get("total_tokens") or 0),
            credits=credits, retries=retries, client_ip=params.client_ip, error=error,
        ))
        if params.key_id:
            self.key_store.add_usage(params.key_id, credits, int(u.get("total_tokens") or 0))


# ---- 流式 / 非流式 OpenAI 辅助 ----

def extract_credits(usage: dict[str, Any] | None) -> float:
    """从 usage 里提取本次消耗积分（上游字段名不定，逐个尝试）。"""
    if not isinstance(usage, dict):
        return 0.0
    for key in ("credit", "credits", "cost", "totalCredit"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


async def openai_stream(session: ChatSession) -> AsyncIterator[bytes]:
    """透传上游 OpenAI SSE，收尾时回写统计（usage 由 iter_lines 统一捕获）。

    下游拿到的是原样的 OpenAI chunk 流（含 `data: [DONE]`）。
    """
    try:
        async for line in session.iter_lines():
            yield (line.strip("\r\n") + "\n\n").encode("utf-8")
    finally:
        # usage / TTFB 已在 iter_lines 内捕获，这里只做收尾落库
        session.finish(None)
        await session.close()


async def openai_collect(session: ChatSession) -> tuple[dict[str, Any], dict[str, Any]]:
    """把上游流聚合为完整 OpenAI 响应；返回 (响应体, usage)。"""
    from ..upstream.sse import aggregate

    chunks = []
    saw_done = False
    async for line in session.iter_lines():
        stripped = line.strip("\r\n")
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            continue
        if not payload:
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, dict):
            chunks.append(chunk)

    agg = aggregate(chunks)
    agg.saw_done = saw_done
    usage = agg.usage or {}
    body = {
        "id": agg.id or f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": agg.created or int(time.time()),
        "model": session.params.model or agg.model,
        "choices": [{
            "index": 0,
            "message": _assemble_message(agg),
            "finish_reason": agg.finish_reason,
        }],
        "usage": usage,
    }
    if agg.truncated and not session.error:
        session.error = f"truncated:{agg.truncation_reason}"
    session.finish(usage, extract_credits(usage))
    await session.close()
    return body, usage


def _assemble_message(agg: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": agg.content or ""}
    if agg.reasoning:
        msg["reasoning_content"] = agg.reasoning
    if agg.tool_calls:
        msg["tool_calls"] = agg.tool_calls
    return msg
