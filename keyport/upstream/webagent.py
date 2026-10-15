"""驱动网页端（云 agent）会话跑完一轮 —— ACP over streamable HTTP。

## 为什么必须驱动，而不是只建会话

`POST /console/as/conversations/` 只是**排队**了一条会话。agent 要等客户端接上这条
会话的沙箱、并请求这一轮，才会真的跑起来 —— 只建会话的会话**永远停在 CREATING、
没有任何输出**，也就不算一次有效对话。

而官方「每日活跃」认的正是「跑完的 agent 会话」。所以这里是国际版打卡的关键一环：
不驱动 = 白建一条会话 + 拿不到那份积分。

## 协议

网页端的做法是 ACP（Agent Client Protocol）的 streamable-HTTP 传输：

1. `GET <沙箱 link>`，`Accept: text/event-stream` → 服务端返回 SSE 流，
   响应头带 `Acp-Connection-Id`；
2. `POST <沙箱 link>`（带 `Acp-Connection-Id`）发 JSON-RPC：
   `initialize` → `session/load` → `session/set_model` → `session/prompt`；
3. 服务端把 `session/update` 通知推回 SSE 流，直到这一轮结束。

本模块只实现打卡需要的那点协议：**不带工具、不接终端、不回调文件系统** ——
`CLIENT_CAPABILITIES` 里把能力全部关掉。能力开得越多，沙箱越可能停下来等客户端
响应它根本不需要的请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

#: ACP 协议版本
PROTOCOL_VERSION = 1

#: 打卡只需要 agent 把话说完，不需要客户端提供文件系统 / 终端能力。
CLIENT_CAPABILITIES: dict[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}

#: JSON-RPC 请求序号（固定顺序，便于排查时对照日志）
_RID_INITIALIZE = 1
_RID_SESSION_LOAD = 2
_RID_SET_MODEL = 3
_RID_PROMPT = 4

#: 单条 HTTP 请求超时（SSE 长连接另算）
REQUEST_TIMEOUT = 30.0

#: 会话已跑完 / 已失败的状态值
STATUS_COMPLETED = "completed"
STATUS_FAILED = ("failed", "error")


class AcpError(Exception):
    """驱动失败时抛这个（调用方转成可读的错误文本）。"""


class AcpChannel:
    """ACP 的 streamable-HTTP 传输：一条 SSE 收事件，POST 发请求。

    与 `httpx.AsyncClient` 共用连接池（因此账号级出口代理自动生效 ——
    沙箱 link 是另一个主机，但走的是同一个 client）。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        link: str,
        token: str,
        user_agent: str = "",
        *,
        log: Callable[..., None] | None = None,
    ):
        self._client = client
        self.link = link
        self.token = token
        self.user_agent = user_agent
        self._log = log or (lambda *a, **k: None)
        self.connection_id = ""
        self.updates = 0
        self.chunks = 0
        self._response: httpx.Response | None = None
        self._reader: asyncio.Task | None = None

    # ---- 连接 ----

    def _headers(self, **extra: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        headers.update(extra)
        return headers

    async def open(self) -> str:
        """建立 SSE 通道，返回服务端给的 `Acp-Connection-Id`。"""
        if not self.link:
            raise AcpError("沙箱地址不可用（link 为空）")
        request = self._client.build_request(
            "GET", self.link, headers=self._headers(Accept="text/event-stream"),
            timeout=REQUEST_TIMEOUT,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise AcpError(f"SSE 通道连接失败: {exc}") from exc
        if response.status_code != 200:
            await response.aclose()
            raise AcpError(f"SSE 通道返回 HTTP {response.status_code}")
        connection_id = response.headers.get("Acp-Connection-Id") or ""
        if not connection_id:
            await response.aclose()
            raise AcpError("SSE 通道没有返回 Acp-Connection-Id")
        self._response = response
        self.connection_id = connection_id
        self._reader = asyncio.create_task(self._read_events(response))
        return connection_id

    async def _read_events(self, response: httpx.Response) -> None:
        """把 SSE 流解析成计数（本模块只关心「有多少更新 / 多少输出段」）。

        刻意**不缓存消息内容**：打卡不需要 agent 说了什么，只关心它跑完了。
        留下内容会让内存随轮次线性增长，也可能把模型输出写进日志。
        """
        try:
            async for line in response.aiter_lines():
                text = line.strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "session/update":
                    update = (message.get("params") or {}).get("update") or {}
                    self.updates += 1
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        self.chunks += 1
        except (httpx.HTTPError, asyncio.CancelledError):
            pass  # 连接被关掉 / 主动取消：正常收尾路径

    async def request(self, method: str, params: dict[str, Any], request_id: int) -> int:
        """发一个 JSON-RPC 请求；返回 HTTP 状态码（正常是 202）。"""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        ).encode()
        headers = self._headers(
            **{
                "Content-Type": "application/json",
                "Acp-Connection-Id": self.connection_id,
            }
        )
        try:
            response = await self._client.post(
                self.link, content=body, headers=headers, timeout=REQUEST_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise AcpError(f"{method} 请求失败: {exc}") from exc
        if response.status_code not in (200, 202):
            raise AcpError(f"{method} 返回 HTTP {response.status_code}")
        return response.status_code

    async def close(self, grace: float = 0.5) -> None:
        """收尾：先给读取任务一点时间**排空已到达的事件**，再断开。

        直接取消会丢掉「已经到达但还没被消费」的尾部分片，让 `chunks` 偏低 ——
        而这个计数正是运维用来判断「会话到底跑没跑」的依据（光看「成功」
        分不清是真跑完了，还是只有桌面腿过了）。

        `grace` 是宽限上限：真实 SSE 是长连接，服务端不会主动关流，所以等不到
        就得主动取消，不能无限等。
        """
        reader, self._reader = self._reader, None
        response, self._response = self._response, None

        if reader is not None and not reader.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(reader), timeout=grace)
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(Exception):
                await reader
        if response is not None:
            with contextlib.suppress(Exception):
                await response.aclose()


async def _set_model(
    channel: AcpChannel, session_id: str, model: str,
    log: Callable[..., None],
) -> bool:
    """把这一轮用的模型钉到会话上。

    **这一步不能省**：建会话请求体里的 `model` 只写进会话的 manifest，真正决定
    这一轮用哪个模型的是这条 ACP 扩展请求。少了它，会话跑的是**账号默认模型**
    （网页端默认 Auto、按付费模型计费）—— 于是每天的活跃都在**扣积分**。

    失败不当致命错：这一轮还是要跑完，只是模型可能没切成，日志里留一句。
    """
    try:
        await channel.request(
            "session/set_model", {"sessionId": session_id, "modelId": model},
            _RID_SET_MODEL,
        )
        return True
    except AcpError as exc:
        log("设置模型 %s 失败（%s），本轮仍按会话默认模型跑", model, exc)
        return False


async def run_turn(
    client: httpx.AsyncClient,
    *,
    link: str,
    token: str,
    session_id: str,
    cwd: str = "/workspace",
    prompt: str,
    user_agent: str = "",
    poll_status: Callable[[], Awaitable[str]] | None = None,
    wait_seconds: float = 120.0,
    poll_interval: float = 3.0,
    model: str = "",
    log: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """接上沙箱、把这一轮跑完；返回结果字典。

    `poll_status` 是调用方给的「这条会话现在什么状态」（查 console API）：看到
    `completed` 就算跑完，`failed`/`error` 直接停。返回里带事件数与输出段数，
    方便在打卡结果里一眼看出**到底跑没跑**（光看「成功」分不清是真跑完了，
    还是只有桌面腿过了）。
    """
    log = log or (lambda *a, **k: None)
    started = time.monotonic()
    channel = AcpChannel(client, link, token, user_agent, log=log)
    status = ""
    error = ""
    finished = False
    try:
        await channel.open()
        await channel.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": CLIENT_CAPABILITIES},
            _RID_INITIALIZE,
        )
        await channel.request(
            "session/load",
            {"sessionId": session_id, "cwd": cwd or "/workspace", "mcpServers": []},
            _RID_SESSION_LOAD,
        )
        if model:
            await _set_model(channel, session_id, model, log)
        await channel.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
            _RID_PROMPT,
        )

        deadline = time.monotonic() + max(1.0, float(wait_seconds))
        while time.monotonic() < deadline:
            if poll_status is not None:
                try:
                    status = str(await poll_status() or "")
                except Exception as exc:  # noqa: BLE001 - 查状态失败不该中断等待
                    log("状态查询失败（%s）", exc)
                if status == STATUS_COMPLETED:
                    finished = True
                    break
                if status in STATUS_FAILED:
                    error = f"会话状态={status}"
                    break
            if poll_interval > 0:
                await asyncio.sleep(poll_interval)
    except AcpError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 驱动异常一律转成结果里的 error
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await channel.close()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not status and poll_status is not None:
        with contextlib.suppress(Exception):
            status = str(await poll_status() or "")
    if not finished and not error:
        error = f"会话在 {wait_seconds}s 内没有跑完（状态={status or '未知'}）"

    return {
        "ok": bool(finished and not error),
        "status": status,
        "events": channel.updates,
        "chunks": channel.chunks,
        "elapsed_ms": elapsed_ms,
        "error": error,
    }
