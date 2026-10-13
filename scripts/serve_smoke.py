"""部署冒烟：真实启动 uvicorn，验证静态托管、健康检查与管理台可访问。

用法：python scripts/serve_smoke.py
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import threading  # noqa: E402

import uvicorn  # noqa: E402

from keyport.app import create_app  # noqa: E402
from keyport.config import Config  # noqa: E402

PORT = 8799


def fetch(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main() -> int:
    cfg = Config(
        data_dir="./data",
        auth_dir="./auths",
        listen_host="127.0.0.1",
        listen_port=PORT,
    )
    app = create_app(cfg)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # 等待就绪
    for _ in range(60):
        try:
            status, _ = fetch("/healthz")
            if status in (200, 503):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)

    results: list[tuple[str, bool, str]] = []

    status, body = fetch("/healthz")
    ok = status in (200, 503) and "keyport" in body
    results.append(("/healthz", ok, f"{status} {body[:80]}"))

    status, body = fetch("/")
    ok = status == 200 and "<!doctype html" in body.lower()
    results.append(("/ (管理台 index.html)", ok, f"{status} len={len(body)}"))

    status, body = fetch("/login/")
    ok = status == 200 and "<!doctype html" in body.lower()
    results.append(("/login/ (SPA 路由)", ok, f"{status} len={len(body)}"))

    status, body = fetch("/settings/")
    ok = status == 200 and "<!doctype html" in body.lower()
    results.append(("/settings/ (SPA 路由)", ok, f"{status} len={len(body)}"))

    # 静态资源：从 index.html 里取一个 _next 资源并验证可访问
    _, index_html = fetch("/")
    asset = ""
    for token in index_html.split('"'):
        if token.startswith("/_next/"):
            asset = token
            break
    if asset:
        status, _ = fetch(asset)
        results.append((f"{asset} (静态资源)", status == 200, str(status)))

    # 未授权 API 应 401
    status, body = fetch("/api/accounts")
    results.append(("/api/accounts 未登录", status == 401, f"{status} {body[:60]}"))

    # /v1 未授权应 401
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/models", method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            v1_status = resp.status
    except urllib.error.HTTPError as exc:
        v1_status = exc.code
    results.append(("/v1/models 无密钥", v1_status == 401, str(v1_status)))

    server.should_exit = True
    thread.join(timeout=10)

    print(json.dumps([{"check": c, "ok": ok, "detail": d} for c, ok, d in results],
                     ensure_ascii=False, indent=2))
    failures = [c for c, ok, _ in results if not ok]
    print("\n失败项：", failures or "无")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
