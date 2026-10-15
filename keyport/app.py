"""FastAPI 应用装配：中间件、路由挂载、静态托管、生命周期。

单进程单端口部署：`/api` 与 `/v1` 同源，前端静态导出交由后端托管，无需 CORS。
"""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

from .api.admin_auth import AdminAuth, bootstrap_admin_password
from .api.auth_middleware import KeyStore
from .api.deps import AppContext
from .api.gateway import Gateway
from .api.logging import RequestLogger
from .api.models import ModelRegistry
from .api.routes_admin import router as admin_router
from .api.routes_keys import router as keys_router
from .api.routes_ops import router as ops_router
from .api.routes_v1 import router as v1_router
from .auth.refresh import TokenRefresher
from .auth.store import load_auth_dir
from .budget import DailyBudget
from .config import Config, get_config
from .db import init_db
from .pool.manager import AccountPool
from .ratelimit import ClientLimiter
from .scheduler import TASKS, Scheduler
from .taskjobs import TaskJobManager
from .upstream.captcha import (
    DEFAULT_PREFIX,
    DEFAULT_REGION,
    DEFAULT_SCENE_ID,
    CaptchaManager,
)
from .upstream.captcha import (
    CaptchaConfig as CaptchaCfg,
)
from .upstream.claim import ClaimService
from .upstream.client import UpstreamClient

log = logging.getLogger("keyport")


def _task_ledger(ctx: AppContext) -> dict[str, dict]:
    """从既有 `task_runs` 台账聚合「每类任务最近一轮」的计数（供 /metrics）。

    只为**跑过至少一轮**的任务返回条目 —— 从未跑过的任务没有序列，而不是
    「时刻为 0」；否则服务刚启动时按 `time() - last_run > 86400` 的告警会在每次
    部署时误报一轮。
    """
    ledger: dict[str, dict] = {}
    for task in ("checkin", "travel", "activity", "keepalive", "cat"):
        row = ctx.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_n, "
            "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS fail_n, "
            "SUM(CASE WHEN detail LIKE 'skipped:%' THEN 1 ELSE 0 END) AS skipped_n, "
            "MAX(ts) AS finished "
            "FROM task_runs WHERE task=? AND ts = ("
            "  SELECT MAX(ts) FROM task_runs WHERE task=?)",
            (task, task),
        )
        if row is None or not row["total"]:
            continue
        total = int(row["total"] or 0)
        skipped_n = int(row["skipped_n"] or 0)
        # 跳过记的是 ok=1（它是**预期**的正常态），所以要从 ok 里扣掉，
        # 否则「全灭」判据会被跳过项污染
        ok_n = max(0, int(row["ok_n"] or 0) - skipped_n)
        fail_n = int(row["fail_n"] or 0)
        ledger[task] = {
            "total": total, "ok": ok_n, "fail": fail_n,
            "skipped": skipped_n, "finished": int(row["finished"] or 0),
            # 「全灭」判据刻意不是失败率：还有账号成功就说明上游是通的
            "all_failed": bool(fail_n > 0 and ok_n == 0),
        }
    return ledger


