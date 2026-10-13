"""ZCode 首次安装初始化仿真 —— 按官方客户端真实首启顺序请求一遍。

官方桌面端首启实测序（客户端首启日志 + 应用包逆向 + 实装模仿，三源交叉确认）：

  1. `GET /api/v1/client/configs?app_version=…` 免鉴权拉取运行配置
     （验证码 scene/region 开关、活动套餐投放开关等功能开关）
  2. `POST /api/v1/event/report` 激活遥测（**无 Authorization**）
     `element = app_launch → app_daily_active`（日活去重由上游按
     device_mid + 日期计算）
  3. （用户登录后）OAuth 流程 → 业务凭证；登录态的激活上报由
     `claim.report_activation_events` 承担，不在此处

## 安装语义要点（对齐官方客户端）

- `client/configs` 不带 Authorization（免鉴权端点），带 `app_version` 查询参数；
  实测带 `platform` 参数会被拒（3001），故只带 `app_version`。
- `event/report` 的 `device_mid` + 当日日期构成日活去重键；重复上报同日
  `app_daily_active` 无副作用（官方客户端每次启动都发）。
- 全程不需要账号凭证 —— 「安装」先于「登录」，未登录设备同样上报
  `app_launch`（`user_id` 为空串），登录后才带 `user_id`。

对外入口 `run_install_sequence_for_account()`：入池 / 指纹轮换后后台执行。
任何失败都不抛出，`errors` 列表留痕（安装仿真不影响主服务，与官方
「配置拉取失败继续启动」一致）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from . import constants as C
from .base import ErrorKind, UpstreamError
from .fingerprint import DeviceProfile, profile_for
from .telemetry import business_code, post_activation_event

# 后台安装任务集合：持有强引用防 GC，完成即自行移除
_install_tasks: set[asyncio.Task] = set()


def jwt_user_id(token: str | None) -> str:
    """从 JWT payload 解 user_id（user_id 优先，sub 兜底）；解不出返回空串。

    官方客户端事件上报以 user_id 标识用户；网关不存 user_info，直接从 JWT 解。
    """
    raw = (token or "").strip()
    if raw.count(".") != 2:
        return ""
    try:
        seg = raw.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (IndexError, ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    uid = payload.get("user_id") or payload.get("sub")
    return uid.strip() if isinstance(uid, str) else ""


def is_jwt(token: str | None) -> bool:
    """三段点分形态即 Coding Plan JWT（ZCode 账号包的判据）。"""
    return bool(token) and (token or "").count(".") == 2


async def fetch_client_configs(client: Any, timeout: float = 15.0) -> dict[str, Any]:
    """第 1 步：client/configs（免鉴权）。HTTP / 业务码任一失败抛异常。"""
    url = f"{C.CLIENT_CONFIGS_URL}?app_version={C.CLIENT_APP_VERSION}"
    resp = await client.get_url(
        url, headers={"User-Agent": f"ZCode/{C.CLIENT_APP_VERSION}"}, timeout=timeout
    )
    if resp.status_code >= 400:
        raise UpstreamError(
            ErrorKind.UNKNOWN, resp.status_code,
            f"client/configs HTTP {resp.status_code}",
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise UpstreamError(
            ErrorKind.UNKNOWN, resp.status_code, "client/configs 非 JSON"
        ) from exc
    code = business_code(body)
    if code != 0:
        raise UpstreamError(
            ErrorKind.UNKNOWN, resp.status_code, f"client/configs 业务码异常({code})"
        )
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def captcha_enabled(configs: dict[str, Any]) -> bool:
    """从 client/configs 读出验证码开关（上游下发为准）。"""
    cfg = configs.get("configs")
    captcha = cfg.get("captcha") if isinstance(cfg, dict) else None
    return bool(captcha.get("enabled")) if isinstance(captcha, dict) else False


async def run_install_sequence_for_account(
    client: Any, auth: Any, installed_at: int = 0, timeout: float = 15.0
) -> dict[str, Any]:
    """按账号安装序：client/configs + 激活事件，绑定该账号的安装身份。

    与进程级安装序的差别：

      - 设备档案 = 账号自身指纹（每账号独立 device_mid，官方语义「每台安装
        一台设备」），而非全局 device_mid；
      - `user_id` 从账号 JWT 解出（登录态安装）；无 JWT（apiKey 账号）退回
        空串 —— 官方未登录安装形态；
      - 幂等：`installed_at` 非空直接跳过（`skipped=True`，零上游请求）。

    任何失败都不抛出，`errors` 留痕。
    """
    result: dict[str, Any] = {
        "configs_fetched": False, "events_reported": [], "errors": [],
        "installed": False, "skipped": False,
    }
    from .claim import is_zcode_plan_account

    if not is_zcode_plan_account(auth):
        result["skipped"] = True
        result["reason"] = "非 ZCode 平台账号，跳过安装序"
        return result
    if installed_at:
        result["skipped"] = True
        return result

    profile = profile_for(auth)
    user_id = jwt_user_id(getattr(auth, "access_token", ""))

    try:
        configs = await fetch_client_configs(client, timeout=timeout)
        result["configs_fetched"] = True
        result["captcha_enabled"] = captcha_enabled(configs)
    except UpstreamError as err:
        result["errors"].append(f"client/configs 失败: {err.message or err.kind.value}")

    for element in C.ACTIVATION_ELEMENTS:
        try:
            await post_activation_event(client, profile, user_id, element, timeout=timeout)
            result["events_reported"].append(element)
        except UpstreamError as err:
            result["errors"].append(err.message or err.kind.value)

    result["installed"] = not result["errors"]
    return result


def schedule_install(client: Any, auth: Any, on_done: Any = None) -> None:
    """后台调度按账号安装序（configs + 激活事件，幂等）。

    入池、指纹换发、风控禁用换设备等场景共用：不阻塞调用方；重复调用安全
    （`installed_at` 幂等跳过）；异常全部兜住不冒泡。
    **仅 ZCode 平台账号会真正执行**，其余账号在 `run_install_sequence_for_account`
    内即被跳过（零上游请求）。
    `on_done(uid, installed_at)` 用于调用方落库。
    """
    uid = getattr(auth, "uid", "")
    installed_at = int(getattr(auth, "installed_at", 0) or 0)

    async def _job() -> None:
        try:
            result = await run_install_sequence_for_account(client, auth, installed_at)
            if on_done is not None and (result.get("installed") or result.get("skipped")):
                at = installed_at or int(time.time())
                if result.get("installed"):
                    on_done(uid, at)
        except Exception:  # noqa: BLE001 - 兜底：绝不冒泡
            pass

    task = asyncio.create_task(_job())
    _install_tasks.add(task)
    task.add_done_callback(_install_tasks.discard)


def process_profile() -> DeviceProfile:
    """进程级安装序使用的设备档案（诊断语义：本机形态 + 全新 device_mid）。"""
    from . import hostinfo

    return hostinfo.collect_host_profile()
