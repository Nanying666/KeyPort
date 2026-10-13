"""ZCode 端点的人机验证参数（verifyParam）供给。

## 为什么需要

zcode.z.ai 的 Coding Plan 网关对每个请求都要阿里云无痕验证参数
（`X-Aliyun-Captcha-Verify-Param`），缺失即返回
`400 {"code":3007,"msg":"captcha verify failed"}`。

## 关键实测结论（与参考项目的分歧）

参考实现（zcode2api）把求得的 verifyParam **缓存 45 秒复用**。
实测在 KeyPort 本环境**不成立**：

    复用同一参数:  第1次 200 OK  →  第2次 3007  →  第3次 3007
    每请求新参数:  200      →  200      →  200

即 verifyParam 是**一次性**的（内含 certifyId 被消费即失效）。
因此本模块不做长期缓存，改为「农场持续产参数 + 取一个消费一个」。

## 农场

单次求解约 1–3 秒，若每次请求同步等待会把首字延迟抬到秒级。
故后台常驻一个浏览器页按需补货：队列低于 `pool_size` 才求解，
攒够即停（`idle_sleep_ms` 轮询），避免无节制打码触发风控。

## 风控与自愈（实测）

农场曾以 `while(true)` 无节制求解，运行约 25 分钟后开始稳定收到
`F001`（SDK 判定自动化），随后页面异常退出，农场**永久死亡**。

对照实验（各 25 次连续求解）：
    A 同页反复 re-init   → 25/25 成功
    B 每次全新页面       → 25/25 成功
    C 无间隔连打         → 25/25 成功
    D 间隔 300ms         → 25/25 成功

结论：短时高频与反复 re-init **都不是**触发条件，是长时间累计量触发。
故对策是「按需补货 + 失败退避 + 异常自愈重启」，
而不是靠调大间隔硬扛。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("keyport.captcha")

# 上游 /api/v1/client/configs 下发；以服务端下发为准，此处仅兜底
DEFAULT_SCENE_ID = "11xygtvd"
DEFAULT_PREFIX = "no8xfe"
DEFAULT_REGION = "cn"

CONFIG_URL = "https://zcode.z.ai/api/v1/client/configs"
SDK_URL = "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"
FARM_URL = "https://zcode.z.ai/__captcha_farm__"

FARM_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div id="cap"></div><button id="btn"></button>
<script src="{sdk}"></script>
</body></html>"""

# 单次求解：返回参数或 null。农场循环由 Python 侧控制（按需补货、退避、换页）。
_SOLVE_ONCE = """
async (cfg) => new Promise((resolve) => {
  const finish = (v) => resolve(v);
  window.initAliyunCaptcha({
    SceneId: cfg.scene, mode: 'popup', region: cfg.region, prefix: cfg.prefix,
    element: '#cap', button: '#btn', captchaLogoImg: '', showErrorTip: false,
    getInstance: (i) => {
      try { (i.startTracelessVerification || i.show).call(i); } catch (e) {}
    },
    success: (p) => finish({ ok: true, param: p }),
    // 风控判自动化会走这里（F001/F011）。回传原因便于区分「环境被识别」与「配置错误」。
    fail: (r) => finish({ ok: false, reason: JSON.stringify(r) }),
    onError: (e) => finish({ ok: false, reason: JSON.stringify(e) }),
  });
  setTimeout(() => finish({ ok: false, reason: 'TIMEOUT' }), cfg.perAttemptMs);
})
"""


@dataclass
class CaptchaConfig:
    """验证码农场配置（config.json 的 captcha 段）。"""

    enabled: bool = True
    headless: bool = True           # 本环境实测可用；被风控时改 False
    pool_size: int = 3              # 目标库存：队列达到该值即停止求解（省打码量）
    # 参数被上游接受的时效（秒）。**短于**它在队列里的保留时长。
    #
    # 为什么必须有这个：农场在库存满时停止产出，参数就此静置。低流量场景下
    # 几小时后才来请求，取出的全是陈参数，逐个被判 3007。参考项目实测
    # 「48s 的参数已被判 3007」，故取 40s 留余量。
    # 它同时是**产出判据**：池里 3 个全陈时若按数量判断会认为"库存充足"而
    # 永不补货（参考项目记录的「池看着健康、实际一个都不能用」）。
    usable_ms: int = 40000
    per_attempt_ms: int = 15000     # 单次求解超时
    gap_ms: int = 300               # 两次求解之间的间隔
    idle_sleep_ms: int = 1000       # 库存已满时的轮询间隔
    max_wait_seconds: float = 20.0  # 取参数最长等待
    fail_backoff_ms: int = 5000     # 单次求解失败后的退避起始
    fail_backoff_max_ms: int = 120000  # 连续失败退避封顶
    restart_backoff_ms: int = 5000  # 农场异常退出后的重启退避
    restart_backoff_max_ms: int = 300000
    restart_max_consecutive: int = 20  # 连续重启上限（防无限重启风暴）
    chrome_path: str = ""           # 空 = 自动探测
    scene_id: str = DEFAULT_SCENE_ID
    prefix: str = DEFAULT_PREFIX
    region: str = DEFAULT_REGION

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CaptchaConfig:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


