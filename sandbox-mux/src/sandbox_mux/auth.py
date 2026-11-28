"""Caller identity. Entra ID access tokens are verified; dev mode is for local use only."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

import jwt
from fastapi import Header, HTTPException, Request

from .config import AuthConfig
from .obo import OboError, check_same_user, extract_assertion


def uid_for(tenant_id: str, oid: str) -> str:
    """Stable pseudonymous user key for a (tenant, object id)."""
    return hashlib.sha256(f"{tenant_id}:{oid}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    oid: str
    # The caller's token, kept only for the On-Behalf-Of exchange. Excluded from repr and
    # equality so it cannot leak through logging.
    assertion: str | None = field(default=None, repr=False, compare=False)
    upn: str | None = field(default=None, compare=False)      # sign-in name, for display only

    @property
    def uid(self) -> str:
        """Stable pseudonymous key. Never derived from anything the caller can choose
        independently of the verified token."""
        return uid_for(self.tenant_id, self.oid)


class Authenticator:
    def __init__(self, cfg: AuthConfig) -> None:
        self.cfg = cfg
        self._jwks = None
        if cfg.mode == "entra":
            self._jwks = jwt.PyJWKClient(
                f"https://login.microsoftonline.com/{cfg.tenant_id}/discovery/v2.0/keys",
                cache_keys=True)

    async def _verify(self, token: str, audience: str) -> dict:
        try:
            # PyJWKClient does blocking HTTP on a cache miss, so keep it off the event loop.
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)  # type: ignore[union-attr]
            return jwt.decode(
                token, key.key, algorithms=["RS256"], audience=audience,
                issuer=[f"https://login.microsoftonline.com/{self.cfg.tenant_id}/v2.0",
                        f"https://sts.windows.net/{self.cfg.tenant_id}/"],
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            raise HTTPException(401, f"Invalid token: {type(e).__name__}") from e

    async def authenticate(self, authorization: str | None, dev_user: str | None,
                           assertion: str | None = None) -> Principal:
        if self.cfg.mode == "dev":
            if not dev_user:
                raise HTTPException(401, "X-Dev-User header required in dev auth mode")
            return Principal("dev", dev_user)
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Bearer token required")
        token = authorization[7:].strip()
        claims = await self._verify(token, self.cfg.audience)
        oid, tid = claims.get("oid"), claims.get("tid")
        if not oid or tid != self.cfg.tenant_id:
            raise HTTPException(401, "Token has no oid or belongs to another tenant")
        if "scp" not in claims or claims.get("idtyp") == "app":
            # Only delegated user tokens are users. A managed identity's token (such as the
            # connector namespace calling the trigger callback) is not a user.
            raise HTTPException(401, "A delegated user token is required")
        if assertion and assertion != token:
            # Delegation token supplied in a different header: it must be the same user's.
            try:
                check_same_user(assertion, tid, oid)
            except OboError as e:
                raise HTTPException(e.status, e.detail) from e
        return Principal(tid, oid, assertion=assertion or token,
                         upn=claims.get("preferred_username") or claims.get("upn"))

    async def authenticate_service(self, authorization: str | None, audience: str,
                                   allowed_oids: list[str]) -> dict:
        """The connector namespace's managed identity calling a trigger callback. It must be
        an app token (no `scp`) for `audience` from one of the allowed object IDs."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Bearer token required")
        claims = await self._verify(authorization[7:].strip(), audience)
        if claims.get("tid") != self.cfg.tenant_id or claims.get("oid") not in allowed_oids \
                or "scp" in claims:
            raise HTTPException(403, "Caller is not an allowed identity")
        return claims


async def principal_dependency(request: Request,
                               authorization: str | None = Header(None),
                               x_dev_user: str | None = Header(None)) -> Principal:
    obo = request.app.state.ctx["cfg"].obo
    assertion = extract_assertion(request.headers, obo.assertion_header) if obo.enabled else None
    return await request.app.state.auth.authenticate(authorization, x_dev_user, assertion)
