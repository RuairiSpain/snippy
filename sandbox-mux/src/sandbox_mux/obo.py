"""Entra On-Behalf-Of token exchange.

The caller's access token (audience: this service's app registration) is exchanged for
tokens to downstream APIs, issued for that same user. The tokens are handed to the agent
process running as the user; they are never stored in Redis and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

import jwt

from .config import Config

log = logging.getLogger(__name__)

# AAD error codes meaning "the user (or their tenant admin) must consent, or re-authenticate
# with stronger auth". The client can fix these; retrying the same token cannot.
_INTERACTION = {"interaction_required", "consent_required", "invalid_grant"}


class OboError(Exception):
    def __init__(self, status: int, detail: str, claims: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.claims = status, detail, claims


def extract_assertion(headers: Any, header_name: str) -> str | None:
    """The user's token from the configured header, without any `Bearer ` prefix."""
    value = headers.get(header_name)
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


def check_same_user(assertion: str, tenant_id: str, oid: str) -> None:
    """Refuse an assertion that belongs to someone other than the authenticated caller.

    The signature is not verified here: Entra verifies it during the exchange. This only
    stops a request from authenticating as one user and delegating as another."""
    try:
        claims = jwt.decode(assertion, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise OboError(400, "OBO assertion is not a JWT") from e
    if claims.get("oid") != oid or claims.get("tid") != tenant_id:
        raise OboError(403, "OBO assertion does not belong to the authenticated user")


def check_delegated(token: str, alias: str, tenant_id: str, oid: str) -> None:
    """Refuse anything that is not the signed-in user's delegated token.

    The token comes straight from Entra over TLS, so its claims are read without verifying a
    signature. This guards against a mis-set scope (`.default` on an app permission), a
    resource that returns an app-only token, and a mix-up between users."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise OboError(502, f"The '{alias}' token is not a JWT, so delegation cannot be verified. "
                            "Set obo.verify_delegated: false only if this API issues opaque tokens.") from None
    delegated = "scp" in claims and claims.get("idtyp") != "app"
    if not delegated:
        log.error("OBO token for %s is not delegated (no scp claim or app-only)", alias)
        raise OboError(502, f"The '{alias}' token is not a delegated user token")
    if claims.get("oid") != oid or claims.get("tid") != tenant_id:
        log.error("OBO token for %s does not belong to the signed-in user", alias)
        raise OboError(502, f"The '{alias}' token does not belong to the signed-in user")


class OboBroker:
    def __init__(self, cfg: Config, app_factory: Callable[[], Any] | None = None) -> None:
        self.cfg = cfg
        self._factory = app_factory or self._msal_app
        self._app: Any = None

    def _msal_app(self):
        import msal

        o = self.cfg.obo
        credential: Any
        if o.credential == "secret":
            credential = os.environ.get("OBO_CLIENT_SECRET")
            if not credential:
                raise RuntimeError("OBO_CLIENT_SECRET is not set")
        elif o.credential == "certificate":
            with open(o.certificate_path, encoding="utf-8") as f:  # type: ignore[arg-type]
                key = f.read()
            credential = {"private_key": key, "public_certificate": key}
            if o.certificate_thumbprint:
                credential["thumbprint"] = o.certificate_thumbprint
        else:  # managed_identity: federated credential, no secret to store or rotate
            from azure.identity import ManagedIdentityCredential

            mi = ManagedIdentityCredential()
            credential = {"client_assertion": lambda: mi.get_token(
                "api://AzureADTokenExchange/.default").token}
        return msal.ConfidentialClientApplication(
            o.client_id,
            authority=f"https://login.microsoftonline.com/{self.cfg.auth.tenant_id}",
            client_credential=credential,
        )

    @property
    def app(self):
        if self._app is None:
            self._app = self._factory()
        return self._app

    def validate(self, aliases: list[str]) -> list[str]:
        o = self.cfg.obo
        unique = list(dict.fromkeys(aliases))
        unknown = [a for a in unique if a not in o.resources]
        if unknown:
            raise OboError(422, f"Unknown obo resource(s): {', '.join(unknown)}")
        if len(unique) > o.max_resources_per_turn:
            raise OboError(422, f"At most {o.max_resources_per_turn} obo resources per request")
        return unique

    async def tokens(self, assertion: str, aliases: list[str], *, tenant_id: str | None = None,
                     oid: str | None = None) -> dict[str, dict[str, Any]]:
        """Delegated tokens for the requested aliases. When `tenant_id` and `oid` are given
        (the API always gives them) each token is checked to be that user's delegated token."""
        aliases = self.validate(aliases)
        results = await asyncio.gather(*(self._one(assertion, a) for a in aliases))
        if self.cfg.obo.verify_delegated and oid and tenant_id:
            for alias, r in zip(aliases, results):
                check_delegated(r["access_token"], alias, tenant_id, oid)
        return dict(zip(aliases, results))

    async def _one(self, assertion: str, alias: str) -> dict[str, Any]:
        scopes = self.cfg.obo.resources[alias]
        # MSAL is blocking (HTTP plus a lock around its token cache).
        r = await asyncio.to_thread(self.app.acquire_token_on_behalf_of, assertion, scopes)
        if "access_token" in r:
            return {"access_token": r["access_token"], "token_type": r.get("token_type", "Bearer"),
                    "expires_in": int(r.get("expires_in", 0)), "scopes": scopes}
        err = r.get("error", "unknown_error")
        # Log the code only. Descriptions can echo request details.
        log.warning("OBO exchange for %s failed: %s", alias, err)
        if err in _INTERACTION:
            raise OboError(403, f"Consent or step-up authentication required for '{alias}' ({err})",
                           claims=r.get("claims"))
        raise OboError(502, f"Token exchange for '{alias}' failed ({err})")
