"""Retrieve a WeChat-hosted emoji using only its authenticated message metadata."""
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

from .platform_media import media_type

MAX_EMOJI_BYTES = 10 * 1024 * 1024
EMOJI_HOSTS = {'mmbiz.qpic.cn', 'emoji.qpic.cn'}


def fetch_emoji(raw):
    try:
        root = ET.fromstring(raw.split(':\n', 1)[-1])
        emoji = root if root.tag == 'emoji' else root.find('.//emoji')
        if emoji is None:
            raise ValueError('表情消息格式无效')
        expected = emoji.get('md5', '').lower()
        address = emoji.get('cdnurl', '')
        if not re.fullmatch(r'[0-9a-f]{32}', expected) or len(address) > 4096:
            raise ValueError('表情资源标识无效')
        parts = urlsplit(address)
        if (parts.scheme not in {'http', 'https'} or parts.hostname not in EMOJI_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443)):
            raise ValueError('表情资源地址不受支持')
        # Do not pass credentials, redirects, proxy settings or arbitrary hosts
        # from a chat message to the server's network client.
        url = urlunsplit(('https', parts.hostname, parts.path, parts.query, ''))
        with httpx.Client(timeout=httpx.Timeout(12, connect=5), follow_redirects=False, trust_env=False) as client:
            for attempt in range(2):
                try:
                    with client.stream('GET', url) as response:
                        if response.status_code >= 500 and attempt == 0:
                            continue
                        if response.status_code != 200:
                            raise ValueError('表情资源暂不可用')
                        content = bytearray()
                        for block in response.iter_bytes():
                            content.extend(block)
                            if len(content) > MAX_EMOJI_BYTES:
                                raise ValueError('表情资源过大')
                    break
                except httpx.TransportError:
                    if attempt:
                        raise
        data = bytes(content)
        if hashlib.md5(data).hexdigest() != expected:
            raise ValueError('表情资源与消息不匹配')
        return data, media_type(47, data[:32])
    except (ET.ParseError, httpx.HTTPError, UnicodeError):
        raise ValueError('表情资源暂不可用') from None
