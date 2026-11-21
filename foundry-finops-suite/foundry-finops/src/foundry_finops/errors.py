"""Exception hierarchy and helpers for Foundry cost-control blocks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BUDGET_CAUSE_HEADER = "x-ms-budget-cause"
BUDGET_RESET_HEADER = "x-ms-budget-window-reset"
#: Foundry documents both names for the ID of the cost control that blocked a request.
BUDGET_POLICY_HEADERS = ("x-ms-applicable-policy-id", "x-ms-applied-cost-control-id")
BUDGET_EXCEEDED_MESSAGE = "Budget exceeded for cost control"


class FinOpsError(Exception):
    """Base class for every error raised by this package."""


class HttpResponseError(FinOpsError):
    """A REST call returned an unsuccessful status code."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.headers = dict(headers or {})
        self.body = body
        self.method = method
        self.url = url

    def __str__(self) -> str:
        where = f" ({self.method} {self.url})" if self.method else ""
        code = f" [{self.code}]" if self.code else ""
        return f"HTTP {self.status_code}{code}: {self.message}{where}"


class AuthenticationError(HttpResponseError):
    """401 – the token was missing, expired or for the wrong audience."""


class ForbiddenError(HttpResponseError):
    """403 – the caller lacks an RBAC role (or an APIM token quota was exceeded)."""


class NotFoundError(HttpResponseError):
    """404 – the resource does not exist."""


class ConflictError(HttpResponseError):
    """409/412 – concurrent write, ETag mismatch or If-None-Match create conflict."""


class ThrottledError(HttpResponseError):
    """429 – rate limited after retries were exhausted."""

    @property
    def retry_after(self) -> float | None:
        value = self.headers.get("Retry-After") or self.headers.get("retry-after")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None


class BudgetExceededError(ThrottledError):
    """429 caused by a Foundry cost control ``Block usage`` action. Retrying won't help until reset."""

    @property
    def reset_at(self) -> str | None:
        return _get_header(self.headers, BUDGET_RESET_HEADER)

    @property
    def cost_control_id(self) -> str | None:
        for name in BUDGET_POLICY_HEADERS:
            value = _get_header(self.headers, name)
            if value:
                return value
        return None


class ServiceError(HttpResponseError):
    """5xx after retries were exhausted."""


class DependencyMissingError(FinOpsError, ImportError):
    """An optional dependency (pandas, OpenTelemetry, …) is required for this feature."""


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def is_budget_exceeded(headers: Mapping[str, str] | None, body: Any = None) -> bool:
    """Return ``True`` if a response was blocked by a Foundry cost control.

    Works with ``requests``/``httpx`` headers, ``openai.APIStatusError.response.headers``
    or a plain dict. Streaming responses return HTTP 200 and carry the block as an error
    event; pass that event's text/dict as ``body`` to detect it.
    """
    if headers and (_get_header(headers, BUDGET_CAUSE_HEADER) or "").lower() == "budget_exceeded":
        return True
    if body is not None:
        return BUDGET_EXCEEDED_MESSAGE.lower() in str(body).lower()
    return False


def raise_for_budget(exc: BaseException) -> None:
    """Re-raise an OpenAI/HTTP exception as :class:`BudgetExceededError` when a cost control blocked it.

    >>> try:
    ...     client.responses.create(model="gpt-5.4-mini", input="hi")
    ... except openai.RateLimitError as e:
    ...     raise_for_budget(e)   # raises BudgetExceededError, else returns
    ...     raise
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    if is_budget_exceeded(headers, getattr(exc, "message", None) or str(exc)):
        raise BudgetExceededError(
            BUDGET_EXCEEDED_MESSAGE,
            status_code=int(getattr(response, "status_code", 429) or 429),
            code="budget_exceeded",
            headers=dict(headers),
        ) from exc
