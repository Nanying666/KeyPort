"""KeyPort 入口：`python -m keyport` 或 `keyport` 命令启动网关 + 管理台。"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .config import get_config, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keyport", description="KeyPort 网关与管理台")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.json）")
    parser.add_argument("--host", default=None, help="监听地址（覆盖配置）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置）")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    parser.add_argument("--log-level", default="info", help="日志级别")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config) if args.config else get_config()
    if args.host:
        cfg.listen_host = args.host
    if args.port:
        cfg.listen_port = args.port

    if args.reload:
        # reload 模式由 uvicorn 自行导入应用字符串
        uvicorn.run("keyport.app:create_app", factory=True, host=cfg.listen_host,
                    port=cfg.listen_port, reload=True, log_level=args.log_level)
        return 0

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
