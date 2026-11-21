"""HTTP pipeline: auth, retries with ``Retry-After``, error mapping, paging and LRO polling."""

from __future__ import annotations

import email.utils
import json
import logging
import random
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

from ._auth import TokenCredential, _TokenCache
from ._version import __version__
from .errors import (
    AuthenticationError,
    BudgetExceededError,
    ConflictError,
    FinOpsError,
    ForbiddenError,
    HttpResponseError,
    NotFoundError,
    ServiceError,
    ThrottledError,
    is_budget_exceeded,
)

logger = logging.getLogger("foundry_finops")

_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
#: Cost Management reports its throttling windows in several ``x-ms-ratelimit-*`` headers.
_RETRY_AFTER_HEADERS = (
    "retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
)
_LRO_DONE = frozenset({"succeeded", "completed"})
_LRO_FAILED = frozenset({"failed", "canceled", "cancelled"})


def _parse_retry_after(value: str) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP date."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())


@dataclass
class RetryPolicy:
    """Exponential back-off that honours ``Retry-After`` / ``x-ms-ratelimit-*`` headers."""

    total: int = 5
    backoff_factor: float = 0.8
    max_backoff: float = 60.0
    retry_non_idempotent: bool = True  # ARM POST queries (Cost Management) are safe to retry

    def sleep_for(self, attempt: int, headers: Mapping[str, str]) -> float:
        lower = {k.lower(): v for k, v in headers.items()}
        for name in ("retry-after-ms", "x-ms-retry-after-ms"):
            if name in lower:
                try:
                    return max(0.0, min(float(lower[name]) / 1000.0, self.max_backoff))
                except ValueError:
                    pass
        for name in _RETRY_AFTER_HEADERS:
            if name in lower:
                seconds = _parse_retry_after(lower[name])
                if seconds is not None:
                    return min(seconds, self.max_backoff)
        delay = self.backoff_factor * (2**attempt)
        return float(min(delay + random.uniform(0, delay / 4), self.max_backoff))


@dataclass
class Response:
    """Minimal immutable view of an HTTP response."""

    status_code: int
    headers: Mapping[str, str]
    body: Any
    text: str = ""
    url: str = ""

    def json(self) -> Any:
        return self.body


