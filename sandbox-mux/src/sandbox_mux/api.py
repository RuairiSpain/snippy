"""HTTP API. Any number of replicas can run this; all coordination is through Redis."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import telemetry
from .auth import Authenticator, Principal, principal_dependency
from .backends import make_backend
from .config import Config
from .pool import PoolBusy, PoolManager
from .reaper import Reaper
from .service import AgentError, SessionService, TurnInProgress
from .store import Store

log = logging.getLogger(__name__)
CONV_ID = r"^[A-Za-z0-9_-]{1,64}$"


class InvokeRequest(BaseModel):
    conversation_id: str = Field("default", pattern=CONV_ID)
    prompt: str


class InvokeResponse(BaseModel):
    conversation_id: str
    reply: str
    sandbox_id: str


def create_app(cfg: Config | None = None, *, backend=None, redis_client=None,
               run_reaper: bool = True) -> FastAPI:
    cfg = cfg or Config.load()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        telemetry.setup()
        r = redis_client or redis.from_url(cfg.redis.resolved_url())
        b = backend or make_backend(cfg)
        await b.start()
        store = Store(r, cfg.redis.key_prefix, cfg.pool.name)
        pool = PoolManager(cfg, store, b)
        service = SessionService(cfg, store, b, pool)
        reaper = Reaper(cfg, store, b, pool, service)
        app.state.ctx = dict(cfg=cfg, store=store, pool=pool, service=service)
        app.state.auth = Authenticator(cfg.auth)
        task = asyncio.create_task(reaper.run()) if run_reaper else None
        try:
            yield
        finally:
            reaper.stop()
            if task:
                await task
            await b.close()
            if not redis_client:
                await r.aclose()

    app = FastAPI(title="sandbox-mux", lifespan=lifespan)

    def svc() -> SessionService:
        return app.state.ctx["service"]

    @app.post("/v1/agent/invoke", response_model=InvokeResponse)
    async def invoke(req: InvokeRequest, who: Principal = Depends(principal_dependency)):
        if not re.match(CONV_ID, req.conversation_id):
            raise HTTPException(422, "conversation_id must match " + CONV_ID)
        if not req.prompt.strip() or len(req.prompt) > cfg.session.max_prompt_chars:
            raise HTTPException(422, f"prompt must be 1..{cfg.session.max_prompt_chars} characters")
        try:
            res = await svc().run_turn(who.uid, req.conversation_id, req.prompt)
        except TurnInProgress:
            raise HTTPException(409, "A turn is already running for this user")
        except PoolBusy as e:
            return JSONResponse({"detail": "All sandboxes are busy"}, status_code=429,
                                headers={"Retry-After": str(e.retry_after)})
        except AgentError as e:
            log.error("agent error for %s: %s | %s", who.uid[:8], e, e.detail)
            raise HTTPException(502, str(e))
        return InvokeResponse(conversation_id=req.conversation_id, reply=res.reply,
                              sandbox_id=res.sandbox_id)

    @app.get("/v1/conversations/{conversation_id}/messages")
    async def messages(conversation_id: Annotated[str, Path(pattern=CONV_ID)],
                       who: Principal = Depends(principal_dependency)):
        return await app.state.ctx["store"].history(
            who.uid, conversation_id, cfg.session.max_history_messages * 5)

    @app.delete("/v1/me/session", status_code=204)
    async def end_my_session(who: Principal = Depends(principal_dependency)):
        """Release the caller's slot and delete their home directory in the sandbox."""
        try:
            await svc().end_session(who.uid)
        except TurnInProgress:
            raise HTTPException(409, "A turn is running for this user")

    @app.get("/v1/pool/status")
    async def pool_status(who: Principal = Depends(principal_dependency)):
        if who.oid not in cfg.auth.admin_oids and cfg.auth.mode != "dev":
            raise HTTPException(403)
        st = app.state.ctx["store"]
        return {"loads": await st.loads(), "states": await st.states(),
                "creating": await st.creating_count(), "waiting": await st.waiting_count(),
                "users_per_sandbox": cfg.pool.users_per_sandbox,
                "max_sandboxes": cfg.pool.max_sandboxes}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        try:
            await app.state.ctx["store"].r.ping()
        except Exception:
            raise HTTPException(503, "redis unavailable")
        return {"ok": True}

    return app
