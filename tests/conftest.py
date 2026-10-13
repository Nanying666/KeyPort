"""测试公共夹具。

关键约定：测试用 Config 必须是「文件支撑」的（设置 source_path），
否则 save_config 会拒绝写入 —— 这是防止测试污染生产 config.json 的护栏。

放在 conftest.py 里，pytest 会把 tests/ 加入 sys.path，故各测试文件
可直接 `from conftest import make_test_config`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from keyport.config import Config


def make_test_config(tmp_path: Path, **overrides) -> Config:
    """构造隔离的、文件支撑的测试配置。

    默认关闭 captcha：求解要真启动浏览器，单测不应依赖它。
    需要覆盖该路径的用例自行传 `captcha=CaptchaConfig(enabled=True)`。
    """
    cfg = Config(
        data_dir=str(tmp_path / "data"),
        auth_dir=str(tmp_path / "auths"),
        static_dir=str(tmp_path / "no-static"),
        **overrides,
    )
    if "captcha" not in overrides:
        cfg.captcha.enabled = False
    # 指向临时文件：热改落盘只写这里，绝不碰项目根的 config.json
    cfg.source_path = tmp_path / "config.json"
    return cfg


@pytest.fixture()
def test_config(tmp_path: Path) -> Config:
    return make_test_config(tmp_path)
