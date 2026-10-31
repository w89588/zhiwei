"""Portable server entry point. Configure credentials through the environment."""
import argparse
import logging
from pathlib import Path

import uvicorn


def main():
    parser = argparse.ArgumentParser(description='微信对话存储与 MCP 服务端')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--env-file', default='.env', help='环境配置文件；已有环境变量优先')
    args = parser.parse_args()
    from dotenv import load_dotenv
    if Path(args.env_file).is_file():
        load_dotenv(args.env_file, override=False, interpolate=False)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    from backend.app import from_environment
    uvicorn.run(from_environment(), host=args.host, port=args.port, log_config=None, access_log=False)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Driver exceptions may embed connection credentials; only expose safe app diagnostics.
        from backend.migrations import CompatibilityError, MigrationError
        if isinstance(error, (CompatibilityError, MigrationError)):
            logging.error('%s', error)
        else:
            logging.error('服务端启动失败，请检查环境配置、数据库状态与数据库授权（%s）', type(error).__name__)
        raise SystemExit(1)
