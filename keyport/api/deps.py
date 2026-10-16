"""依赖注入：把网关组件挂到 FastAPI 的 app.state 上，并提供取用函数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request

from ..api.gateway import Gateway
from ..api.models import ModelRegistry
from ..config import Config
from ..db import Database


@dataclass
class AppContext:
    """进程级组件容器。"""

    cfg: Config
    db: Database
    pool: Any
    client: Any
    refresher: Any
    key_store: Any
    request_logger: Any
    gateway: Gateway
    models: ModelRegistry
    scheduler: Any = None
    captcha: Any = None
    claims: Any = None
    # 当日积分预算闸（全局准入控制）与客户端维度限流器
    budget: Any = None
    limiter: Any = None
    # 后台任务作业管理器
    jobs: Any = None
    # 面板登录节流（按 IP + 全局双维度）
    login_guard: Any = None
    started_at: float = 0.0
    login_states: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.login_states is None:
            self.login_states = {}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx
