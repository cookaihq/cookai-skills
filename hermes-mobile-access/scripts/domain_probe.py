"""User-selected occupancy policy: normal HTTP access means occupied.

This is an access observation, not an FRPS registry query. No credential,
cookie, environment proxy or redirect is used. Registration still handles
conflicts if an apparently unused route was already registered elsewhere.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

import aiohttp

from configuration import ConfigError, LABEL

# A single lightweight preflight, shorter than metadata queries. No retry or
# response body is needed: the user's rule classifies this access attempt.
REQUEST_BUDGET = 8
WORKER_BUDGET = 12


def server_url(prefix, suffix):
    if (not isinstance(prefix, str) or not isinstance(suffix, str)
            or not LABEL.fullmatch(prefix.lower()) or not suffix
            or not all(LABEL.fullmatch(label) for label in suffix.lower().split('.'))
            or len(prefix + '.' + suffix) > 253):
        raise ConfigError('domain_invalid', '请使用单个服务子域名及 FRP Launch 提供的有效父域。')
    return 'https://' + prefix.lower() + '.' + suffix.lower() + '/'


def validate_url(value):
    if not isinstance(value, str):
        raise ConfigError('domain_invalid', '域名检查需要完整 HTTPS Server URL。')
    parsed = urlsplit(value)
    labels = (parsed.hostname or '').split('.')
    if (parsed.scheme != 'https' or parsed.netloc != parsed.hostname or parsed.path != '/'
            or parsed.query or parsed.fragment or len(labels) < 2
            or not all(LABEL.fullmatch(label) for label in labels)
            or len(parsed.hostname or '') > 253):
        raise ConfigError('domain_invalid', '域名检查只接受 FRP 配置派生的 HTTPS 基础地址。')
    return value


def observation(url, status, reason, elapsed):
    occupied = status is not None and 200 <= status < 400
    return {'result': 'domain_checked', 'server_url': url,
            'occupancy': 'occupied' if occupied else 'unoccupied',
            'policy': 'normal_http_occupied', 'http_status': status, 'reason': reason,
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'elapsed_ms': round(elapsed * 1000), 'registration': 'not_checked'}


async def observe(url, *, budget=REQUEST_BUDGET):
    """Transport seam; callers validate the generated HTTPS URL before use."""
    start = time.monotonic()
    status = None
    try:
        timeout = aiohttp.ClientTimeout(total=budget, connect=min(3, budget), sock_read=min(3, budget))
        async with asyncio.timeout(budget):
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False) as client:
                # aiohttp 3.14.5 otherwise silently retries a disconnected GET.
                client._retry_connection = False
                async with client.get(url, allow_redirects=False,
                        headers={'User-Agent': 'hermes-mobile-access/domain-probe'}) as response:
                    status = response.status
        reason = 'normal_http_response' if 200 <= status < 400 else 'abnormal_http_response'
    except (asyncio.TimeoutError, TimeoutError):
        reason = 'timeout'
    except aiohttp.ClientSSLError:
        reason = 'tls_error'
    except aiohttp.ClientConnectorDNSError:
        reason = 'dns_error'
    except (aiohttp.ClientError, OSError):
        reason = 'connection_error'
    return observation(url, status, reason, time.monotonic() - start)


def probe_domain(url):
    url = validate_url(url)
    started = time.monotonic()
    try:
        child = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'],
            input=json.dumps({'server_url': url}).encode(), capture_output=True, timeout=WORKER_BUDGET,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result = json.loads(child.stdout)
        if (child.returncode or not isinstance(result, dict) or result.get('result') != 'domain_checked'
                or result.get('server_url') != url or result.get('occupancy') not in ('occupied', 'unoccupied')):
            raise ValueError()
        return result
    except subprocess.TimeoutExpired:
        return observation(url, None, 'timeout', time.monotonic() - started)
    except (OSError, ValueError):
        # A broken local runtime is not a completed network observation.
        raise ConfigError('domain_probe_failed', '域名检查程序未能完成，请检查本机运行环境。') from None


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    target = validate_url(json.loads(sys.stdin.buffer.read(65537))['server_url'])
    print(json.dumps(asyncio.run(observe(target)), ensure_ascii=False))
