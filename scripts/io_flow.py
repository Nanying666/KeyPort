"""真实 uvicorn 下的导入导出端到端验证。"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from keyport.app import create_app  # noqa: E402
from keyport.config import Config  # noqa: E402
from keyport.security import hash_password  # noqa: E402

PORT = 8797
PW = "io-password-123"


def req(path, method="GET", body=None, headers=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        hdrs["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                               data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Set-Cookie", "")


def main() -> int:
    cfg = Config(data_dir="./data/io", auth_dir="./data/io/auths", static_dir="./frontend/out",
                 listen_host="127.0.0.1", listen_port=PORT)
    cfg.security.admin_password_hash = hash_password(PW)
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(60):
        if req("/healthz")[0] in (200, 503):
            break
        time.sleep(0.2)

    checks = []
    st, _, sc = req("/api/login", "POST", {"username": "admin", "password": PW})
    cookie = next((p.strip() for p in sc.split(";") if p.strip().startswith("kp_session=")), "")
    checks.append(("登录", st == 200 and bool(cookie), str(st)))

    # 导入：粘贴一份含包裹层的导出样式文件
    pasted = json.dumps({"version": 1, "accounts": [
        {"auth": {"accessToken": "tok-a", "refreshToken": "r-a", "expiresAt": 1800000000,
                  "realm": "cn"}, "account": {"uid": "acc-a", "nickname": "A"}},
        {"auth": {"accessToken": "sk-zai", "refreshToken": "", "realm": "zai"},
         "account": {"uid": "acc-zai", "nickname": "Z"}, "kind": "apikey"},
        {"accessToken": "flat-tok", "uid": "acc-flat"},
    ]})
    st, body, _ = req("/api/accounts/import", "POST", {"text": pasted}, cookie=cookie)
    ok = st == 200 and json.loads(body)["imported"] == 3
    checks.append(("粘贴导入 3 个账号", ok, f"{st} {body[:80]}"))

    # 导入：坏数据逐条回报原因
    st, body, _ = req("/api/accounts/import", "POST", {"accounts": [
        {"auth": {"accessToken": "ok-tok"}, "account": {"uid": "acc-ok"}},
        {"auth": {"refreshToken": "no-access"}},
    ]}, cookie=cookie)
    parsed = json.loads(body)
    ok = parsed["imported"] == 1 and parsed["skipped"] == 1 and "accessToken" in parsed["failed"][0]["reason"]
    checks.append(("失败逐条回报原因", ok, f"{parsed['failed']}"))

    # 导出
    st, body, _ = req("/api/accounts/export", cookie=cookie)
    exported = json.loads(body)
    checks.append(("导出含 4 个账号", st == 200 and exported["count"] == 4, f"count={exported['count']}"))

    docs = {d["account"]["uid"]: d for d in exported["accounts"]}
    checks.append(("导出保留 realm", docs["acc-zai"]["auth"]["realm"] == "zai",
                   docs["acc-zai"]["auth"]["realm"]))
    checks.append(("导出保留 kind", docs.get("acc-zai", {}).get("kind") == "apikey",
                   str(docs.get("acc-zai", {}).get("kind"))))

    # 下载端点
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/accounts/export/download",
                               headers={"Cookie": cookie})
    with urllib.request.urlopen(r, timeout=20) as resp:
        cd = resp.headers.get("content-disposition", "")
    checks.append(("下载端点带 attachment", "attachment" in cd and "keyport-accounts" in cd, cd[:60]))

    # 清空后回灌导出文件 → 字段零丢失
    for uid in ("acc-a", "acc-zai", "acc-flat", "acc-ok"):
        req(f"/api/accounts/{uid}", "DELETE", cookie=cookie)
    st, body, _ = req("/api/accounts", cookie=cookie)
    checks.append(("清空账号", json.loads(body)["accounts"] == [], body[:40]))

    st, body, _ = req("/api/accounts/import", "POST", exported, cookie=cookie)
    parsed = json.loads(body)
    checks.append(("回灌导出文件", st == 200 and parsed["imported"] == 4 and parsed["skipped"] == 0,
                   f"imported={parsed['imported']} skipped={parsed['skipped']}"))

    st, body, _ = req("/api/accounts", cookie=cookie)
    listed = {a["uid"]: a for a in json.loads(body)["accounts"]}
    ok = (listed["acc-zai"]["kind"] == "apikey" and listed["acc-zai"]["realm"] == "zai"
          and listed["acc-a"]["realm"] == "cn" and listed["acc-flat"]["nickname"] == "")
    checks.append(("回灌后字段零丢失", ok, f"zai.kind={listed['acc-zai']['kind']}"))

    # auths/ 落盘（用配置里的实际路径，而非写死 ./auths）
    files = sorted(p.name for p in cfg.auth_path().glob("workbuddy*.json"))
    checks.append(("凭证落盘 auths/", len(files) >= 4, ",".join(files)))

    server.should_exit = True
    t.join(timeout=10)

    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:26} {detail}")
    fails = [n for n, ok, _ in checks if not ok]
    print("\n失败项：", fails or "无")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
