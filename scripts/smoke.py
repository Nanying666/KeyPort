"""开发期冒烟脚本：验证配置/数据库/安全原语可导入可用。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keyport import config, db, security  # noqa: E402


def main() -> int:
    cfg = config.get_config()
    print("realms:", list(cfg.realms))
    print("enabled realms:", cfg.enabled_realms())
    print("db path:", cfg.resolved_db_path())
    print("static dir:", cfg.resolved_static_dir())

    database = db.init_db(cfg.resolved_db_path())
    tables = [r["name"] for r in database.query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("tables:", tables)

    h = security.hash_password("hunter2")
    print("pw ok:", security.verify_password("hunter2", h),
          "pw bad:", security.verify_password("wrong", h))

    key = security.generate_api_key()
    print("key:", security.key_prefix(key), "verify:",
          security.verify_api_key(key, security.hash_api_key(key)))

    print("ip allow:", security.evaluate_ip("10.0.0.5", ["10.0.0.0/24"], []))
    print("ip block:", security.evaluate_ip("10.0.0.5", [], ["10.0.0.0/24"]))
    print("bad cidr:", security.validate_cidr_list(["10.0.0.0/24", "nope"]))

    # 往返一次配置落盘
    tmp = Path(__file__).parent / "data" / "_smoke_config.json"
    config.save_config(cfg, tmp)
    back = config.load_config(tmp)
    print("roundtrip realms:", list(back.realms), "port:", back.listen_port)
    tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
