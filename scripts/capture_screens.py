"""抓取 KeyPort 管理台各页面截图（供 README 引用）。

登录 → 逐页截图，落盘到 `docs/images/`。

用法：
    python scripts/capture_screens.py --base http://127.0.0.1:8790
                                      --user admin --password ***
                                      --out docs/images

依赖 playwright（`pip install playwright`，浏览器用本机 Chrome 或已安装的
Playwright Chromium）。本脚本只读页面、不修改任何服务端状态。

## 脱敏（默认开启）

管理台页面会展示真实账号标识（UID、昵称、密钥前缀）。截图要进版本库，
所以默认**自动脱敏**：先向管理 API 读出当前实例的敏感值，再拦截这些 API 的
响应、把敏感串替换成掩码后再交给前端渲染。

为什么用**响应拦截**而不是截图后打码 / 改 DOM：
  - 截图后打码需要 OCR 定位，脆且易漏；
  - 改 DOM 会被 10–15 秒一轮的轮询重新渲染冲掉，存在竞态。
拦截响应是唯一无竞态的做法 —— 前端从头到尾只见过掩码值。

`--no-redact` 可关闭（本机自查用，**不要把产物提交进版本库**）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 页面清单：(文件名, 路由, 说明)。顺序即 README 里的展示顺序。
#
# `tabs` 非空时表示该页有池页签需要分别截图（账号管理分
# WorkBuddy/CodeBuddy 与 ZCode 两个池，列表列与操作不同，必须各截一张）。
PAGES: tuple[tuple[str, str, str], ...] = (
    ("overview", "/", "概览"),
    ("accounts-wb", "/accounts", "账号管理 · WorkBuddy/CodeBuddy 池"),
    ("accounts-zcode", "/accounts", "账号管理 · ZCode 池"),
    ("keys", "/keys", "密钥管理"),
    ("logs", "/logs", "调用日志"),
    ("usage", "/usage", "用量统计"),
    ("tasks", "/tasks", "任务调度"),
    ("playground", "/playground", "聊天测试台"),
    ("settings", "/settings", "系统设置"),
)

# 需要切换页签的页面：路由 → [(文件名后缀, 页签序号)]。
# 账号管理页第一个页签是 WorkBuddy/CodeBuddy，第二个是 ZCode。
TAB_PAGES: dict[str, list[tuple[str, int]]] = {
    "/accounts": [("wb", 0), ("zcode", 1)],
}

# 需要读出来做脱敏的 API（登录后直接查，未拦截）
DISCOVERY_APIS = (
    "/api/accounts",
    "/api/keys",
    "/api/logs?limit=200",
    "/api/claim/history?limit=200",
    "/api/claim/preview",
    "/api/tasks",
    "/api/billing?days=7",
)

# 需要拦截替换的 API 前缀。**必须覆盖所有会回显账号标识的端点** ——
# 漏一个（例如 /api/tasks 执行记录、/api/billing 快照里带账号 uid / 昵称）
# 就会在截图里露出真实标识。
REDACT_ROUTES = (
    "/api/accounts",
    "/api/keys",
    "/api/logs",
    "/api/claim/**",
    "/api/guards",
    "/api/overview",
    "/api/usage",
    "/api/tasks",
    "/api/billing",
)

VIEWPORT = {"width": 1600, "height": 1000}
# 视口高度上限：设置页很长（护栏 + 别名 + 双域 + 上游 + 审计），
# 撑高视口才能一次截全；超过上限则退回滚动截屏。
MAX_VIEWPORT_HEIGHT = 5200


def _mask_value(value: str, keep: int = 4) -> str:
    """保留前 `keep` 个字符，其余替换为掩码点。

    保留极短前缀是为了让截图仍能体现「这里显示的是标识」（而不是看起来像坏了），
    同时不足以还原原值。UID / 昵称这类可定位到人的标识刻意只留 3–4 个字符。
    """
    value = value.strip()
    if len(value) <= keep:
        return "•" * len(value)
    return value[:keep] + "•" * min(len(value) - keep, 12)


def _collect_strings(obj, out: set[str]) -> None:
    """递归收集 JSON 里的所有字符串（用于发现敏感值）。"""
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, out)


def discover_sensitive(page, base: str) -> dict[str, str]:
    """向管理 API 读出当前实例的敏感值，返回 {原值: 掩码}。

    只取**明确是标识**的字段（账号 uid / 昵称、密钥前缀），不泛化到所有字符串
    —— 否则「不限域」「playground」这类正常文案也会被掩码，截图就失去说明力了。
    """
    mapping: dict[str, str] = {}

    def add(value: object, keep: int = 4) -> None:
        if isinstance(value, str) and value.strip() and value not in mapping:
            mapping[value] = _mask_value(value, keep)

    for path in DISCOVERY_APIS:
        try:
            data = page.evaluate(
                "async (p) => { try { const r = await fetch(p, "
                "{credentials:'same-origin'}); return r.ok ? await r.json() : null; } "
                "catch { return null; } }",
                f"{base}{path}",
            )
        except Exception as exc:  # noqa: BLE001 - 单个 API 失败不阻断
            print(f"  发现敏感值 {path} 失败: {exc}")
            continue
        if not isinstance(data, dict):
            continue

        for acct in data.get("accounts") or []:
            if isinstance(acct, dict):
                add(acct.get("uid"), 3)          # 账号 UID（只留 3 字符）
                add(acct.get("nickname"), 4)     # 账号昵称
        for key in data.get("keys") or []:
            if isinstance(key, dict):
                add(key.get("key_prefix"), 7)    # 密钥前缀（sk-kp-…，本就只展示前 12 位）
        for row in data.get("history") or []:
            if isinstance(row, dict):
                add(row.get("uid"), 3)
                add(row.get("account"), 4)
        for log in data.get("logs") or []:
            if isinstance(log, dict):
                add(log.get("account_uid"), 3)
                add(log.get("account_name"), 4)
        for run in data.get("runs") or []:
            if isinstance(run, dict):
                add(run.get("account"), 3)       # /api/tasks 执行记录里的账号 uid
        for acct in data.get("accounts") or []:
            if isinstance(acct, dict):
                add(acct.get("uid"), 3)          # /api/billing 快照里的账号
                add(acct.get("nickname"), 4)
        for row in data.get("local_usage") or []:
            if isinstance(row, dict):
                add(row.get("account_uid"), 3)

    # 兜底：任何 UUID 形态的串一律掩码（即使上面没发现）
    return mapping


def _apply_mapping(obj, mapping: dict[str, str]):
    """对 JSON 里的字符串做替换（先整串命中，再子串命中）。"""
    if isinstance(obj, str):
        if obj in mapping:
            return mapping[obj]
        out = obj
        for src in sorted(mapping, key=len, reverse=True):
            if src and src in out:
                out = out.replace(src, mapping[src])
        return out
    if isinstance(obj, dict):
        return {k: _apply_mapping(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_apply_mapping(v, mapping) for v in obj]
    return obj


def install_redaction(page, base: str, mapping: dict[str, str]) -> None:
    """拦截管理 API 响应并替换敏感串，让前端只见到掩码值。"""

    def handler(route) -> None:
        try:
            resp = route.fetch()
        except Exception:  # noqa: BLE001 - 取不到就放行原始响应
            route.continue_()
            return
        ctype = (resp.headers or {}).get("content-type", "")
        if "application/json" not in ctype:
            route.fulfill(response=resp)
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - 非 JSON 原样放行
            route.fulfill(response=resp)
            return
        route.fulfill(
            response=resp,
            body=json.dumps(_apply_mapping(body, mapping), ensure_ascii=False).encode(),
        )

    for path in REDACT_ROUTES:
        page.route(f"**{path}*", handler)


def _fit_viewport(page, max_height: int = MAX_VIEWPORT_HEIGHT) -> int:
    """把视口撑到能完整容纳主内容区，返回最终高度。

    管理台是 `h-dvh overflow-hidden` 外壳 + `main { overflow-y: auto }` 的内部
    滚动布局，所以：
      - `page.screenshot(full_page=True)` 只拿到视口那一屏（main 的 rect 被 flex
        约束成可视高度，文档本身没有滚动条）；
      - `main.getBoundingClientRect().height` 同样等于可视高度，测不出真实内容高。
    真实内容高要看 `main.scrollHeight`；而撑高视口后 flex 会重新分配空间、
    scrollHeight 随之收敛，所以迭代两轮取稳定值。
    """
    height = VIEWPORT["height"]
    for _ in range(3):
        need = int(page.evaluate(
            """() => {
                const main = document.querySelector('main');
                if (!main) return document.documentElement.scrollHeight;
                return main.scrollHeight + Math.ceil(main.getBoundingClientRect().top) + 24;
            }"""
        ))
        need = max(VIEWPORT["height"], min(need, max_height))
        if need <= height:
            break
        height = need
        page.set_viewport_size({"width": VIEWPORT["width"], "height": height})
        time.sleep(0.45)  # 让 recharts 等按新尺寸重排
    return height


def _launch(pw, chrome_path: str):
    """优先用本机 Chrome（渲染更接近真实浏览器），失败回落 Playwright Chromium。"""
    for kwargs in (
        {"channel": "chrome"},
        {"executable_path": chrome_path} if chrome_path else None,
        {},
    ):
        if kwargs is None:
            continue
        try:
            return pw.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 换下一种启动方式
            print(f"  launch {kwargs or 'bundled'} 失败: {exc}")
    raise RuntimeError("无法启动浏览器（Chrome / Playwright Chromium 都不可用）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KeyPort 管理台截图")
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--chrome", default="", help="Chrome 可执行文件路径（可选）")
    ap.add_argument("--settle", type=float, default=2.5, help="每页等待秒数（等数据加载）")
    ap.add_argument("--no-redact", action="store_true",
                    help="关闭自动脱敏（产物含真实账号标识，勿提交进版本库）")
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：pip install playwright", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")

    with sync_playwright() as pw:
        browser = _launch(pw, args.chrome)
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = ctx.new_page()

        # ---- 登录 ----
        print(f"[1/2] 登录 {base}/login")
        page.goto(f"{base}/login", wait_until="networkidle", timeout=30000)
        time.sleep(1.0)
        login_shot = out_dir / "00-login.png"
        page.screenshot(path=str(login_shot))
        print(f"  登录页 → {login_shot.name}")
        page.fill('input[autocomplete="username"]', args.user)
        page.fill('input[type="password"]', args.password)
        page.click('button[type="submit"]')
        try:
            page.wait_for_url(f"{base}/", timeout=15000)
        except Exception:  # noqa: BLE001 - 落到下面用 /api/session 复核
            pass
        authed = page.evaluate(
            "async () => (await fetch('/api/session', "
            "{credentials:'same-origin'})).json()"
        )
        if not authed.get("authenticated"):
            print(f"登录失败：{authed}", file=sys.stderr)
            browser.close()
            return 1
        print("  登录成功")

        # ---- 脱敏（必须在逐页截图之前装好，前端才只见掩码值）----
        if not args.no_redact:
            mapping = discover_sensitive(page, base)
            if mapping:
                install_redaction(page, base, mapping)
                print(f"  已脱敏 {len(mapping)} 个标识（账号 UID / 昵称 / 密钥前缀）")
            else:
                print("  未发现需要脱敏的标识")
        else:
            print("  ⚠ 已关闭脱敏：产物含真实账号标识，请勿提交进版本库")

        # ---- 逐页截图 ----
        print(f"[2/2] 抓取 {len(PAGES)} 个页面 → {out_dir}")
        failures: list[str] = []
        for i, (slug, route, label) in enumerate(PAGES, start=1):
            url = f"{base}{route}"
            target = out_dir / f"{i:02d}-{slug}.png"
            suffix = slug.split("-")[-1] if "-" in slug else ""
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                # 轮询组件（概览/账号/任务页都有定时刷新）先等一轮数据落定
                time.sleep(args.settle)

                # 有池页签的页面：点到对应页签再截（两池的列与操作不同）
                tab_index = None
                for suffix_key, idx in TAB_PAGES.get(route, []):
                    if suffix_key == suffix:
                        tab_index = idx
                        break
                if tab_index is not None:
                    tabs = page.query_selector_all("div.mb-3 button")
                    if tab_index < len(tabs):
                        tabs[tab_index].click()
                        time.sleep(1.2)  # 等列表重渲染
                    else:
                        raise RuntimeError(f"页签 #{tab_index} 不存在（找到 {len(tabs)} 个）")

                # 撑高视口以容纳长页面（外壳是内部滚动布局，full_page 够不到折下内容）
                height = _fit_viewport(page)
                page.screenshot(path=str(target), full_page=True)
                size_kb = target.stat().st_size / 1024
                print(f"  [{i}/{len(PAGES)}] {label:<26} {route:<12} → {target.name} "
                      f"({size_kb:.0f} KB, {VIEWPORT['width']}x{height})")
            except Exception as exc:  # noqa: BLE001 - 单页失败不中断整体
                failures.append(f"{label} ({route}): {exc}")
                print(f"  [{i}/{len(PAGES)}] {label:<26} 失败: {exc}")
            finally:
                # 恢复默认视口，避免上一页的尺寸影响下一页布局
                page.set_viewport_size(VIEWPORT)

        browser.close()
    if failures:
        print("\n失败的页面：", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n全部截图完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
