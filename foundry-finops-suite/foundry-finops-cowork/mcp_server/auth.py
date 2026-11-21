"""Microsoft Entra ID token validation and on-behalf-of (OBO) exchange.

Flow: Cowork signs the user in through the OAuth client registration in Teams Developer Portal and
sends ``Authorization: Bearer <token>`` whose audience is this server's Entra app. The server
validates the token, then exchanges it (OBO) for Azure Resource Manager and Log Analytics tokens,
so every REST call runs with **the signed-in user's own Azure RBAC** – users only see subscriptions,
budgets and cost data they already have access to.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import jwt  # PyJWT[crypto]


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class AuthConfig:
    tenant_id: str
    client_id: str
    #: Scope the token must carry (Expose an API → scope name).
    required_scope: str = "access_as_user"
    #: Optional allow-list of tenant IDs (multi-tenant apps). Empty = the configured tenant only.
    allowed_tenants: tuple[str, ...] = ()
    #: Secret for OBO. Prefer a managed identity federated credential (set OBO_USE_MANAGED_IDENTITY=1).
    client_secret: str | None = field(default=None, repr=False)
    use_managed_identity: bool = False
    public_base_url: str = ""

    @classmethod
    def from_env(cls) -> AuthConfig:
        e = os.environ.get
        tenants = tuple(t.strip() for t in (e("MCP_ALLOWED_TENANTS") or "").split(",") if t.strip())
        return cls(
            tenant_id=e("AZURE_TENANT_ID", ""),
            client_id=e("MCP_CLIENT_ID", ""),
            required_scope=e("MCP_REQUIRED_SCOPE", "access_as_user"),
            allowed_tenants=tenants,
            client_secret=e("MCP_CLIENT_SECRET"),
            use_managed_identity=(e("OBO_USE_MANAGED_IDENTITY", "") == "1"),
            public_base_url=e("MCP_PUBLIC_BASE_URL", ""),
        )

    @property
    def audiences(self) -> list[str]:
        return [self.client_id, f"api://{self.client_id}"]

    @property
    def issuers(self) -> list[str]:
        tenants = self.allowed_tenants or (self.tenant_id,)
        out = []
        for t in tenants:
            out += [f"https://login.microsoftonline.com/{t}/v2.0", f"https://sts.windows.net/{t}/"]
        return out


@dataclass
class User:
    oid: str
    tid: str
    upn: str
    name: str
    scopes: tuple[str, ...]
    token: str = field(repr=False)
    #: Token expiry (epoch seconds); the cached on-behalf-of credential is dropped by then.
    expires_at: float = 0.0


class TokenValidator:
    """Validates Entra access tokens against the tenant's signing keys (cached)."""

    def __init__(self, config: AuthConfig, jwks_client: Any = None) -> None:
        self.config = config
        self._jwks = jwks_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{config.tenant_id}/discovery/v2.0/keys", cache_keys=True, lifespan=3600
        )

    def validate(self, authorization: str | None) -> User:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("Missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.config.audiences,
                options={"require": ["exp", "iat", "aud", "iss", "tid"]}, leeway=60,
            )
        except jwt.PyJWKClientConnectionError as exc:  # Entra's key endpoint is unreachable: not the caller's fault
            raise AuthError("Cannot fetch token signing keys; try again shortly", status=503) from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"Invalid token: {exc}") from exc
        if claims.get("iss") not in self.config.issuers:
            raise AuthError("Token issuer not allowed")
        scopes = tuple((claims.get("scp") or "").split())
        if self.config.required_scope and self.config.required_scope not in scopes:
            raise AuthError(f"Token is missing the '{self.config.required_scope}' scope", status=403)
        return User(
            oid=claims.get("oid", ""), tid=claims["tid"],
            upn=claims.get("preferred_username") or claims.get("upn") or "", name=claims.get("name", ""),
            scopes=scopes, token=token, expires_at=float(claims.get("exp") or 0),
        )


class OboCredential:
    """A ``TokenCredential`` that exchanges the user's token for downstream tokens (cached per scope)."""

    def __init__(self, config: AuthConfig, user_assertion: str) -> None:
        from azure.identity import OnBehalfOfCredential

        kwargs: dict[str, Any] = {"user_assertion": user_assertion}
        if config.use_managed_identity:
            from azure.identity import ManagedIdentityCredential

            mi = ManagedIdentityCredential(client_id=os.environ.get("OBO_MANAGED_IDENTITY_CLIENT_ID"))
            kwargs["client_assertion_func"] = lambda: mi.get_token("api://AzureADTokenExchange/.default").token
        elif config.client_secret:
            kwargs["client_secret"] = config.client_secret
        else:
            raise AuthError("Server isn't configured for on-behalf-of (no secret or managed identity)", 500)
        self._inner = OnBehalfOfCredential(config.tenant_id, config.client_id, **kwargs)
        self._lock = threading.Lock()

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        with self._lock:
            return self._inner.get_token(*scopes, **kwargs)


class CredentialCache:
    """Keeps one OBO credential per user token for its lifetime (tokens are short-lived)."""

    def __init__(self, config: AuthConfig, factory: Any = None, max_entries: int = 500) -> None:
        self.config = config
        self.factory = factory or (lambda token: OboCredential(config, token))
        self._items: dict[str, tuple[float, Any]] = {}
        self._max = max_entries
        self._lock = threading.Lock()

    def for_user(self, user: User) -> Any:
        key = f"{user.oid}:{hashlib.sha256(user.token.encode()).hexdigest()}"
        now = time.time()
        with self._lock:
            hit = self._items.get(key)
            if hit and hit[0] > now:
                return hit[1]
            if len(self._items) >= self._max:
                self._items = {k: v for k, v in self._items.items() if v[0] > now}
                while len(self._items) >= self._max:  # all still valid: drop the entry closest to expiry
                    self._items.pop(min(self._items, key=lambda k: self._items[k][0]))
            cred = self.factory(user.token)
            self._items[key] = (min(now + 3000, user.expires_at or now + 3000), cred)
            return cred