@dataclass
class Transport:
    """Shared HTTP client for every operation group.

    Args:
        credential: Any ``TokenCredential`` (``DefaultAzureCredential`` etc.).
        session: Optional ``requests.Session`` (for proxies, custom adapters or tests).
        retry: Retry policy.
        timeout: Per-request timeout in seconds.
        user_agent_suffix: Appended to the ``User-Agent`` header.
    """

    credential: TokenCredential
    session: requests.Session = field(default_factory=requests.Session)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: float = 60.0
    user_agent_suffix: str = ""
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self._tokens = _TokenCache(self.credential)

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        url: str,
        *,
        scope: str,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
        authenticate: bool = True,
    ) -> Response:
        method = method.upper()
        hdrs = {
            "Accept": "application/json",
            "User-Agent": f"foundry-finops/{__version__} {self.user_agent_suffix}".strip(),
        }
        if json_body is not None:
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)

        attempt = 0
        refreshed = False
        while True:
            if authenticate:
                hdrs["Authorization"] = self._bearer(scope, method, url)
            logger.debug("%s %s", method, url)
            try:
                raw = self.session.request(
                    method,
                    url,
                    params=params,
                    data=json.dumps(json_body) if json_body is not None else data,
                    headers=hdrs,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                if attempt < self.retry.total:
                    delay = self.retry.sleep_for(attempt, {})
                    logger.warning("Transport error %s; retrying in %.1fs", exc, delay)
                    self.sleep(delay)
                    attempt += 1
                    continue
                raise ServiceError(str(exc), status_code=0, method=method, url=url) from exc

            resp = _to_response(raw)
            if resp.status_code in expected:
                return resp

            # An access token can be revoked or rotated before its stated expiry: refresh once.
            if resp.status_code == 401 and authenticate and not refreshed:
                refreshed = True
                self._tokens.invalidate(scope)
                continue

            retryable = resp.status_code in _RETRY_STATUS and (
                method in _IDEMPOTENT or self.retry.retry_non_idempotent
            )
            # A Foundry cost-control block won't clear until the budget resets – never retry it.
            if resp.status_code == 429 and is_budget_exceeded(resp.headers, resp.text):
                retryable = False
            if retryable and attempt < self.retry.total:
                delay = self.retry.sleep_for(attempt, resp.headers)
                logger.info("HTTP %s from %s; retry %s in %.1fs", resp.status_code, url, attempt + 1, delay)
                self.sleep(delay)
                attempt += 1
                continue
            raise _to_error(resp, method, url)

    def _bearer(self, scope: str, method: str, url: str) -> str:
        try:
            return self._tokens.bearer(scope)
        except FinOpsError:
            raise
        except Exception as exc:  # azure.core ClientAuthenticationError, CredentialUnavailableError, ...
            raise AuthenticationError(
                f"Could not get an access token for {scope}: {exc}", status_code=401, method=method, url=url
            ) from exc

    # --------------------------------------------------------------- helpers
    def paged(
        self,
        url: str,
        *,
        scope: str,
        params: Mapping[str, Any] | None = None,
        item_key: str = "value",
        next_key: str = "nextLink",
    ) -> Iterator[dict[str, Any]]:
        """Iterate every item across ``nextLink`` pages."""
        next_url: str | None = url
        next_params: Mapping[str, Any] | None = params
        seen: set[str] = set()
        while next_url:
            body = self.request("GET", next_url, scope=scope, params=next_params).body
            if not isinstance(body, dict):
                return
            yield from body.get(item_key, []) or []
            next_url = body.get(next_key) or body.get("@odata.nextLink") or body.get("NextPageLink")
            if next_url in seen:  # a misbehaving service that repeats its own link must not hang us
                logger.warning("Paging loop detected at %s; stopping", next_url)
                return
            if next_url:
                seen.add(next_url)
            next_params = None  # nextLink already carries the query string

    def poll(self, resp: Response, *, scope: str, interval: float = 2.0, timeout: float = 600) -> Response:
        """Follow ``Azure-AsyncOperation`` / ``Location`` headers until an ARM LRO finishes."""
        if resp.status_code not in (201, 202):
            return resp
        lower = {k.lower(): v for k, v in resp.headers.items()}
        async_url = lower.get("azure-asyncoperation")
        poll_url = async_url or lower.get("location")
        if not poll_url:
            return resp
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait = _parse_retry_after(lower.get("retry-after", "")) if lower.get("retry-after") else None
            self.sleep(min(wait if wait is not None else interval, max(deadline - time.monotonic(), 0)))
            current = self.request("GET", poll_url, scope=scope)
            body = current.body if isinstance(current.body, dict) else {}
            status = str(body.get("status") or "").lower()
            if status in _LRO_FAILED:
                raise HttpResponseError(
                    f"Long-running operation {body.get('status')}: {_lro_error(body)}",
                    status_code=current.status_code,
                    body=current.body,
                )
            if status in _LRO_DONE:
                return current
            # A ``Location`` poll has no status document: 200/204 means finished, 202 means keep going.
            if current.status_code in (200, 204) and not status:
                return current
            lower = {k.lower(): v for k, v in current.headers.items()}
            poll_url = lower.get("azure-asyncoperation") or lower.get("location") or poll_url
        raise ServiceError("Long-running operation timed out", status_code=0)


def _lro_error(body: Mapping[str, Any]) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "")
    return ""


def _to_response(raw: requests.Response) -> Response:
    text = raw.text or ""
    body: Any = None
    if text:
        ctype = raw.headers.get("Content-Type", "")
        if "json" in ctype or text[:1] in "[{":
            try:
                body = raw.json()
            except ValueError:
                body = text
        else:
            body = text
    return Response(raw.status_code, dict(raw.headers), body, text, raw.url)


def _to_error(resp: Response, method: str, url: str) -> HttpResponseError:
    code: str | None = None
    message = resp.text[:500] or f"HTTP {resp.status_code}"
    if isinstance(resp.body, dict):
        err = resp.body.get("error") or resp.body
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or message
    kwargs: dict[str, Any] = {
        "status_code": resp.status_code,
        "code": code,
        "headers": resp.headers,
        "body": resp.body,
        "method": method,
        "url": url,
    }
    s = resp.status_code
    if s == 429 and is_budget_exceeded(resp.headers, resp.text):
        return BudgetExceededError(message, **kwargs)
    cls: type[HttpResponseError] = {
        401: AuthenticationError,
        403: ForbiddenError,
        404: NotFoundError,
        409: ConflictError,
        412: ConflictError,
        429: ThrottledError,
    }.get(s, ServiceError if s >= 500 else HttpResponseError)
    return cls(message, **kwargs)
