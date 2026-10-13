"""ZCode 账号包（zcode-accounts-bundle）解析测试。"""

from __future__ import annotations

import json

from keyport.auth.zcode import (
    ZCODE_START_PLAN_BASE,
    is_zcode_bundle,
    parse_zcode_bundle,
)

# 与用户实际导出文件同构的样本（令牌已替换为占位符）
SAMPLE = {
    "format": "zcode-accounts-bundle",
    "version": 2,
    "exportedAt": "2026-09-26 16:53",
    "accounts": [
        {
            "name": "iyzcfc",
            "createdAt": "2026-09-26 16:52",
            "credentials": {
                "oauth:active_provider": "zai",
                "oauth:zai:access_token": "PLATFORM_TOKEN_xxx",
                "oauth:zai:user_info": json.dumps({
                    "id": "fe9be650-56a2-46be-8a50-db8d44e17fb4",
                    "displayName": "iyzcfc",
                    "username": "iyzcfc",
                }),
                "zcodejwttoken": "CODING_PLAN_JWT_yyy",
            },
            "virtualArmsUid": "uid_ediilp3hfowr1pt9",
            "virtualDeviceMid": "78b921b4-ef0e-47cf-a333-555a14baa2f1",
            "config": {
                "provider": {
                    "builtin:zai": {
                        "enabled": False, "kind": "anthropic", "name": "Z.ai - API Key",
                        "options": {"apiKey": "", "apiKeyRequired": True,
                                    "baseURL": "https://api.z.ai/api/anthropic"},
                        "source": "custom",
                    },
                    "builtin:zai-start-plan": {
                        "enabled": True, "kind": "anthropic", "name": "Z.ai - Coding Plan",
                        "options": {"apiKey": "CODING_PLAN_JWT_yyy",
                                    "baseURL": "https://zcode.z.ai/api/v1/zcode-plan/anthropic"},
                        "source": "custom",
                    },
                }
            },
        }
    ],
}


def test_detects_bundle_format():
    assert is_zcode_bundle(SAMPLE) is True
    assert is_zcode_bundle({"format": "other"}) is False
    assert is_zcode_bundle(None) is False


def test_parses_account_with_all_fields():
    cands, failed = parse_zcode_bundle(SAMPLE)
    assert failed == []
    assert len(cands) == 1
    c = cands[0]
    assert c.uid == "fe9be650-56a2-46be-8a50-db8d44e17fb4"
    assert c.nickname == "iyzcfc"
    assert c.realm == "zai"
    assert c.kind == "apikey"
    assert c.protocol == "anthropic"
    assert c.base_url == ZCODE_START_PLAN_BASE
    assert c.device_token == "78b921b4-ef0e-47cf-a333-555a14baa2f1"
    assert c.token == "CODING_PLAN_JWT_yyy"
    assert c.provider_id == "builtin:zai-start-plan"


def test_uses_enabled_provider_base_url():
    """启用的 provider 的 baseURL 必须被采用，而不是写死内置常量。"""
    doc = json.loads(json.dumps(SAMPLE))
    doc["accounts"][0]["config"]["provider"]["builtin:zai-start-plan"]["options"]["baseURL"] = (
        "https://custom.example.com/anthropic"
    )
    cands, _ = parse_zcode_bundle(doc)
    assert cands[0].base_url == "https://custom.example.com/anthropic"


def test_falls_back_to_jwt_when_provider_has_no_key():
    doc = json.loads(json.dumps(SAMPLE))
    doc["accounts"][0]["config"]["provider"]["builtin:zai-start-plan"]["options"]["apiKey"] = ""
    cands, _ = parse_zcode_bundle(doc)
    assert cands[0].token == "CODING_PLAN_JWT_yyy"
    assert cands[0].base_url == ZCODE_START_PLAN_BASE


def test_falls_back_to_platform_token_when_no_jwt():
    doc = json.loads(json.dumps(SAMPLE))
    del doc["accounts"][0]["credentials"]["zcodejwttoken"]
    doc["accounts"][0]["config"] = {"provider": {}}
    cands, _ = parse_zcode_bundle(doc)
    assert cands[0].token == "PLATFORM_TOKEN_xxx"
    assert cands[0].base_url == "https://api.z.ai/api/anthropic"


def test_reports_failure_when_no_token():
    doc = json.loads(json.dumps(SAMPLE))
    del doc["accounts"][0]["credentials"]["zcodejwttoken"]
    doc["accounts"][0]["credentials"].pop("oauth:zai:access_token")
    doc["accounts"][0]["config"] = {"provider": {}}
    cands, failed = parse_zcode_bundle(doc)
    assert cands == []
    assert len(failed) == 1
    assert "没有可用令牌" in failed[0]["reason"]


def test_reports_failure_when_credentials_missing():
    doc = {"format": "zcode-accounts-bundle", "accounts": [{"name": "x"}]}
    cands, failed = parse_zcode_bundle(doc)
    assert cands == []
    assert "credentials" in failed[0]["reason"]


def test_reports_failure_when_accounts_not_array():
    cands, failed = parse_zcode_bundle({"format": "zcode-accounts-bundle"})
    assert cands == []
    assert "accounts" in failed[0]["reason"]


def test_uid_falls_back_when_user_info_missing():
    doc = json.loads(json.dumps(SAMPLE))
    doc["accounts"][0]["credentials"].pop("oauth:zai:user_info")
    doc["accounts"][0].pop("virtualArmsUid")
    cands, _ = parse_zcode_bundle(doc)
    assert len(cands) == 1
    assert cands[0].uid.startswith("zcode-")


def test_uid_is_stable_across_reparse():
    doc = json.loads(json.dumps(SAMPLE))
    doc["accounts"][0]["credentials"].pop("oauth:zai:user_info")
    doc["accounts"][0].pop("virtualArmsUid")
    first, _ = parse_zcode_bundle(doc)
    second, _ = parse_zcode_bundle(json.loads(json.dumps(doc)))
    assert first[0].uid == second[0].uid


def test_multiple_accounts():
    doc = json.loads(json.dumps(SAMPLE))
    second = json.loads(json.dumps(SAMPLE["accounts"][0]))
    second["name"] = "other"
    second["credentials"]["oauth:zai:user_info"] = json.dumps({"id": "uid-2"})
    doc["accounts"].append(second)
    cands, failed = parse_zcode_bundle(doc)
    assert len(cands) == 2 and failed == []
    assert {c.uid for c in cands} == {"fe9be650-56a2-46be-8a50-db8d44e17fb4", "uid-2"}


def test_to_auth_maps_to_anthropic_protocol():
    cands, _ = parse_zcode_bundle(SAMPLE)
    auth = cands[0].to_auth()
    assert auth.is_anthropic() is True
    assert auth.is_zai() is True
    assert auth.kind == "apikey"
    assert auth.device_token == "78b921b4-ef0e-47cf-a333-555a14baa2f1"
    assert auth.base_url == ZCODE_START_PLAN_BASE
