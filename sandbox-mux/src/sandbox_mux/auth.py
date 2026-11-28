"""Caller identity. Entra ID access tokens are verified; dev mode is for local use only."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, Request

from .config import AuthConfig


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    oid: str

    @property
    def uid(self) -> str:
        """Stable pseudonymous key. Never derived from anything the caller can choose
        independently of the verified token."""
        return hashlib.sha256(f"{self.tenant_id}:{self.oid}".encode()).hexdigest()[:32]


class Authenticator:
    def __init__(self, cfg: AuthConfig) -> None:
        self.cfg = cfg
        self._jwks = None
        if cfg.mode == "entra":
            self._jwks = jwt.PyJWKClient(
                f"https://login.microsoftonline.com/{cfg.tenant_id}/discovery/v2.0/keys",
                cache_keys=True)

    async def authenticate(self, authorization: str | None, dev_user: str | None) -> Principal:
        if self.cfg.mode == "dev":
            if not dev_user:
                raise HTTPException(401, "X-Dev-User header required in dev auth mode")
            return Principal("dev", dev_user)
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Bearer token required")
        token = authorization[7:].strip()
        try:
            # PyJWKClient does blocking HTTP on a cache miss, so keep it off the event loop.
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)  # type: ignore[union-attr]
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.cfg.audience,
                issuer=[f"https://login.microsoftonline.com/{self.cfg.tenant_id}/v2.0",
                        f"https://sts.windows.net/{self.cfg.tenant_id}/"],
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            raise HTTPException(401, f"Invalid token: {type(e).__name__}") from e
        oid, tid = claims.get("oid"), claims.get("tid")
        if not oid or tid != self.cfg.tenant_id:
            raise HTTPException(401, "Token has no oid or belongs to another tenant")
        return Principal(tid, oid)


async def principal_dependency(request: Request,
                               authorization: str | None = Header(None),
                               x_dev_user: str | None = Header(None)) -> Principal:
    return await request.app.state.auth.authenticate(authorization, x_dev_user)
