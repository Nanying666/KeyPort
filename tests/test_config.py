"""配置加载与调度器排程测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from keyport import config as cfgmod
from keyport.config import Config, load_config, save_config
from keyport.scheduler import Scheduler

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    """config.example.json 必须能被解析且字段正确（否则用户复制即踩坑）。"""
    path = ROOT / "config.example.json"
    assert path.is_file(), "config.example.json 缺失"
    cfg = load_config(path)
    assert cfg.listen_port == 8790
    assert set(cfg.realms) >= {"cn", "global", "zai"}
    assert cfg.realms["cn"].chat_base == "https://copilot.tencent.com"
    assert cfg.realms["global"].chat_base == "https://www.workbuddy.ai"
    assert cfg.realms["zai"].chat_base == "https://api.z.ai/api/coding/paas/v4"
    assert cfg.pool.top_k == 5
    assert cfg.schedule.checkin_hours == [9, 21]


def test_missing_file_falls_back_to_defaults(tmp_path: Path):
    cfg = load_config(tmp_path / "nope.json")
    assert isinstance(cfg, Config)
    assert cfg.listen_port == 8790


def test_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{ this is not json", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.listen_port == 8790


def test_unknown_keys_ignored(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"listen_port": 9000, "totally_unknown": 1}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.listen_port == 9000
    assert not hasattr(cfg, "totally_unknown")


def test_nested_partial_override_keeps_defaults(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"pool": {"top_k": 9}}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.pool.top_k == 9
    assert cfg.pool.max_in_flight == 3  # 未提供的字段保持默认


def test_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KP_LISTEN_PORT", "9123")
    monkeypatch.setenv("KP_SECURITY__TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("KP_POOL__TOP_K", "7")
    cfg = load_config(tmp_path / "nope.json")
    assert cfg.listen_port == 9123
    assert cfg.security.trust_proxy_headers is True
    assert cfg.pool.top_k == 7


def test_env_invalid_value_ignored(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KP_LISTEN_PORT", "not-a-number")
    cfg = load_config(tmp_path / "nope.json")
    assert cfg.listen_port == 8790  # 回落默认，不崩溃


def test_save_and_reload_roundtrip(tmp_path: Path):
    cfg = Config()
    cfg.listen_port = 8888
    cfg.pool.top_k = 3
    cfg.realms["cn"].chat_base = "https://example.test"
    path = tmp_path / "sub" / "c.json"
    save_config(cfg, path)  # 显式路径：父目录不存在也应自动创建
    assert path.is_file()
    back = load_config(path)
    assert back.listen_port == 8888
    assert back.pool.top_k == 3
    assert back.realms["cn"].chat_base == "https://example.test"


def test_save_config_refuses_without_path_or_source(tmp_path: Path):
    """护栏：临时构造的 Config 不带 source_path，save_config 必须拒绝而非
    静默写全局 config.json（否则测试会污染生产配置）。"""
    cfg = Config(data_dir=str(tmp_path / "data"))
    assert cfg.source_path is None
    with pytest.raises(ValueError, match="拒绝写入"):
        save_config(cfg)


def test_save_config_uses_source_path(tmp_path: Path):
    """load_config 回来的配置，热改落盘应写回原文件。"""
    path = tmp_path / "c.json"
    path.write_text("{}", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.source_path == path
    cfg.pool.top_k = 4
    save_config(cfg)  # 不传 path：应写回 source_path
    assert load_config(path).pool.top_k == 4


def test_save_config_explicit_path_overrides_source(tmp_path: Path):
    src = tmp_path / "src.json"
    src.write_text("{}", encoding="utf-8")
    cfg = load_config(src)
    cfg.pool.top_k = 6
    other = tmp_path / "other.json"
    save_config(cfg, other)
    # 显式路径优先：只写 other，src 保持原样
    assert json.loads(other.read_text(encoding="utf-8"))["pool"]["top_k"] == 6
    assert json.loads(src.read_text(encoding="utf-8")) == {}


def test_config_to_dict_excludes_source_path():
    from keyport.config import config_to_dict

    cfg = Config()
    cfg.source_path = Path("/tmp/x.json")
    assert "source_path" not in config_to_dict(cfg)


def test_load_config_does_not_serialise_source_path(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text("{}", encoding="utf-8")
    cfg = load_config(path)
    save_config(cfg)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "source_path" not in raw


def test_config_path_from_env(monkeypatch, tmp_path: Path):
    target = tmp_path / "xyz.json"
    monkeypatch.setenv("KP_CONFIG_FILE", str(target))
    assert cfgmod.config_path_from_env() == target
    monkeypatch.delenv("KP_CONFIG_FILE")
    assert cfgmod.config_path_from_env() == cfgmod.DEFAULT_CONFIG_PATH


# ---- 调度器排程 ----


class _StubDb:
    """最小可用的假 DB：只记录 task_runs 插入。"""

    def __init__(self):
        self.rows: list[tuple] = []

    def execute(self, sql: str, params=()):
        self.rows.append(params)
        return type("Cur", (), {"rowcount": 1})()

    def query(self, sql: str, params=()):
        return []

    def query_one(self, sql: str, params=()):
        return None


class _StubPool:
    def all_entries(self):
        return []


class _StubClient:
    pass


@pytest.mark.asyncio
async def test_scheduler_fires_only_at_configured_hour(tmp_path):
    cfg = Config(data_dir=str(tmp_path), auth_dir=str(tmp_path / "auths"))
    cfg.schedule.checkin_hours = [9]
    cfg.schedule.checkin_enabled = True
    cfg.schedule.travel_enabled = False
    cfg.schedule.activity_enabled = False
    cfg.schedule.keepalive_enabled = False
    cfg.schedule.cat_enabled = False

    sched = Scheduler(cfg, _StubDb(), _StubPool(), _StubClient(), None)

    lt = time.localtime()
    nine_am = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 30, 0, 0, 0, -1))
    fired = await sched.tick(int(nine_am))
    assert [f["task"] for f in fired] == ["checkin"]

    # 同一小时内重复 tick 不重复触发
    again = await sched.tick(int(nine_am) + 60)
    assert again == []


@pytest.mark.asyncio
async def test_scheduler_skips_when_disabled(tmp_path):
    cfg = Config(data_dir=str(tmp_path), auth_dir=str(tmp_path / "auths"))
    cfg.schedule.checkin_hours = [9]
    cfg.schedule.travel_hours = [9]
    cfg.schedule.checkin_enabled = False
    cfg.schedule.travel_enabled = False  # 默认 travel 也在 9 点，必须一并关闭
    sched = Scheduler(cfg, _StubDb(), _StubPool(), _StubClient(), None)
    lt = time.localtime()
    nine = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 15, 0, 0, 0, -1))
    assert await sched.tick(int(nine)) == []


@pytest.mark.asyncio
async def test_scheduler_status_shape(tmp_path):
    cfg = Config(data_dir=str(tmp_path), auth_dir=str(tmp_path / "auths"))
    sched = Scheduler(cfg, _StubDb(), _StubPool(), _StubClient(), None)
    status = sched.status()
    assert "running" in status
    assert status["schedule"]["checkin_hours"] == [9, 21]
    assert status["schedule"]["keepalive_enabled"] is True