def _chrome_candidates(explicit: str = "") -> list[str]:
    """本机 Chrome / Edge 可执行文件候选路径（按优先级）。"""
    cands = [explicit] if explicit else []
    cands += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    out: list[str] = []
    for c in cands:
        if c and c not in out and Path(c).exists():
            out.append(c)
    return out


# 无头模式下使用的 UA 主版本（默认 141）。
#
# ⚠️ 历史误判更正（2026-09-29 实测）：此前注释声称
#     「headless + UA 141 → 5/5 成功；headless + UA 153 → 5/5 F011」
#     并把 F001/F011 归因于 UA 版本与指纹的错配。
# 四象限对照实验（headless × UA{141,153}，各 3 次）结果**全部 3/3 成功**，
# 说明 UA 版本并非失败原因。真因是**同一页面反复 init SDK 污染状态**，
# 见 `_solve_on_page` 的 E/F 对照（复用 1/5 vs 新建 5/5）。
#
# 因此本常量现在只是「一个合理的桌面 UA」，不再是规避风控的关键开关；
# 保留固定值以免每次探测本机版本（多一次 subprocess），并非因为必须错配。
FARM_UA_MAJOR = "141"


def _chrome_major(chrome_path: str) -> str | None:
    """探测本机浏览器真实主版本号（仅用于诊断/有头模式）。"""
    if platform.system() != "Windows" or not chrome_path:
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{chrome_path}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        major = out.split(".")[0]
        return major if major.isdigit() and len(major) >= 2 else None
    except Exception:  # noqa: BLE001
        return None


