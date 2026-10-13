"""凭证导入导出测试：格式兼容、容错、往返一致性、失败原因回报。"""

from __future__ import annotations

import json


from keyport.auth.store import parse_auth_file, parse_auth_file_verbose, to_auth_doc
from keyport.upstream.base import AccountAuth

# ---- 解析层 ----


def test_parse_nested_form():
    doc = {
        "auth": {"accessToken": "tok", "refreshToken": "ref", "expiresAt": 1800000000,
                 "domain": "x.workbuddy.ai", "realm": "global"},
        "account": {"uid": "u1", "enterpriseId": "e1", "nickname": "nick"},
    }
    auth = parse_auth_file(json.dumps(doc))
    assert auth is not None
    assert auth.uid == "u1"
    assert auth.access_token == "tok"
    assert auth.refresh_token == "ref"
    assert auth.expires_at == 1800000000
    assert auth.realm == "global"
    assert auth.enterprise_id == "e1"
    assert auth.nickname == "nick"
    assert auth.kind == "oauth"


def test_parse_flat_form():
    doc = {"accessToken": "tok", "refreshToken": "ref", "uid": "u2", "nickname": "n2"}
    auth = parse_auth_file(json.dumps(doc))
    assert auth is not None and auth.uid == "u2" and auth.access_token == "tok"


def test_parse_snake_case_aliases():
    doc = {"access_token": "tok", "refresh_token": "ref", "expires_at": 1700000000,
           "enterprise_id": "e2"}
    auth = parse_auth_file(json.dumps(doc))
    assert auth is not None
    assert auth.access_token == "tok"
    assert auth.refresh_token == "ref"
    assert auth.expires_at == 1700000000
    assert auth.enterprise_id == "e2"


def test_parse_expires_at_iso_string_does_not_raise():
    """回归：expiresAt 写成 ISO 字符串曾直接抛 ValueError，导致整个导入 500。"""
    doc = {"auth": {"accessToken": "tok", "expiresAt": "2026-01-01T00:00:00Z"}}
    auth = parse_auth_file(json.dumps(doc))
    assert auth is not None
    assert auth.expires_at == 1767225600


def test_parse_expires_at_milliseconds():
    doc = {"auth": {"accessToken": "tok", "expiresAt": 1767225600000}}
    auth = parse_auth_file(json.dumps(doc))
    assert auth is not None and auth.expires_at == 1767225600


def test_parse_expires_at_garbage_falls_back_to_zero():
    doc = {"auth": {"accessToken": "tok", "expiresAt": "not-a-time"}}
    auth = parse_auth_file(json.dumps(doc))
    assert auth is not None and auth.expires_at == 0


def test_parse_missing_access_token_returns_none():
    assert parse_auth_file(json.dumps({"auth": {"refreshToken": "r"}})) is None


def test_parse_invalid_json_returns_none():
    assert parse_auth_file("{not json") is None


def test_parse_non_object_returns_none():
    assert parse_auth_file("[1,2,3]") is None


def test_verbose_reports_missing_access_token():
    auth, reason = parse_auth_file_verbose(json.dumps({"auth": {"refreshToken": "r"}}))
    assert auth is None
    assert "accessToken" in reason


def test_verbose_reports_bad_json():
    auth, reason = parse_auth_file_verbose("{broken")
    assert auth is None and "JSON" in reason


def test_apikey_kind_roundtrips():
    """回归：kind 曾不序列化，apikey 账号导出再导入会退化成 oauth，
    之后网关会去调 refresh 端点并失败。"""
    key_acct = AccountAuth(
        uid="apikey-abc", realm="zai", access_token="sk-zai-key",
        refresh_token="", expires_at=0, nickname="zai-1", kind="apikey",
    )
    doc = to_auth_doc(key_acct)
    assert doc["kind"] == "apikey"
    back = parse_auth_file(json.dumps(doc))
    assert back is not None and back.kind == "apikey"


def test_oauth_doc_has_no_kind_field():
    """oauth 凭证保持与官方插件格式一致，不额外写 kind。"""
    doc = to_auth_doc(AccountAuth(uid="u", realm="cn", access_token="t"))
    assert "kind" not in doc


def test_device_token_roundtrips():
    auth = AccountAuth(uid="u", realm="cn", access_token="t", device_token="dev-1")
    back = parse_auth_file(json.dumps(to_auth_doc(auth)))
    assert back is not None and back.device_token == "dev-1"
