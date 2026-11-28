"""Delegated access while the user is away (triggers, option C).

The user signs in once through a consent flow the service starts. MSAL keeps the resulting
refresh token in a per-user token cache, which is encrypted and stored in Redis. Later the
service asks MSAL for access tokens silently, so an unattended run can act as that user.

This keeps a long-lived credential for the user. Everything here is meant to limit that:
the feature is off unless a developer enables it, a consent names one resource alias, it
expires after `max_lifetime_days`, the user can revoke it, the encryption key lives outside
Redis, and nothing is ever logged or returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .auth import uid_for
from .config import Config
from .obo import check_delegated, make_msal_app
from .store import Store, now_ms

log = logging.getLogger(__name__)
FLOW_TTL_S = 600


class DelegationError(Exception):
    def __init__(self, status: int, detail: str, consent_needed: bool = False) -> None:
        super().__init__(detail)
        self.status, self.detail, self.consent_needed = status, detail, consent_needed


class TokenVault:
    """Authenticated encryption for what is stored in Redis. Several keys, separated by commas,
    allow rotation: the first encrypts, all of them decrypt."""

    def __init__(self, keys: str) -> None:
        try:
            self._f = MultiFernet([Fernet(k.strip()) for k in keys.split(",") if k.strip()])
        except (ValueError, TypeError) as e:
            raise ValueError("the trigger token key must be one or more Fernet keys "
                             "(python -c 'from cryptography.fernet import Fernet; "
                             "print(Fernet.generate_key().decode())')") from e

    @classmethod
    def from_env(cls, name: str, environ: dict[str, str] | None = None) -> "TokenVault":
        value = (os.environ if environ is None else environ).get(name)
        if not value:
            raise ValueError(f"set {name} to a Fernet key to enable trigger delegation")
        return cls(value)

    def seal(self, data: dict) -> str:
        return self._f.encrypt(json.dumps(data).encode()).decode()

    def open(self, blob: str | bytes) -> dict:
        try:
            return json.loads(self._f.decrypt(blob if isinstance(blob, bytes) else blob.encode()))
        except InvalidToken:
            raise DelegationError(409, "Stored consent cannot be read; consent again",
                                  consent_needed=True) from None


class DelegationService:
    def __init__(self, cfg: Config, store: Store, vault: TokenVault,
                 app_factory: Callable[[Any], Any] | None = None) -> None:
        self.cfg, self.store, self.vault = cfg, store, vault
        self._factory = app_factory or (lambda cache: make_msal_app(cfg, cache))
        self.d = cfg.triggers.delegation

    def _k(self, uid: str) -> str:
        return self.store.k(f"deleg:{uid}")

    async def _load(self, uid: str) -> dict | None:
        raw = await self.store.r.get(self._k(uid))
        if not raw:
            return None
        rec = self.vault.open(raw)
        if rec["expires_ms"] <= now_ms():
            await self.store.r.delete(self._k(uid))
            return None
        return rec

    async def _save(self, uid: str, rec: dict) -> None:
        ttl = max(1, (rec["expires_ms"] - now_ms()) // 1000)
        await self.store.r.set(self._k(uid), self.vault.seal(rec), ex=ttl)

    # ---- consent ---------------------------------------------------------------------------

    async def start(self, uid: str, upn: str | None, alias: str) -> dict:
        scopes = self.cfg.obo.resources.get(alias)
        if not scopes:
            raise DelegationError(422, f"Unknown resource '{alias}'")
        app = await asyncio.to_thread(self._factory, None)
        flow = await asyncio.to_thread(
            app.initiate_auth_code_flow, scopes, redirect_uri=self.d.redirect_uri, login_hint=upn)
        if "auth_uri" not in flow:
            log.error("could not start consent for %s: %s", alias, flow.get("error"))
            raise DelegationError(502, "Could not start consent")
        await self.store.r.set(self.store.k(f"deleg:flow:{flow['state']}"),
                               self.vault.seal({"flow": flow, "uid": uid, "alias": alias}),
                               ex=FLOW_TTL_S)
        return {"auth_url": flow["auth_uri"], "expires_in": FLOW_TTL_S}

    async def complete(self, params: dict[str, str]) -> str:
        """Finish the consent redirect. Returns the uid that consented."""
        state = params.get("state", "")
        raw = await self.store.r.getdel(self.store.k(f"deleg:flow:{state}")) if state else None
        if not raw:
            raise DelegationError(400, "This consent request has expired or was already used")
        data = self.vault.open(raw)                       # one use: it was just deleted
        if "error" in params or "code" not in params:
            raise DelegationError(400, "Consent was not granted")
        uid, alias = data["uid"], data["alias"]

        cache = _new_cache()
        existing = await self._load(uid)
        if existing:
            cache.deserialize(existing["cache"])
        app = await asyncio.to_thread(self._factory, cache)
        result = await asyncio.to_thread(app.acquire_token_by_auth_code_flow, data["flow"], params)
        if "access_token" not in result:
            log.warning("consent exchange failed: %s", result.get("error"))
            raise DelegationError(400, "Consent could not be completed")
        claims = result.get("id_token_claims") or {}
        if claims.get("tid") != self.cfg.auth.tenant_id or uid_for(
                claims.get("tid", ""), claims.get("oid", "")) != uid:
            # Someone else signed in on the consent page: never bind their token to this user.
            raise DelegationError(403, "Consent was granted by a different account")
        accounts = await asyncio.to_thread(app.get_accounts)
        home = next((a["home_account_id"] for a in accounts
                     if str(a.get("home_account_id", "")).startswith(claims["oid"])), None)
        now = now_ms()
        rec = {"cache": cache.serialize(), "home_account_id": home,
               "granted": {**(existing or {}).get("granted", {}), alias: now},
               "created_ms": (existing or {}).get("created_ms", now),
               "expires_ms": now + self.d.max_lifetime_days * 86_400_000}
        await self._save(uid, rec)
        log.info("delegated consent stored for user %s resource %s", uid[:8], alias)
        return uid

    async def status(self, uid: str) -> dict | None:
        rec = await self._load(uid)
        if not rec:
            return None
        return {"resources": sorted(rec["granted"]), "created_at": _iso(rec["created_ms"]),
                "expires_at": _iso(rec["expires_ms"]), "expires_ms": rec["expires_ms"]}

    async def revoke(self, uid: str) -> bool:
        return bool(await self.store.r.delete(self._k(uid)))

    # ---- use -------------------------------------------------------------------------------------

    async def tokens_for(self, uid: str, tenant_id: str, oid: str, aliases: list[str]) -> dict:
        """Access tokens for `aliases`, as the user, without the user present."""
        barred = [a for a in aliases if a not in self.cfg.triggers.delegated_aliases]
        if barred:
            raise DelegationError(403, f"Not allowed for unattended runs: {', '.join(barred)}")
        lock = f"lock:deleg:{uid}"
        deadline = time.monotonic() + 15
        while not (token := await self.store.acquire_lock(lock, 30)):
            if time.monotonic() > deadline:
                raise DelegationError(503, "Delegation is busy; try again")
            await asyncio.sleep(0.1)
        try:
            rec = await self._load(uid)
            if not rec:
                raise DelegationError(409, "Consent is missing or has expired", consent_needed=True)
            missing = [a for a in aliases if a not in rec["granted"]]
            if missing:
                raise DelegationError(409, f"No consent for: {', '.join(missing)}", consent_needed=True)
            cache = _new_cache()
            cache.deserialize(rec["cache"])
            app = await asyncio.to_thread(self._factory, cache)
            accounts = await asyncio.to_thread(app.get_accounts)
            account = next((a for a in accounts if a.get("home_account_id") == rec["home_account_id"]),
                           accounts[0] if accounts else None)
            if account is None:
                raise DelegationError(409, "Stored consent has no account; consent again",
                                      consent_needed=True)
            out: dict[str, dict] = {}
            for alias in aliases:
                scopes = self.cfg.obo.resources[alias]
                r = await asyncio.to_thread(app.acquire_token_silent, scopes, account)
                if not r or "access_token" not in r:
                    log.warning("silent token for %s failed: %s", alias, (r or {}).get("error"))
                    raise DelegationError(409, f"Consent for '{alias}' is no longer valid",
                                          consent_needed=True)
                if self.cfg.obo.verify_delegated:
                    try:
                        check_delegated(r["access_token"], alias, tenant_id, oid)
                    except Exception as e:
                        raise DelegationError(502, getattr(e, "detail", "Token check failed")) from None
                out[alias] = {"access_token": r["access_token"], "token_type": r.get("token_type", "Bearer"),
                              "expires_in": int(r.get("expires_in", 0)), "scopes": scopes}
            if cache.has_state_changed:                 # the refresh token may have rotated
                await self._save(uid, {**rec, "cache": cache.serialize()})
            return out
        finally:
            await self.store.release_lock(lock, token)


def _new_cache():
    import msal
    return msal.SerializableTokenCache()


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))