def _desktop_ua(chrome_path: str, headless: bool = True) -> str:
    """桌面 Chrome UA（覆盖默认的 HeadlessChrome，后者会被 SDK 判自动化）。

    无头模式用固定 `FARM_UA_MAJOR`（见其注释：版本号必须与无头指纹错配）；
    有头模式才用本机真实版本（写死旧版本会与真实浏览器不符 → 上游 3012）。
    """
    if headless:
        major = FARM_UA_MAJOR
    else:
        major = _chrome_major(chrome_path) or FARM_UA_MAJOR
    plat = ("Windows NT 10.0; Win64; x64" if platform.system() == "Windows"
            else "Macintosh; Intel Mac OS X 10_15_7" if platform.system() == "Darwin"
            else "X11; Linux x86_64")
    return (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


def decode_verify_param(param: str) -> dict[str, Any] | None:
    """verifyParam 是 base64(JSON{certifyId, sceneId, isSign, securityToken})。

    解不出来不代表无效（上游可能改编码），仅用于日志诊断。
    """
    try:
        return json.loads(base64.b64decode(param + "=" * (-len(param) % 4)))
    except Exception:  # noqa: BLE001
        return None


@dataclass
class _Farm:
    """一个常驻浏览器 context，按需补货把参数放进队列。"""

    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    # 与 queue 平行的入队时刻（time.monotonic），用于判定参数是否还新鲜。
    # 不用 asyncio.Queue 的 item 携带时间戳，是因为 acquire() 有
    # get_nowait() 快路径，改动它会影响既有调用方。
    born: deque[float] = field(default_factory=deque)
    task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    last_error: str = ""
    produced: int = 0
    failed: int = 0
    discarded: int = 0         # 因超出 usable_ms 被丢弃（产出跟不上/池在空转）
    started_at: float = 0.0
    restarts: int = 0          # 农场异常重启次数
    consecutive_fails: int = 0  # 连续求解失败次数（用于退避）

    def _prune_stale(self, usable_ms: int) -> None:
        """丢弃队首所有过期参数。

        born 与 queue 同步入队，故队首新鲜则后续都新鲜——只需看队首。
        """
        cutoff = time.monotonic() - usable_ms / 1000
        while self.born and self.born[0] < cutoff:
            self.born.popleft()
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.discarded += 1

    def push(self, param: str, usable_ms: int = 40_000) -> bool:
        """入队一个参数；队列已满则丢弃（返回 False）。

        入队前先清掉过期项：队列上限是 32（远大于 pool_size），
        若不清理，农场停产后陈参数会一直堆积到上限才触发丢弃——
        实测 53s 时 queued=6 / fresh=3，队列在无声膨胀。
        """
        self._prune_stale(usable_ms)
        if self.queue.full():
            return False
        self.queue.put_nowait(param)
        self.born.append(time.monotonic())
        return True

    def take_fresh(self, usable_ms: int) -> str | None:
        """取出一个仍在可用时效内的参数；无新鲜参数则返回 None。

        过期参数不返回：递给上游只会换回 3007，还白耗一次换参重试。
        """
        self._prune_stale(usable_ms)
        if self.queue.empty():
            return None
        self.born.popleft()
        return self.queue.get_nowait()

    def fresh_count(self, usable_ms: int) -> int:
        """仍在可用时效内的参数个数——农场据此决定是否补货。

        只数 queue.qsize() 是不够的：池满但全陈时会被误判为"库存充足"，
        农场从此不再产出，而取用时又全被丢弃 → 永久断供。
        """
        cutoff = time.monotonic() - usable_ms / 1000
        return sum(1 for t in self.born if t >= cutoff)


class CaptchaManager:
    """verifyParam 供给：后台农场产出 + 前台取用（用即弃）。"""

    def __init__(self, cfg: CaptchaConfig) -> None:
        self.cfg = cfg
        self._farm = _Farm()
        self._config: dict[str, str] = {}
        self._config_at = 0.0

    # ── 生命周期 ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.cfg.enabled or self._farm.task:
            return
        self._farm.stop.clear()
        self._farm.started_at = time.time()
        self._farm.task = asyncio.create_task(self._farm_loop(), name="captcha-farm")
        log.info("captcha 农场启动 (headless=%s)", self.cfg.headless)

    async def close(self) -> None:
        task, self._farm.task = self._farm.task, None
        if not task:
            return
        self._farm.stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # ── 配置（以服务端下发为准）────────────────────────────────────────────
    async def _fetch_config(self, jwt: str = "") -> dict[str, str]:
        now = time.time()
        if self._config and now - self._config_at < 1800:
            return self._config
        try:
            import httpx

            headers = {"Authorization": f"Bearer {jwt}"} if jwt else {}
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(CONFIG_URL, headers=headers)
            r.raise_for_status()
            got = (((r.json().get("data") or {}).get("configs") or {}).get("captcha") or {})
            if got.get("sceneId"):
                self._config = {
                    "scene_id": got.get("sceneId") or DEFAULT_SCENE_ID,
                    "prefix": got.get("prefix") or DEFAULT_PREFIX,
                    "region": got.get("region") or DEFAULT_REGION,
                }
                self._config_at = now
                log.info("captcha 配置已同步: %s", self._config)
                return self._config
        except Exception as err:  # noqa: BLE001
            log.warning("captcha 配置获取失败，用默认值: %s", err)
        self._config = {"scene_id": self.cfg.scene_id,
                        "prefix": self.cfg.prefix, "region": self.cfg.region}
        self._config_at = now
        return self._config

    # ── 取参数 ──────────────────────────────────────────────────────────────
    async def acquire(self, jwt: str = "") -> str | None:
        """取一个**仍在可用时效内**的参数；农场无货时按需求解一次。

        返回的参数**只能用一次**，用完即弃（见模块文档的一次性实测）。

        过期参数一律不返回：递上去只会换回 3007，还白耗一次换参重试。
        取不到就退化为按需求解，而不是把陈参数塞给上游。
        """
        if not self.cfg.enabled:
            return None
        try:
            fresh = self._farm.take_fresh(self.cfg.usable_ms)
            if fresh:
                return fresh
            if self._farm.task and not self._farm.task.done():
                # 轮询而非 await queue.get()：必须每轮重新判定时效，
                # 直接 await 队列会拿到可能已过期的参数。
                deadline = time.monotonic() + self.cfg.max_wait_seconds
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                    fresh = self._farm.take_fresh(self.cfg.usable_ms)
                    if fresh:
                        return fresh
                log.warning("captcha 农场 %ss 未产出可用参数，转按需求解",
                            self.cfg.max_wait_seconds)
        except Exception as err:  # noqa: BLE001
            log.warning("captcha 取参数异常: %s", err)
        return await self.solve_once()

    async def _solve_on_page(self, ctx: Any) -> dict[str, Any] | None:
        """在一个**全新页面**上求解一次。

        为什么必须每次新建页面：实测对照（headless + UA141，各 5 次）

            E 同一 page 反复 evaluate（旧实现）→ 1/5，失败为 F001 / TIMEOUT
            F 每次新建页面（本实现）          → 5/5

        即在同一个 page 上反复 `initAliyunCaptcha` 会污染 SDK 状态，
        后续求解被判自动化（F001）或直接超时。此前把 F001 归因于
        headless / UA 是**误判**——四象限对照（headless×UA{141,153}）
        全部 3/3 成功，那次实验因每次都新建 context 而恰好绕过了真因。
        """
        page = await ctx.new_page()
        try:
            await page.route(FARM_URL, lambda route: route.fulfill(
                status=200, content_type="text/html",
                body=FARM_HTML.format(sdk=SDK_URL)))
            await page.goto(FARM_URL, wait_until="domcontentloaded")
            return await page.evaluate(_SOLVE_ONCE, {
                "scene": self._cfg()["scene_id"], "region": self._cfg()["region"],
                "prefix": self._cfg()["prefix"],
                "perAttemptMs": self.cfg.per_attempt_ms,
            })
        finally:
            # 页面用即弃：不关会在长时间运行后累积成百上千个孤儿页。
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    def _cfg(self) -> dict[str, str]:
        """当前生效的 SDK 配置（未同步则先用默认值，由调用方保证已 fetch）。"""
        return self._config or {
            "scene_id": self.cfg.scene_id or DEFAULT_SCENE_ID,
            "prefix": self.cfg.prefix or DEFAULT_PREFIX,
            "region": self.cfg.region or DEFAULT_REGION,
        }

    def ensure_running(self) -> None:
        """农场意外死亡时拉起（供运维/健康检查调用，避免长期无供给）。"""
        if not self.cfg.enabled:
            return
        if self._farm.task and not self._farm.task.done():
            return
        log.warning("captcha 农场已停止，尝试重启")
        self._farm.task = asyncio.create_task(self._farm_loop(), name="captcha-farm")

    # ── 农场 ────────────────────────────────────────────────────────────────
    async def _farm_loop(self) -> None:
        """农场主循环：按需补货，异常自愈重启。

        早期实现把 `while(true)` 放在页面内，宿主侧只在队列满时丢弃结果，
        等于无节制打码 —— 运行约 25 分钟后被风控判 F001，页面异常退出后
        农场永久死亡（本函数直接 return，而 start() 只在启动时调用一次）。

        现在改为：求解由 Python 侧按需驱动（队列未满才求），
        单次失败退避、页面异常则退避后重建整个浏览器。
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._farm.last_error = "playwright 未安装"
            log.error("captcha 需要 playwright：pip install playwright")
            return

        chrome = next(iter(_chrome_candidates(self.cfg.chrome_path)), "")
        restart_backoff = self.cfg.restart_backoff_ms
        consecutive_restarts = 0

        async with async_playwright() as pw:
            while not self._farm.stop.is_set():
                try:
                    # 正常返回 = stop 被设置，收工
                    await self._run_farm_session(pw, chrome)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    consecutive_restarts += 1
                    self._farm.restarts += 1
                    self._farm.last_error = f"农场会话异常: {err}"
                    log.error("captcha 农场会话异常（第 %d 次）: %s",
                              consecutive_restarts, err)
                    if consecutive_restarts >= self.cfg.restart_max_consecutive:
                        log.error("captcha 农场连续重启 %d 次，停止自愈（需人工介入）",
                                  consecutive_restarts)
                        return
                    # 退避后重建（页面/浏览器已失效，整体重建最稳）
                    log.info("captcha 农场 %.1fs 后重启", restart_backoff / 1000)
                    try:
                        await asyncio.wait_for(
                            self._farm.stop.wait(), timeout=restart_backoff / 1000)
                        break  # 退避期间收到停止信号
                    except asyncio.TimeoutError:
                        pass
                    restart_backoff = min(restart_backoff * 2,
                                          self.cfg.restart_backoff_max_ms)

    async def _run_farm_session(self, pw: Any, chrome: str) -> None:
        """一次浏览器会话：一个 context，按需补货直到 stop。"""
        # 确保配置已同步（结果写入 self._config，供 _solve_on_page 使用）。
        await self._fetch_config()
        browser = await pw.chromium.launch(
            executable_path=chrome or None,
            headless=self.cfg.headless,
            args=[] if self.cfg.headless else ["--start-minimized"],
        )
        try:
            ctx = await browser.new_context(
                user_agent=_desktop_ua(chrome, self.cfg.headless),
                viewport={"width": 1280, "height": 800})

            fail_backoff = self.cfg.fail_backoff_ms
            while not self._farm.stop.is_set():
                # 判据用队列长度（而非新鲜数）：库存够就停产，**空闲时不打码**。
                #
                # 曾按 fresh_count 判断"库存是否够"，结果空闲时参数每 40s 变陈、
                # 农场随即补货 → 变成 7×24 连续求解（实测 95s 内 produced=9），
                # 既烧打码量又抬风控风险。原设计「池满即停」的意图必须保留。
                #
                # 代价：长时间空闲后首个请求要等一次按需求解（1–3s）。
                # 这是可接受的取舍——空闲时不该消耗账号行为分。
                if self._farm.queue.qsize() >= self.cfg.pool_size:
                    # 库存充足，不求解，静默轮询（省打码量、避风控）
                    self._farm.consecutive_fails = 0
                    fail_backoff = self.cfg.fail_backoff_ms
                    await self._sleep_or_stop(self.cfg.idle_sleep_ms / 1000)
                    continue

                # 每次求解用新页面：同一 page 反复 init 会被 SDK 判自动化（F001）。
                result = await self._solve_on_page(ctx)

                if isinstance(result, dict) and result.get("ok") and result.get("param"):
                    self._farm.push(result["param"], self.cfg.usable_ms)
                    self._farm.produced += 1
                    self._farm.consecutive_fails = 0
                    fail_backoff = self.cfg.fail_backoff_ms
                    await self._sleep_or_stop(self.cfg.gap_ms / 1000)
                    continue

                # 求解失败：可能是风控，也可能是页面/SDK 异常。
                reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) \
                    else "unknown"
                self._farm.failed += 1
                self._farm.consecutive_fails += 1
                self._farm.last_error = f"求解失败: {reason}"[:200]
                log.warning("captcha 求解失败（连续 %d 次）: %s",
                            self._farm.consecutive_fails, reason[:160])
                # 连续失败很可能是页面已失效，交由外层重建浏览器
                if self._farm.consecutive_fails >= 5:
                    raise RuntimeError(f"连续 {self._farm.consecutive_fails} 次求解失败: {reason[:120]}")
                await self._sleep_or_stop(fail_backoff / 1000)
                fail_backoff = min(fail_backoff * 2, self.cfg.fail_backoff_max_ms)
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass

    async def _sleep_or_stop(self, seconds: float) -> None:
        """可被 stop 事件提前打断的等待。"""
        try:
            await asyncio.wait_for(self._farm.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def solve_once(self, conf: dict[str, str] | None = None) -> str | None:
        """按需同步求解一次（农场不可用时的兜底）。"""
        conf = conf or await self._fetch_config()
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None
        chrome = next(iter(_chrome_candidates(self.cfg.chrome_path)), "")
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    executable_path=chrome or None, headless=self.cfg.headless)
                try:
                    ctx = await browser.new_context(
                        user_agent=_desktop_ua(chrome, self.cfg.headless),
                        viewport={"width": 1280, "height": 800})
                    page = await ctx.new_page()
                    await page.route(FARM_URL, lambda route: route.fulfill(
                        status=200, content_type="text/html",
                        body=FARM_HTML.format(sdk=SDK_URL)))
                    await page.goto(FARM_URL, wait_until="domcontentloaded")
                    result = await page.evaluate(_SOLVE_ONCE, {
                        "scene": conf["scene_id"], "region": conf["region"],
                        "prefix": conf["prefix"], "perAttemptMs": self.cfg.per_attempt_ms,
                    })
                    if isinstance(result, dict) and result.get("ok"):
                        return result.get("param") or None
                    reason = (result or {}).get("reason", "unknown") \
                        if isinstance(result, dict) else "unknown"
                    self._farm.last_error = f"按需求解失败: {reason}"[:200]
                    log.warning("captcha 按需求解失败: %s", str(reason)[:160])
                    return None
                finally:
                    try:
                        await browser.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as err:  # noqa: BLE001
            log.warning("captcha 按需求解异常: %s", err)
            return None

    # ── 诊断 ────────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "running": bool(self._farm.task and not self._farm.task.done()),
            "queued": self._farm.queue.qsize(),
            # 新鲜数才是真库存：池满但全陈时 queued 看着健康、fresh 为 0。
            "fresh": self._farm.fresh_count(self.cfg.usable_ms),
            "usable_ms": self.cfg.usable_ms,
            "pool_size": self.cfg.pool_size,
            "produced": self._farm.produced,
            "failed": self._farm.failed,
            "discarded": self._farm.discarded,
            "restarts": self._farm.restarts,
            "consecutive_fails": self._farm.consecutive_fails,
            "headless": self.cfg.headless,
            "last_error": self._farm.last_error,
            "uptime_seconds": (int(time.time() - self._farm.started_at)
                               if self._farm.started_at else 0),
        }