def build_context(cfg: Config | None = None) -> AppContext:
    """构建全部进程级组件（可被测试直接调用）。"""
    cfg = cfg or get_config()
    db = init_db(cfg.resolved_db_path())
    pool = AccountPool(cfg, db)
    captcha = CaptchaManager(
        CaptchaCfg.from_dict({
            "enabled": cfg.captcha.enabled,
            "headless": cfg.captcha.headless,
            "pool_size": cfg.captcha.pool_size,
            "per_attempt_ms": cfg.captcha.per_attempt_ms,
            "gap_ms": cfg.captcha.gap_ms,
            "idle_sleep_ms": cfg.captcha.idle_sleep_ms,
            "max_wait_seconds": cfg.captcha.max_wait_seconds,
            "fail_backoff_ms": cfg.captcha.fail_backoff_ms,
            "fail_backoff_max_ms": cfg.captcha.fail_backoff_max_ms,
            "restart_backoff_ms": cfg.captcha.restart_backoff_ms,
            "restart_backoff_max_ms": cfg.captcha.restart_backoff_max_ms,
            "restart_max_consecutive": cfg.captcha.restart_max_consecutive,
            "chrome_path": cfg.captcha.chrome_path,
            # 空则留默认，由 _fetch_config 用上游下发值覆盖
            "scene_id": cfg.captcha.scene_id or DEFAULT_SCENE_ID,
            "prefix": cfg.captcha.prefix or DEFAULT_PREFIX,
            "region": cfg.captcha.region or DEFAULT_REGION,
        })
    )
    client = UpstreamClient(cfg, captcha=captcha)
    refresher = TokenRefresher(cfg, pool, client)
    key_store = KeyStore(db)
    request_logger = RequestLogger(db)
    models = ModelRegistry(cfg, pool, client)
    budget = DailyBudget(cfg.pool.budget_daily_credit_limit)
    gateway = Gateway(cfg, pool, client, refresher, key_store, request_logger,
                      models=models)
    # 预算闸同时挂在网关（记账）与上下文（准入判定）
    gateway.budget = budget
    scheduler = Scheduler(cfg, db, pool, client, refresher)
    # 告警数据源需要读 WAF IP 级拦截状态（该状态在网关上）
    scheduler.gateway = gateway
    claims = ClaimService(cfg.claim, client, db, captcha=captcha)
    # 后台任务作业：把一个账号的全量自动任务变成可查询的异步作业
    # （调度器是同步串行的，一个账号卡住会拖累整轮；作业化后创建即返回）
    jobs = TaskJobManager(
        Path(cfg.data_dir) / "task-jobs.json",
        lambda uid, report: scheduler.run_account_tasks(uid, report),
        total_actions=len(TASKS),
        concurrency=cfg.schedule.job_concurrency,
    )
    ctx = AppContext(
        cfg=cfg, db=db, pool=pool, client=client, refresher=refresher,
        key_store=key_store, request_logger=request_logger, gateway=gateway,
        models=models, scheduler=scheduler, captcha=captcha, claims=claims,
        budget=budget,
        limiter=ClientLimiter(),
        jobs=jobs,
        started_at=time.time(),
    )
    ctx.admin_auth = AdminAuth(db, cfg)  # type: ignore[attr-defined]
    return ctx


def _load_auth_files(ctx: AppContext) -> None:
    """启动时用 auths/ 目录自动对齐账号池（新增凭证文件即自动发现）。"""
    auths, failed = load_auth_dir(ctx.cfg.auth_path())
    for auth in auths:
        ctx.pool.upsert_account(auth)
    if auths or failed:
        log.info("auths/ 加载完成：%d 个账号，%d 个文件解析失败", len(auths), len(failed))


