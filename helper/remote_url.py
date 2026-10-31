"""Portable URL validation shared by the desktop uploader and stdio MCP bridge."""
from urllib.parse import urlsplit


def validate_server_url(value):
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or '\\' in value or any(c.isspace() for c in value)):
        raise ValueError('请输入服务端 HTTP 或 HTTPS 地址（不能包含用户名、查询参数或片段）')
    try:
        parsed.port
    except ValueError:
        raise ValueError('服务端端口无效') from None
    return value
