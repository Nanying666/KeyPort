"""SPA 静态托管的路径解析。

回归背景：静态导出（trailingSlash: true）下 `/login/` 对应的是
`login/index.html`（**目录**），而早期实现只判断 `candidate.is_file()`，
于是所有子路径都回落到根 `index.html` → 浏览器按根页面的 React 树
去 hydration 子路由 URL → 路由不匹配 → 整棵树卸载 → **白屏**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app


@pytest.fixture()
def static_dir(tmp_path: Path) -> Path:
    """构造一个最小静态导出产物（含目录索引与 RSC .txt）。"""
    out = tmp_path / "out"
    (out / "login").mkdir(parents=True)
    (out / "accounts").mkdir(parents=True)
    (out / "_next" / "static").mkdir(parents=True)
    (out / "index.html").write_text("<html>ROOT</html>", encoding="utf-8")
    (out / "login" / "index.html").write_text("<html>LOGIN</html>", encoding="utf-8")
    (out / "accounts" / "index.html").write_text("<html>ACCOUNTS</html>", encoding="utf-8")
    (out / "login" / "index.txt").write_text("rsc-payload", encoding="utf-8")
    (out / "_next" / "static" / "app.js").write_text("//js", encoding="utf-8")
    (out / "404.html").write_text("<html>404</html>", encoding="utf-8")
    return out


@pytest.fixture()
def client(tmp_path: Path, static_dir: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    cfg.static_dir = str(static_dir)
    with TestClient(create_app(cfg)) as tc:
        yield tc


def test_root_serves_root_index(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "ROOT" in r.text


def test_directory_route_serves_its_own_index(client: TestClient):
    """核心回归：/login/ 必须返回 login/index.html，而不是根 index.html。"""
    r = client.get("/login/")
    assert r.status_code == 200
    assert "LOGIN" in r.text
    assert "ROOT" not in r.text


def test_each_directory_route_is_distinct(client: TestClient):
    assert "ACCOUNTS" in client.get("/accounts/").text
    assert "LOGIN" in client.get("/login/").text


def test_missing_trailing_slash_redirects(client: TestClient):
    """/login 需重定向到 /login/，否则页面内相对资源解析错位。"""
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/login/"


def test_static_asset_served(client: TestClient):
    r = client.get("/_next/static/app.js")
    assert r.status_code == 200
    assert "//js" in r.text


def test_rsc_txt_not_falling_back_to_html(client: TestClient):
    """RSC 预取缺失时应 404，回落到 HTML 会让客户端解析失败。"""
    r = client.get("/accounts/index.txt")
    assert r.status_code == 404


def test_unknown_path_falls_back_to_root_for_spa(client: TestClient):
    r = client.get("/some/deep/unknown/route")
    assert r.status_code == 200
    assert "ROOT" in r.text


def test_api_prefix_not_swallowed(client: TestClient):
    """SPA 回退不能吞掉 /api 前缀。"""
    assert client.get("/api/nonexistent").status_code == 404


def test_directory_traversal_blocked(client: TestClient):
    r = client.get("/../../etc/passwd")
    assert r.status_code in (200, 404)
    assert "root:" not in r.text
