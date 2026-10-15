"""模型目录服务：聚合账号池可用模型，带缓存（数据直连上游，不编造字段）。"""

from __future__ import annotations

import time

from ..config import Config
from ..pool.manager import AccountPool
from ..upstream.base import ErrorKind, UpstreamError
from ..upstream.client import UpstreamClient

CACHE_TTL = 300


class ModelRegistry:
    """按 realm 聚合模型列表并缓存。

    两份目录，用途不同：

    - `_cache`：**过滤后**的目录（应用 `realms.<域>.models_free_only`），
      用于 `/v1/models` 展示。
    - `_raw`：**未过滤**的目录（上游下发 + `realms.<域>.extra_models` 补录），
      专供**路由归属判定**。

    两者必须分开：`models_free_only` 是「下游只看得到免费档」的展示策略，
    不该顺带把付费模型从路由可用域里删掉 —— 否则 cn 那份付费同名模型被过滤后，
    该模型就只剩 global 一个归属域，`route_by_model` 会把请求锁死在 global，
    全挂时也没有 cn 可兜底。
    """

    def __init__(self, cfg: Config, pool: AccountPool, client: UpstreamClient):
        self.cfg = cfg
        self.pool = pool
        self.client = client
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._raw: dict[str, tuple[float, list[dict]]] = {}

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
        models = self._with_extra_models(realm, models)
        # 路由目录：拉取失败（列表为空）时**不覆盖**已有快照，避免一次上游抖动
        # 就把该域的模型归属全判成「未知」。
        if models or realm not in self._raw:
            self._raw[realm] = (now, models)
        filtered = self._filter_free_only(realm, models)
        self._cache[realm] = (now, filtered)
        return filtered

    def _with_extra_models(self, realm: str, models: list[dict]) -> list[dict]:
        """把 `realms.<域>.extra_models` 补录进目录（去重，已下发的以上游为准）。

        两种写法语义不同，别混用：

        - **字符串**：`"hy4-preview-f"` —— 简写，显式声明「按 0 倍率（免费）处理」。
        - **对象**：`{"id": "..."}` —— **原样采用**。省略 `credit_factor` 即
          「倍率未知」，与上游下发的口径一致（解析不出倍率时上游也不写该键）。
          按 `_filter_free_only` 的语义，未知**不算免费**，因此它在
          `models_free_only` 的域里不会进下游清单 —— 但仍在**路由目录**里，
          调它时会打向正确的域，而不是被不限域随机丢到别的域。
        """
        rc = self.cfg.realm(realm)
        extras = list(getattr(rc, "extra_models", None) or []) if rc else []
        if not extras:
            return models
        known = {m.get("id") for m in models}
        out = list(models)
        for item in extras:
            if isinstance(item, str):
                entry: dict = {"id": item.strip(), "credit_factor": 0}
            elif isinstance(item, dict):
                entry = dict(item)
                entry["id"] = str(entry.get("id") or "").strip()
            else:
                continue
            if not entry["id"] or entry["id"] in known:
                continue
            entry["realm"] = realm
            known.add(entry["id"])
            out.append(entry)
        return out

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

    def realms_for_model(self, model_id: str) -> list[str]:
        """所有**能提供该模型**的域（按路由目录 `_raw` 判定）。

        关键点：不受 `models_free_only` 影响。免费过滤只是「别在下游列表里广告
        付费模型」，不代表那个域跑不了 —— 它正是跨域兜底要用的备胎。若这里误用
        过滤后目录，cn 的付费同名模型会被滤掉，该模型就只剩一个归属域，
        全挂时无域可降级。
        """
        if not model_id:
            return []
        return [
            realm for realm, (_, items) in self._raw.items()
            if any(m.get("id") == model_id for m in items)
        ]

    def realm_for_model(self, model_id: str) -> str | None:
        """模型归属域；仅当**唯一归属**时返回，否则 None（不强制路由）。

        用于无域前缀请求按模型自动路由：模型只在一个域提供时限定该域选号，
        避免高余额域抢走不属于它的模型（该域上游会因模型不存在而报错）。

        目录为空（尚未探测过上游模型目录）或跨域重名时返回 None —— 此时保持
        原有「不限域」行为，不擅自猜测归属。
        """
        owners = self.realms_for_model(model_id)
        return owners[0] if len(owners) == 1 else None

    def cached(self) -> dict[str, list[dict]]:
        """返回展示目录快照（供管理台，不触发网络）。"""
        return {realm: items for realm, (_, items) in self._cache.items()}

    def raw_cached(self) -> dict[str, list[dict]]:
        """返回路由目录快照（未过滤，供排查归属判定）。"""
        return {realm: items for realm, (_, items) in self._raw.items()}

    def invalidate(self, realm: str = "") -> None:
        if realm:
            self._cache.pop(realm, None)
            self._raw.pop(realm, None)
        else:
            self._cache.clear()
            self._raw.clear()
