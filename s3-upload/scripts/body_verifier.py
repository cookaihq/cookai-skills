from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError

from delivery_reference import URL_SCOPES
from delivery_schema import VERIFICATION_CHANNELS
from s3 import TransportError, open_body_stream


# One mebibyte per read. The cap is on the request, not on the object: an
# object of any size is read to its end, a mebibyte at a time.
CHUNK = 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class VerificationError(ValueError):
    """A refusal to call something verified, carrying why in the reason.

    Only two reasons ever come out of this module, and they mean different
    things to the caller: content_mismatch says the bytes at the far end are
    provably not the planned bytes, verification_incomplete says the far end
    never proved anything either way. Collapsing them would turn "we could not
    look" into "we looked and it was wrong".
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Verification:
    channel: str
    size: int
    sha256: str
    url_scope: str
    verified_at: str


def _timestamp(moment: datetime) -> str:
    # Same shape as operations._timestamp (scripts/operations.py:55-56).
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response_headers(stream: Any) -> Dict[str, str]:
    """Read the response headers into a lowercased plain dict.

    A real urllib response carries an email.message.Message here and a test
    double carries a dict; both expose items(), and folding through it is what
    lets one comparison serve both without the module having to know which it
    is holding.
    """
    raw = getattr(stream, "headers", None)
    items = getattr(raw, "items", None)
    if items is None:
        return {}
    return {str(key).lower(): str(value) for key, value in items()}


def verify_body(*, open_stream: Callable[..., Any] = open_body_stream,
                method: str = "GET", url: str, headers: Dict[str, str],
                channel: str, url_scope: str, expected_size: int,
                expected_sha256: str, now: datetime,
                timeout: int = 30) -> Verification:
    """Read a response body to its end and prove it is the planned content.

    The body is never returned and never held: it is folded into a SHA-256 a
    chunk at a time and dropped. What comes back is the record of the read.
    """
    # Everything below is knowable without asking the network anything, so it
    # is settled before a request exists. A call naming a channel nobody
    # registered has already failed; issuing the GET first would spend a real
    # request to discover that, and on the anonymous channel would publish an
    # access attempt to find out. The tests observe this through a counter on
    # the injected opener, which this module neither sets nor reports.
    if channel not in VERIFICATION_CHANNELS:
        raise VerificationError("unregistered verification channel",
                                reason="verification_incomplete")
    if url_scope not in URL_SCOPES:
        raise VerificationError("unregistered url_scope",
                                reason="verification_incomplete")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) \
            or expected_size < 0:
        raise VerificationError("expected_size must be a non-negative integer",
                                reason="verification_incomplete")
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise VerificationError("expected_sha256 must be lowercase hex sha256",
                                reason="verification_incomplete")
    if not isinstance(now, datetime):
        raise VerificationError("now must be a datetime",
                                reason="verification_incomplete")

    digest = hashlib.sha256()
    total = 0
    # Request construction, the open, and the reads sit in one try, and the
    # except names every family the three of them can raise. HTTPException is
    # in that list for a reason that was checked rather than assumed:
    # http.client.IncompleteRead -- a chunked response that stops early, which
    # is exactly the failure this verifier exists to catch -- descends from
    # HTTPException and from neither OSError nor ValueError, so a list naming
    # only the urllib families lets it escape as a foreign type.
    #
    # A malformed `headers` argument raises TypeError out of dict() and is
    # deliberately *not* in that list: that is a caller passing the wrong
    # thing, and reporting it as verification_incomplete would blame the
    # remote object for a bug in the process holding it.
    try:
        with open_stream(method, url, dict(headers), timeout=timeout) as stream:
            status = getattr(stream, "status", None)
            if status != 200:
                raise VerificationError("response is not a complete body",
                                        reason="verification_incomplete")
            seen = _response_headers(stream)
            # A 206 is refused by the status check above; this catches the
            # server that answers 200 and still delivers a range. Either way a
            # partial GET is not adoption evidence -- it proves something
            # about a slice of an object, and the claim being made is about
            # the object.
            if "content-range" in seen:
                raise VerificationError("range response is not a full body",
                                        reason="verification_incomplete")
            declared = seen.get("content-length")
            if declared is not None and declared != str(expected_size):
                raise VerificationError("declared length does not match the plan",
                                        reason="content_mismatch")
            while total <= expected_size:
                # One byte past the plan is enough to know the body is too
                # long, so one byte past the plan is all this asks for. An
                # object that is not the planned object is not read to its
                # end just to find out how much it is not.
                chunk = stream.read(min(CHUNK, expected_size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise VerificationError("body is longer than the plan",
                                            reason="content_mismatch")
                digest.update(chunk)
    except VerificationError:
        # Re-raised before the catch-all below, which would otherwise swallow
        # it: VerificationError is a ValueError subclass, so every
        # content_mismatch raised inside this try would come back out as
        # verification_incomplete. Same shape as the three domain errors that
        # are ValueError subclasses elsewhere in this package.
        raise
    except (HTTPError, URLError, TransportError, HTTPException, OSError,
            ValueError) as exc:
        raise VerificationError("the body could not be read in full",
                                reason="verification_incomplete") from exc
    if total != expected_size:
        raise VerificationError("body is shorter than the plan",
                                reason="verification_incomplete")
    measured = digest.hexdigest()
    if not hmac.compare_digest(measured, expected_sha256):
        raise VerificationError("body does not match the planned content",
                                reason="content_mismatch")
    # The measured digest, not the expected one echoed back. They are equal on
    # this line, but the record travels on into build_verification, which
    # cross-checks it against the planned content -- and a record that carries
    # the plan's own value would make that check compare the plan to itself.
    return Verification(channel=channel, size=total, sha256=measured,
                        url_scope=url_scope, verified_at=_timestamp(now))
