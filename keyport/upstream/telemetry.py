"""激活遥测上报 —— 官方 `event/report` 端点的单一事实源。

事件体字段集与官方桌面端 `sendReport` 逐字段一致（三源交叉：首启日志 /
客户端 bundle 逆向 / 实装模仿）。字段集、URL、业务码判定只在本模块维护：

  - `claim.report_activation_events`（登录态激活上报，preview 之前）
  - `install.run_install_sequence`（官方首启安装序）

## 为什么必须先上报再 preview

活动套餐投放疑似以「当日激活信号」为资格。若先 preview 后补上报，
在新的一天会形成「preview 恒空 → 永不激活 → 也就不会上报」的死锁。
因此激活上报的时序固定在 preview 之前，频率由调用方按日去重门控制。
"""

from __future__ import annotations

import uuid
from typing import Any

from . import constants as C


def build_activation_event_body(element: str, profile: Any, user_id: str) -> dict[str, Any]:
    """激活事件体（官方 sendReport 字段集固定为这 16 个）。"""
    return {
        "event_id": str(uuid.uuid4()),
        "client_timezone": profile.timezone,
        "client_language": profile.language,
        "element_name": element,
        "event_region": "app",
        "event_type": "view",
        "event_text": "",
        "event_extra_detail": {},
        "user_id": user_id,
        "screen_resolution": profile.screen,
        "app_version": C.CLIENT_APP_VERSION,
        "device_os_category": profile.os_category,
        "device_os_version": profile.os_version,
        "device_mid": profile.device_mid,
        "mac_id": "",
        "marketing_params": "{}",
    }


def business_code(body: Any) -> int:
    """上游业务码；非对象 / 缺 code / 非数字 → -1（视为失败）。"""
    if not isinstance(body, dict):
        return -1
    try:
        return int(body.get("code", -1))
    except (TypeError, ValueError):
        return -1


async def post_activation_event(
    client: Any, profile: Any, user_id: str, element: str, timeout: float = 10.0
) -> None:
    """单条激活事件上报（无 Authorization，官方端点不校验登录态）。

    HTTP >= 400 或业务码非 0 抛 `UpstreamError`，文案含定位信息；
    容错策略（中止 / 继续）由调用方决定。走 `client.post_url` 以便测试
    用 MockTransport 拦截（与其余上游调用同一注入点）。
    """
    from .base import ErrorKind, UpstreamError

    try:
        resp = await client.post_url(
            C.EVENT_REPORT_URL,
            headers={"Content-Type": "application/json"},
            json=build_activation_event_body(element, profile, user_id),
            timeout=timeout,
        )
    except UpstreamError as exc:
        raise UpstreamError(exc.kind, exc.status, f"event/report {element}: {exc.message}") from exc
    if resp.status_code >= 400:
        raise UpstreamError(
            ErrorKind.UNKNOWN, resp.status_code,
            f"event/report {element} HTTP {resp.status_code}: {resp.text[:120]}",
        )
    try:
        body = resp.json()
    except ValueError:
        body = None
    code = business_code(body)
    if code != 0:
        raise UpstreamError(
            ErrorKind.UNKNOWN, resp.status_code,
            f"event/report {element} 业务码异常({code}): {resp.text[:120]}",
        )
