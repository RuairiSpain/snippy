"""HTTP API. Any number of replicas can run this; all coordination is through Redis."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import telemetry
from .auth import Authenticator, Principal, principal_dependency
from .backends import make_backend
from .blobs import make_blob_store
from .files import FILE_ID, FileError, FileService
from .config import Config
from .pool import PoolBusy, PoolManager
from .obo import OboBroker, OboError
from .reaper import Reaper
from .service import AgentError, SessionService, TurnInProgress
from .store import Store

log = logging.getLogger(__name__)
CONV_ID = r"^[A-Za-z0-9_-]{1,64}$"


class InvokeRequest(BaseModel):
    conversation_id: str = Field("default", pattern=CONV_ID)
    prompt: str
    # Aliases from `obo.resources` whose delegated tokens the agent needs this turn.
    obo_resources: list[str] = Field(default_factory=list, max_length=10)
    # Ids from POST /v1/files. They appear in the agent's $INPUT_DIR for this turn only.
    inputs: list[Annotated[str, Field(pattern=FILE_ID)]] = Field(default_factory=list, max_length=50)


class InvokeResponse(BaseModel):
    conversation_id: str
    reply: str
    sandbox_id: str
    outputs: list[dict] = []
    skipped_outputs: list[dict] = []


def create_app(cfg: Config | None = None, *, backend=None, redis_client=None,
               run_reaper: bool = True, obo_broker: OboBroker | None = None,
               blob_store=None) -> FastAPI:
    cfg = cfg or Config.load()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        telemetry.setup()
        for what, ratio in cfg.overcommit().items():
            if ratio > 1:
                log.warning("per-user %s limits add up to %.1fx the sandbox size at %d users: a "
                            "full sandbox can still run out. Lower limits.%s or users_per_sandbox.",
                            what, ratio, cfg.pool.users_per_sandbox, what)
        r = redis_client or redis.from_url(cfg.redis.resolved_url())
        b = backend or make_backend(cfg)
        await b.start()
        store = Store(r, cfg.redis.key_prefix, cfg.pool.name)
        pool = PoolManager(cfg, store, b)
        blobs = None
        files = None
        if cfg.files.enabled:
            blobs = blob_store or make_blob_store(cfg.files)
            files = FileService(cfg, store, blobs)
        service = SessionService(cfg, store, b, pool, files)
        reaper = Reaper(cfg, store, b, pool, service, files=files)
        app.state.ctx = dict(cfg=cfg, store=store, pool=pool, service=service, files=files,
                             obo=obo_broker or (OboBroker(cfg) if cfg.obo.enabled else None))
        app.state.auth = Authenticator(cfg.auth)
        task = asyncio.create_task(reaper.run()) if run_reaper else None
        try:
            yield
        finally:
            reaper.stop()
            if task:
                await task
            await b.close()
            if blobs:
                await blobs.close()
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
        input_records = []
        if req.inputs:
            files = app.state.ctx["files"]
            if files is None:
                raise HTTPException(422, "File input is not enabled")
            try:
                input_records = await files.inputs_for_turn(who.uid, req.inputs)
            except FileError as e:
                raise HTTPException(e.status, e.detail) from e
        tokens = None
        if req.obo_resources:
            broker: OboBroker | None = app.state.ctx["obo"]
            if broker is None:
                raise HTTPException(422, "On-Behalf-Of is not enabled")
            if not who.assertion:
                raise HTTPException(401, f"No user token in the {cfg.obo.assertion_header} header")
            try:
                # Fail before taking a sandbox slot if the delegation cannot succeed.
                tokens = await broker.tokens(who.assertion, req.obo_resources,
                                             tenant_id=who.tenant_id, oid=who.oid)
            except OboError as e:
                headers = {"X-Claims-Challenge": e.claims} if e.claims and e.status == 403 else None
                raise HTTPException(e.status, e.detail, headers=headers) from e
        try:
            res = await svc().run_turn(who.uid, req.conversation_id, req.prompt, obo_tokens=tokens,
                                       inputs=input_records)
        except TurnInProgress:
            raise HTTPException(409, "A turn is already running for this user")
        except PoolBusy as e:
            return JSONResponse({"detail": "All sandboxes are busy"}, status_code=429,
                                headers={"Retry-After": str(e.retry_after)})
        except AgentError as e:
            log.error("agent error for %s: %s | %s", who.uid[:8], e, e.detail)
            raise HTTPException(502, str(e))
        return InvokeResponse(conversation_id=req.conversation_id, reply=res.reply,
                              sandbox_id=res.sandbox_id,
                              outputs=[r.public() for r in res.outputs],
                              skipped_outputs=res.skipped_outputs)

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

    # ---- files --------------------------------------------------------------------------------

    def file_service() -> FileService:
        files = app.state.ctx["files"]
        if files is None:
            raise HTTPException(404, "File storage is not enabled")
        return files

    @app.post("/v1/files", status_code=201)
    async def upload_file(request: Request, name: str = Query(..., min_length=1, max_length=255),
                          who: Principal = Depends(principal_dependency)):
        """Upload one file as the raw request body: `POST /v1/files?name=report.csv`."""
        files = file_service()
        declared = request.headers.get("content-length")
        try:
            rec = await files.upload(
                who.uid, name, request.headers.get("content-type"), request.stream(),
                int(declared) if declared and declared.isdigit() else None)
        except FileError as e:
            raise HTTPException(e.status, e.detail) from e
        return rec.public()

    @app.get("/v1/files")
    async def list_files(who: Principal = Depends(principal_dependency)):
        return [r.public() for r in await file_service().list(who.uid)]

    @app.get("/v1/files/{file_id}/metadata")
    async def file_metadata(file_id: Annotated[str, Path(pattern=FILE_ID)],
                            who: Principal = Depends(principal_dependency)):
        try:
            return (await file_service().get(who.uid, file_id)).public()
        except FileError as e:
            raise HTTPException(e.status, e.detail) from e

    @app.get("/v1/files/{file_id}")
    async def download_file(file_id: Annotated[str, Path(pattern=FILE_ID)],
                            who: Principal = Depends(principal_dependency)):
        files = file_service()
        try:
            rec = await files.get(who.uid, file_id)
        except FileError as e:
            raise HTTPException(e.status, e.detail) from e
        from urllib.parse import quote
        return StreamingResponse(files.download(rec), media_type=rec.content_type, headers={
            "Content-Length": str(rec.size),
            # Always a download, never rendered in this origin: agent output can be HTML.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(rec.name)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        })

    @app.delete("/v1/files/{file_id}", status_code=204)
    async def delete_file(file_id: Annotated[str, Path(pattern=FILE_ID)],
                          who: Principal = Depends(principal_dependency)):
        try:
            await file_service().delete(who.uid, file_id)
        except FileError as e:
            raise HTTPException(e.status, e.detail) from e

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
