"""Authentication helpers.

The SDK accepts any object that implements the ``azure.core.credentials.TokenCredential``
protocol (``get_token(*scopes) -> AccessToken``), so every ``azure-identity`` credential works.
"""

from __future__ import annotations

import threading
import time
from typing import Any, NamedTuple, Protocol, runtime_checkable


class AccessToken(NamedTuple):
    token: str
    expires_on: int


@runtime_checkable
class TokenCredential(Protocol):
    def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class StaticTokenCredential:
    """A credential that returns a fixed bearer token. Useful for tests and short scripts."""

    def __init__(self, token: str, expires_in: int = 3600) -> None:
        self._token = AccessToken(token, int(time.time()) + expires_in)

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return self._token


def default_credential(**kwargs: Any) -> TokenCredential:
    """Return ``azure.identity.DefaultAzureCredential`` (imported lazily)."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ImportError("azure-identity is required: pip install azure-identity") from exc
    return DefaultAzureCredential(**kwargs)


class _TokenCache:
    """Caches one token per scope and refreshes it five minutes before expiry."""

    _REFRESH_MARGIN = 300

    def __init__(self, credential: TokenCredential) -> None:
        self._credential = credential
        self._tokens: dict[str, Any] = {}
        self._lock = threading.Lock()

    def bearer(self, scope: str) -> str:
        """Return ``Bearer <token>``. Thread-safe: one client is shared by every server thread."""
        with self._lock:
            tok = self._tokens.get(scope)
            if tok is None or tok.expires_on - self._REFRESH_MARGIN <= time.time():
                tok = self._credential.get_token(scope)
                self._tokens[scope] = tok
            return f"Bearer {tok.token}"

    def invalidate(self, scope: str | None = None) -> None:
        """Forget cached tokens (after a 401) so the next call fetches a fresh one."""
        with self._lock:
            if scope is None:
                self._tokens.clear()
            else:
                self._tokens.pop(scope, None)