def create_app(cfg: Config | None = None) -> FastAPI:
    ctx = build_context(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _load_auth_files(ctx)
        generated = bootstrap_admin_password(ctx.db, ctx.cfg)
        if generated:
            log.warning("=" * 64)
            log.warning("面板初始密码（仅本次显示，请立即登录后修改）：%s", generated)
            log.warning("=" * 64)
        ctx.scheduler.start()
        # 后台任务作业：先加载磁盘状态（陈旧的 pending/running 会被判为 interrupted），
        # 再恢复上次退出时未完成的作业 —— 执行器会先回读服务端状态，
        # 已领奖的项目在内部跳过动作，不会重复领奖。
        ctx.jobs.start()
        try:
            ctx.jobs.resume()
        except Exception as err:  # noqa: BLE001 - 恢复失败不该阻断启动
            log.warning("后台任务恢复失败: %s", err)
        if ctx.captcha:
            await ctx.captcha.start()
        if ctx.claims is not None:
            ctx.claims.start(ctx.pool)
        # 预热模型目录：无域前缀请求靠「模型归属域」路由，缓存为空时无法判定。
        # 失败不影响启动（后续请求仍会按需拉取）。
        try:
            await ctx.models.list_models("", refresh=True)
        except Exception as err:  # noqa: BLE001
            log.warning("模型目录预热失败（路由将按需拉取）: %s", err)
        try:
            yield
        finally:
            if ctx.claims is not None:
                await ctx.claims.stop()
            if ctx.captcha:
                await ctx.captcha.close()
            # 作业先停：等正在飞的上游调用返回并把结果记完，再停调度器
            await ctx.jobs.stop()
            await ctx.scheduler.stop()
            ctx.pool.save_state()
            await ctx.client.aclose()
            ctx.db.close()

    app = FastAPI(
        title="KeyPort",
        description="自托管多账号 API 反向代理网关（ZCode/CodeBuddy → OpenAI/Anthropic）",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx

    # ---- 路由 ----
    app.include_router(v1_router)
    app.include_router(admin_router)
    app.include_router(keys_router)
    app.include_router(ops_router)

    # ---- 健康检查 ----
    @app.get("/healthz")
    async def healthz():
        summary = ctx.pool.summary()
        # 农场若已停止（异常退出且未重启），借健康检查顺带拉起，避免长期无供给
        if ctx.captcha:
            ctx.captcha.ensure_running()
        status = 200 if summary["healthy"] > 0 else 503
        return JSONResponse(
            status_code=status,
            content={"healthy": summary["healthy"], "total": summary["total"],
                     "service": "keyport", **summary},
        )

    @app.get("/status")
    async def status(request: Request):
        """状态端点（需 API Key，兼容既有网关运维习惯）。"""
        from .api.routes_v1 import AuthFailure, _authorize

        try:
            _authorize(request, request.headers.get("authorization", ""),
                       request.headers.get("x-api-key", ""))
        except AuthFailure as exc:
            return JSONResponse(status_code=exc.status,
                                content={"error": {"message": exc.message}})
        return {
            "pool": ctx.pool.summary(),
            "accounts": [
                {"uid": e.uid, "realm": e.realm, "credits": e.credits,
                 "healthy": e.healthy(int(__import__("time").time()))}
                for e in ctx.pool.all_entries()
            ],
            "model_costs": ctx.pool.ledger.snapshot(),
            "cost_explore": ctx.pool.ledger.explore_snapshot(),
        }

    @app.get("/metrics")
    async def metrics():
        """Prometheus 文本格式指标（零依赖现算，不触发任何上游调用）。

        刻意**不加管理台鉴权**：标准抓取端配置的是 bearer token 或网络层隔离，
        而这里的数据全部是池级聚合量（不含 uid、凭证、会话键）。
        """
        from .metrics import render_metrics

        now = int(time.time())
        health = {label: ctx.pool.metrics_health(realm)
                  for label, realm in (("cn", "cn"), ("global", "global"),
                                       ("all", ""))}
        # 请求统计：近 24h 的按模型聚合（现算，不维护第二份累加器）
        rows = ctx.db.query(
            "SELECT model, "
            "SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS failed, "
            "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
            "COALESCE(SUM(credits),0) AS credits, "
            "COALESCE(AVG(NULLIF(ttfb_ms,0)),0) AS avg_ttfb_ms "
            "FROM request_logs WHERE ts >= ? AND model <> '' GROUP BY model",
            (now - 86400,),
        )
        totals = ctx.db.query_one(
            "SELECT SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS failed "
            "FROM request_logs WHERE ts >= ?",
            (now - 86400,),
        ) or {}
        tasks = _task_ledger(ctx)
        gate = getattr(ctx.gateway, "waf_gate", None)
        return Response(
            content=render_metrics(
                health=health,
                totals={"success": int(totals["success"] or 0),
                        "failed": int(totals["failed"] or 0)},
                models=[dict(r) for r in rows],
                sticky_sessions=ctx.pool.sticky.size(),
                waf_active=bool(gate and gate.active()),
                tasks=tasks,
                since=int(getattr(ctx, "started_at", 0) or 0),
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ---- 静态托管（前端静态导出） ----
    static_dir = ctx.cfg.resolved_static_dir()
    if static_dir.is_dir():
        # Next.js 静态导出的资源目录是 _next/（存在才挂载，避免启动即失败）
        next_assets = static_dir / "_next"
        if next_assets.is_dir():
            app.mount(
                "/_next",
                CacheAwareStaticFiles(directory=str(next_assets)),
                name="next-assets",
            )
        _mount_spa(app, static_dir)
    else:
        @app.get("/")
        async def placeholder():
            return JSONResponse({
                "service": "keyport",
                "notice": "管理台前端尚未构建；请执行 `cd frontend && npm install && npm run build`",
                "static_dir": str(static_dir),
            })

    return app


IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
# HTML 与固定名清单：允许 ETag 协商（304），但每次必须回源验证一次。
REVALIDATE_CACHE = "no-cache"


def _is_immutable(name: str) -> bool:
    """文件名是否带内容哈希。

    Next.js 构建产物的 chunk / 媒体资源名内嵌内容哈希（如
    `page-251e88d0605c73e9.js`），内容一变文件名随之变，因此可安全长缓存。
    例外是固定名清单 `_buildManifest.js` / `_ssgManifest.js`——它们名字不变
    但内容随构建变化，长缓存会让浏览器在新版本发布后仍读到旧清单。
    """
    return bool(re.search(r"[0-9a-f]{8,}", name))


def _not_modified(response_headers: Headers, request_headers: Headers) -> bool:
    """是否可返回 304。复用 Starlette 的判断，避免自己重写 ETag 解析规则。

    `StaticFiles.is_not_modified` 不依赖实例状态，这里直接借用。
    """
    return StaticFiles.is_not_modified(None, response_headers, request_headers)  # type: ignore[arg-type]


class CacheAwareStaticFiles(StaticFiles):
    """按资源类型下发 Cache-Control 的静态文件挂载。

    默认 StaticFiles 完全不发 Cache-Control：内容哈希资源本可直接命中本地
    缓存，却要每次发条件请求回源验证；而 HTML 又会落到浏览器的启发式缓存
    上，可能读到过期页面。这里按名称特征分别给出策略。
    """

    def file_response(  # type: ignore[override]
        self,
        full_path: "os.PathLike[str]",
        stat_result: os.stat_result,
        scope: "Scope",
        status_code: int = 200,
    ) -> Response:
        resp = super().file_response(full_path, stat_result, scope, status_code)
        # 304 分支同样要带 Cache-Control：浏览器据此决定后续是否还需回源。
        resp.headers["Cache-Control"] = (
            IMMUTABLE_CACHE if _is_immutable(Path(full_path).name) else REVALIDATE_CACHE
        )
        return resp


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    """托管静态导出并做 SPA 回退。

    静态导出（trailingSlash: true）的产物形态：
      - `/`          → `index.html`
      - `/login/`    → `login/index.html`（**目录**，不是同名文件）
      - `/login`     → 需重定向到 `/login/`，否则相对资源解析会错位
      - RSC 预取     → `*.txt`（Next.js 客户端路由数据）

    早期实现只判断 `candidate.is_file()`，导致所有子路径都回落到根
    `index.html`：浏览器按根页面的 React 树去 hydration 子路由 URL，
    路由不匹配 → 整棵树卸载 → 白屏。
    """
    index = static_dir / "index.html"

    def _resolve(full_path: str) -> Path | None:
        """把 URL 路径解析为磁盘上的静态文件（含目录索引），越界返回 None。"""
        candidate = (static_dir / full_path).resolve()
        try:
            candidate.relative_to(static_dir.resolve())
        except ValueError:
            return None  # 目录穿越防护
        if candidate.is_file():
            return candidate
        # 目录 → 目录内 index.html（trailingSlash 导出的常规形态）
        if candidate.is_dir():
            idx = candidate / "index.html"
            if idx.is_file():
                return idx
        # 无扩展名且同名 .html 存在（部分导出形态）
        if not candidate.suffix:
            html = candidate.with_suffix(".html")
            if html.is_file():
                return html
        return None

    def _html(path: Path, request_headers: "Headers | None" = None) -> Response:
        """HTML 一律 no-cache：每次必须回源确认，但确认未变时回 304 省掉传输。

        不加 Cache-Control 的话，浏览器会按 Last-Modified 启用启发式缓存，
        新版本发布后可能继续用旧的 index.html 去配新的 chunk，导致资源 404。

        这里必须自己做条件判断：FileResponse 只负责生成 ETag，是否返回 304
        的判断是 StaticFiles.is_not_modified 的职责，直接 new 一个
        FileResponse 不会协商——于是发出了 no-cache 却永远回 200。
        """
        stat = os.stat(path)
        resp = FileResponse(path, stat_result=stat)
        resp.headers["Cache-Control"] = REVALIDATE_CACHE
        if request_headers is not None and _not_modified(resp.headers, request_headers):
            return NotModifiedResponse(resp.headers)
        return resp

    @app.get("/")
    async def root(request: Request):
        return _html(index, request.headers)

    @app.get("/{full_path:path}")
    async def spa(full_path: str, request: Request):
        if full_path.startswith(("api/", "v1/", "healthz", "status")):
            return Response(status_code=404)
        # 目录形式的子路由缺尾斜杠时补上，避免页面内相对路径解析错位
        if not full_path.endswith("/") and (static_dir / full_path).is_dir():
            return RedirectResponse(url=f"/{full_path}/", status_code=308)
        found = _resolve(full_path)
        if found is not None:
            return _html(found, request.headers)
        # RSC 预取（*.txt）找不到时不应回落到 HTML，否则客户端解析失败
        if request.headers.get("rsc") == "1" or full_path.endswith(".txt"):
            return Response(status_code=404)
        return _html(index, request.headers)
