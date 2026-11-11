"""Bounded idempotent PDF GETs; signed query strings never enter task history."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

DOWNLOAD_BUDGET = 180
DOWNLOAD_LIMIT = 256 * 1024 * 1024


class DownloadDeferred(ValueError):
    def __init__(self, retry_at: str):
        self.retry_at = retry_at
        super().__init__("Server cooling period exceeds this download's budget; retry at " + retry_at)


def redacted(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if parts.port:
        host += ":" + str(parts.port)
    return urlunsplit((parts.scheme, host, parts.path, "redacted" if parts.query else "", ""))


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0, float(int(value)))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return float(2 ** attempt)


async def retrieve(url: str, target: Path) -> list[dict]:
    deadline = time.monotonic() + DOWNLOAD_BUDGET
    attempts: list[dict[str, Any]] = []
    async with asyncio.timeout(DOWNLOAD_BUDGET):
        async with httpx.AsyncClient(follow_redirects=True, timeout=30, max_redirects=5) as client:
            for attempt in range(3):
                wait = float(2 ** attempt)
                try:
                    async with client.stream("GET", url) as response:
                        status = response.status_code
                        if status == 429 or status in {408, 500, 502, 503, 504}:
                            wait = retry_delay(response.headers.get("retry-after"), attempt)
                            attempts.append({"attempt": attempt + 1, "status": status, "wait": wait})
                        elif status != 200:
                            raise ValueError(f"PDF download refused with HTTP {status}")
                        else:
                            total = 0
                            with target.open("wb") as stream:
                                async for chunk in response.aiter_bytes(1024 * 1024):
                                    total += len(chunk)
                                    if total > DOWNLOAD_LIMIT:
                                        raise ValueError("PDF exceeds the 256 MiB download limit")
                                    stream.write(chunk)
                            attempts.append({"attempt": attempt + 1, "status": status, "bytes": total})
                            return attempts
                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc).upper():
                        raise ValueError("PDF download certificate verification failed") from None
                    attempts.append({"attempt": attempt + 1, "error": type(exc).__name__, "wait": wait})
                if wait >= deadline - time.monotonic():
                    raise DownloadDeferred((datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat())
                if attempt < 2:
                    await asyncio.sleep(wait)
    raise ValueError("PDF download failed after 3 attempts")


def fetch(url: str, target: Path) -> list[dict]:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        return asyncio.run(retrieve(url, target))
    except TimeoutError:
        raise ValueError("PDF download exceeded the 180 second total budget") from None
    except httpx.HTTPError as exc:
        raise ValueError("PDF download failed: " + type(exc).__name__) from None
