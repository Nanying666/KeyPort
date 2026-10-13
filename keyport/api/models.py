"""模型目录服务：聚合账号池可用模型，带缓存（数据直连上游，不编造字段）。"""

from __future__ import annotations

import time

from ..config import Config
from ..pool.manager import AccountPool
from ..upstream.base import ErrorKind, UpstreamError
from ..upstream.client import UpstreamClient

CACHE_TTL = 300


class ModelRegistry:
    """按 realm 聚合模型列表并缓存。"""

    def __init__(self, cfg: Config, pool: AccountPool, client: UpstreamClient):
        self.cfg = cfg
        self.pool = pool
        self.client = client
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    async def list_models(self, realm: str = "", refresh: bool = False) -> list[dict]:
        """聚合可用模型；realm 为空时遍历全部启用域。"""
        realms = [realm] if realm else self.cfg.enabled_realms()
        merged: dict[str, dict] = {}
        for rname in realms:
            for item in await self._models_for_realm(rname, refresh):
                model_id = item.get("id", "")
                if not model_id:
                    continue
                # 多域同名模型：保留首次出现（域前缀已由调用方区分）
                merged.setdefault(model_id, {**item, "realm": rname})
        return sorted(merged.values(), key=lambda m: (m.get("series", ""), m["id"]))

    async def _models_for_realm(self, realm: str, refresh: bool) -> list[dict]:
        now = time.time()
        if not refresh:
            cached = self._cache.get(realm)
            if cached and (now - cached[0]) < CACHE_TTL:
                return cached[1]

        # 取该域任一健康账号拉取（真实上游数据）
        entries = [
            e for e in self.pool.all_entries()
            if e.realm == realm and e.in_pool()
        ]
        models: list[dict] = []
        for entry in entries[:3]:
            auth = self.pool.auth_for(entry)
            if not auth.access_token:
                continue
            try:
                models = await self.client.fetch_models(auth)
            except UpstreamError as exc:
                if exc.kind in (ErrorKind.AUTH, ErrorKind.NETWORK):
                    continue
                break
            if models:
                break
        models = self._filter_free_only(realm, models)
        self._cache[realm] = (now, models)
        return models

    def _filter_free_only(self, realm: str, models: list[dict]) -> list[dict]:
        """按域配置过滤：`models_free_only` 时只留**明确 0 倍率**（免费）模型。

        白名单语义：倍率必须明确为 0 才保留。倍率缺失/未知（如上游档位别名
        `default-model`，`credits` 为空字符串）一律剔除 —— 「只列 0 积分模型」
        要求的是确定的免费，未知不能当作免费。
        """
        rc = self.cfg.realm(realm)
        if rc is None or not rc.models_free_only:
            return models
        return [m for m in models if m.get("credit_factor") == 0]

    def realm_for_model(self, model_id: str) -> str | None:
        """模型归属域；仅当**唯一归属**时返回，否则 None（不强制路由）。

        用于无域前缀请求按模型自动路由：模型只在一个域提供时限定该域选号，
        避免高余额域抢走不属于它的模型（该域上游会因模型不存在而报错）。

        缓存为空（尚未探测过模型目录）或跨域重名时返回 None —— 此时保持
        原有「不限域」行为，不擅自猜测归属。
        """
        if not model_id:
            return None
        owners = [
            realm for realm, (_, items) in self._cache.items()
            if any(m.get("id") == model_id for m in items)
        ]
        return owners[0] if len(owners) == 1 else None

    def cached(self) -> dict[str, list[dict]]:
        """返回缓存快照（供管理台，不触发网络）。"""
        return {realm: items for realm, (_, items) in self._cache.items()}

    def invalidate(self, realm: str = "") -> None:
        if realm:
            self._cache.pop(realm, None)
        else:
            self._cache.clear()
