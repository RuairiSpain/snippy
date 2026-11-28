"""HTTP API. Any number of replicas can run this; all coordination is through Redis."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import telemetry
from .auth import Authenticator, Principal, principal_dependency
from .backends import make_backend
from .blobs import make_blob_store
from .delegation import DelegationError, DelegationService, TokenVault
from .files import FILE_ID, FileError, FileService
from .config import Config
from .pool import PoolBusy, PoolManager
from .approvals import APPROVAL_ID, ApprovalError, ApprovalService
from .obo import OboBroker, OboError
from .reaper import Reaper
from .service import AgentError, SessionService, TurnInProgress
from .store import Store
from .triggers import SUB_ID, CallbackSecrets, TriggerError, TriggerService

log = logging.getLogger(__name__)
CONV_ID = r"^[A-Za-z0-9_-]{1,64}$"


class InvokeRequest(BaseModel):
    conversation_id: str = Field("default", pattern=CONV_ID)
    prompt: str
    # Aliases from `obo.resources` whose delegated tokens the agent needs this turn.
    obo_resources: list[str] = Field(default_factory=list, max_length=10)
    # Ids from POST /v1/files. They appear in the agent's $INPUT_DIR for this turn only.
    inputs: list[Annotated[str, Field(pattern=FILE_ID)]] = Field(default_factory=list, max_length=50)


class TriggerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    source: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    # e.g. {"from": ["alice@contoso.com", "@partner.com"], "subject_contains": ["invoice"]}
    filters: dict[str, list[str]] = Field(default_factory=dict)
    instruction: str = Field(..., min_length=1, max_length=4000)
    # Aliases from obo.resources the agent may use as the user while they are away. Needs consent.
    delegated_resources: list[str] = Field(default_factory=list, max_length=5)
    expires_in_days: int | None = Field(None, ge=1, le=365)
    include_own_messages: bool = False


class ApprovalDecision(BaseModel):
    # Resource aliases (obo.resources) the approved action may use, as you, right now. None means
    # it runs with no delegated access.
    obo_resources: list[str] = Field(default_factory=list, max_length=5)


class ConsentStart(BaseModel):
    resource: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,40}$")


class InvokeResponse(BaseModel):
    conversation_id: str
    reply: str
    sandbox_id: str
    outputs: list[dict] = []
    skipped_outputs: list[dict] = []


def create_app(cfg: Config | None = None, *, backend=None, redis_client=None,
               run_reaper: bool = True, obo_broker: OboBroker | None = None,
               blob_store=None, msal_factory=None, vault: TokenVault | None = None,
               callback_secrets: CallbackSecrets | None = None) -> FastAPI:
    cfg = cfg or Config.load()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        telemetry.setup()
        pool_cfg_for_trigger = cfg.triggers_pool_cfg()
        for c in {id(x): x for x in (cfg, pool_cfg_for_trigger)}.values():
            for what, ratio in c.overcommit().items():
                if ratio > 1:
                    log.warning("pool %s: per-user %s limits add up to %.1fx the sandbox size at %d "
                                "users: a full sandbox can still run out. Lower limits.%s or "
                                "users_per_sandbox.", c.pool.name, what, ratio,
                                c.pool.users_per_sandbox, what)
        r = redis_client or redis.from_url(cfg.redis.resolved_url())
        b = backend or make_backend(cfg)
        await b.start()
        store = Store(r, cfg.redis.key_prefix, cfg.pool.name)
        main_b = b.with_config(cfg)      # each pool talks to the shared connection with its own settings
        pool = PoolManager(cfg, store, main_b)
        blobs = None
        files = None
        if cfg.files.enabled:
            blobs = blob_store or make_blob_store(cfg.files)
            files = FileService(cfg, store, blobs)
        service = SessionService(cfg, store, main_b, pool, files)
        triggers = delegation = approvals = None
        secrets_for_callbacks = callback_secrets
        obo_instance = obo_broker or (OboBroker(cfg) if cfg.obo.enabled else None)
        # Triggered runs get their own pool (own sandboxes, egress, connectors, limits) unless
        # triggers.pool.separate is false. Same backend connection, separate settings and state.
        trigger_service, trigger_pool, trigger_reaper, trigger_store = service, pool, None, store
        if pool_cfg_for_trigger is not cfg:
            trigger_store = Store(r, cfg.redis.key_prefix, pool_cfg_for_trigger.pool.name)
            trigger_backend = b.with_config(pool_cfg_for_trigger)
            trigger_pool = PoolManager(pool_cfg_for_trigger, trigger_store, trigger_backend)
            trigger_service = SessionService(pool_cfg_for_trigger, trigger_store, trigger_backend,
                                             trigger_pool, files)
            trigger_reaper = Reaper(pool_cfg_for_trigger, trigger_store, trigger_backend,
                                    trigger_pool, trigger_service)
        worker_tasks: list[asyncio.Task] = []
        stop_workers = asyncio.Event()
        if cfg.triggers.enabled:
            if cfg.triggers.delegation.enabled:
                delegation = DelegationService(
                    cfg, store, vault or TokenVault.from_env(cfg.triggers.delegation.key_env),
                    msal_factory)
            approvals = (ApprovalService(cfg, store, service, obo_instance)
                         if cfg.triggers.approvals.enabled else None)
            if cfg.triggers.require_source_secret and secrets_for_callbacks is None:
                secrets_for_callbacks = CallbackSecrets.from_env(cfg.triggers.callback_key_env)
            triggers = TriggerService(cfg, store, trigger_service, delegation, approvals)
            if run_reaper:       # workers run wherever the maintenance loop does (production)
                worker_tasks = [asyncio.create_task(triggers.worker_loop(stop_workers))
                                for _ in range(cfg.triggers.workers)]
        reaper = Reaper(cfg, store, main_b, pool, service, files=files, triggers=triggers)
        app.state.ctx = dict(cfg=cfg, store=store, pool=pool, service=service, files=files,
                             triggers=triggers, delegation=delegation, trigger_service=trigger_service,
                             trigger_pool=trigger_pool, trigger_store=trigger_store,
                             trigger_reaper=trigger_reaper, reaper=reaper, approvals=approvals,
                             callback_secrets=secrets_for_callbacks, obo=obo_instance)
        app.state.auth = Authenticator(cfg.auth)
        task = asyncio.create_task(reaper.run()) if run_reaper else None
        trigger_task = (asyncio.create_task(trigger_reaper.run())
                        if run_reaper and trigger_reaper else None)
        try:
            yield
        finally:
            reaper.stop()
            if trigger_reaper:
                trigger_reaper.stop()
            stop_workers.set()
            for w in worker_tasks:
                w.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            if task:
                await task
            if trigger_task:
                await trigger_task
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
            ts = app.state.ctx["trigger_service"]
            if ts is not svc():                 # also delete what a triggered run left in its pool
                await ts.end_session(who.uid)
        except TurnInProgress:
            raise HTTPException(409, "A turn is running for this user")

    @app.get("/v1/pool/status")
    async def pool_status(who: Principal = Depends(principal_dependency)):
        if who.oid not in cfg.auth.admin_oids and cfg.auth.mode != "dev":
            raise HTTPException(403)
        async def one(st, c):
            return {"pool": c.pool.name, "loads": await st.loads(), "states": await st.states(),
                    "creating": await st.creating_count(), "waiting": await st.waiting_count(),
                    "users_per_sandbox": c.pool.users_per_sandbox,
                    "max_sandboxes": c.pool.max_sandboxes, "egress": c.pool.egress.mode,
                    "egress_connector_hosts": c.pool.egress.allow_connector_hosts,
                    "connectors": len(c.connectors.ids)}
        out = await one(app.state.ctx["store"], cfg)
        if app.state.ctx["triggers"] is not None:
            out["trigger_queue"] = await app.state.ctx["triggers"].queue_stats()
        ts = app.state.ctx["trigger_store"]
        if ts is not app.state.ctx["store"]:
            out["triggers_pool"] = await one(ts, cfg.triggers_pool_cfg())
        return out

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

    # ---- triggers (registered only when a developer enables them in the config) ----------------------

    if cfg.triggers.enabled:
        _register_trigger_routes(app, cfg)

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


async def _read_json_limited(request: Request, limit: int):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Payload too large")
    total, chunks = 0, []
    async for c in request.stream():
        total += len(c)
        if total > limit:
            raise HTTPException(413, "Payload too large")
        chunks.append(c)
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        raise HTTPException(400, "Body is not JSON") from None


def _register_trigger_routes(app: FastAPI, cfg: Config) -> None:
    t = cfg.triggers

    def triggers() -> TriggerService:
        return app.state.ctx["triggers"]

    def delegation() -> DelegationService:
        d = app.state.ctx["delegation"]
        if d is None:
            raise HTTPException(404, "Delegated triggers are not enabled")
        return d

    def fail(e: TriggerError | DelegationError) -> HTTPException:
        return HTTPException(e.status, e.detail)

    # The connector namespace calls this with its managed identity.
    @app.post("/v1/triggers/callback/{source}")
    async def callback(source: str, request: Request, authorization: str | None = Header(None)):
        await app.state.auth.authenticate_service(
            authorization, t.callback_audience or cfg.auth.audience, t.callback_principal_ids)
        secrets_ = app.state.ctx["callback_secrets"]
        if secrets_ and not secrets_.verify(source, request.query_params.get("k")):
            # Same answer as a wrong identity, so a caller learns nothing about which sources exist.
            raise HTTPException(403, "Caller is not an allowed identity")
        payload = await _read_json_limited(request, t.max_payload_bytes)
        try:
            return {"ok": True, **await triggers().handle_callback(source, payload)}
        except TriggerError as e:
            raise fail(e) from e

    @app.get("/v1/triggers/sources")
    async def sources(who: Principal = Depends(principal_dependency)):
        return [{"source": n, "kind": s.kind, "filters": s.allow_filters or sorted(s.field_paths()),
                 "text_filters": "add _contains to a field name, e.g. subject_contains"}
                for n, s in t.sources.items()]

    # Consent for unattended delegated access (option C). Declared before the {id} routes.
    if t.delegation.enabled:
        @app.post("/v1/triggers/consent/start")
        async def consent_start(req: ConsentStart, who: Principal = Depends(principal_dependency)):
            if req.resource not in t.delegated_aliases:
                raise HTTPException(422, f"'{req.resource}' is not allowed for unattended runs. Allowed: "
                                         f"{', '.join(t.delegated_aliases)}")
            try:
                return await delegation().start(who.uid, who.upn, req.resource)
            except DelegationError as e:
                raise fail(e) from e

        @app.get("/v1/triggers/consent/callback", response_class=HTMLResponse)
        async def consent_callback(request: Request):
            # The browser lands here after the Microsoft sign-in page. No bearer token: the
            # one-time `state` ties it to the user who started it, and the account that signs in
            # must be that same user.
            headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            try:
                await delegation().complete(dict(request.query_params))
            except DelegationError as e:
                return HTMLResponse(f"<h1>Consent not saved</h1><p>{e.detail}</p>",
                                    status_code=e.status, headers=headers)
            return HTMLResponse("<h1>Consent saved</h1><p>You can close this window.</p>", headers=headers)

        @app.get("/v1/triggers/consent")
        async def consent_status(who: Principal = Depends(principal_dependency)):
            s = await delegation().status(who.uid)
            return {"resources": s["resources"], "expires_at": s["expires_at"]} if s else {"resources": []}

        @app.delete("/v1/triggers/consent", status_code=204)
        async def consent_revoke(who: Principal = Depends(principal_dependency)):
            await delegation().revoke(who.uid)
            await triggers().disable_delegated(who.uid, "Consent revoked")

    else:
        @app.api_route("/v1/triggers/consent", methods=["GET", "POST", "DELETE"])
        @app.api_route("/v1/triggers/consent/{rest:path}", methods=["GET", "POST", "DELETE"])
        async def consent_disabled(rest: str = ""):
            raise HTTPException(404, "Delegated triggers are not enabled")

    if t.approvals.enabled:
        def approvals() -> ApprovalService:
            return app.state.ctx["approvals"]

        @app.get("/v1/approvals")
        async def list_approvals(status: str | None = Query(None, pattern=r"^(pending|approved|rejected|expired|failed)$"),
                                 who: Principal = Depends(principal_dependency)):
            return await approvals().list(who.uid, status)

        @app.get("/v1/approvals/{approval_id}")
        async def get_approval(approval_id: Annotated[str, Path(pattern=APPROVAL_ID)],
                               who: Principal = Depends(principal_dependency)):
            try:
                return approvals().public(await approvals().get(who.uid, approval_id))
            except ApprovalError as e:
                raise HTTPException(e.status, e.detail) from e

        @app.post("/v1/approvals/{approval_id}/reject")
        async def reject_approval(approval_id: Annotated[str, Path(pattern=APPROVAL_ID)],
                                  who: Principal = Depends(principal_dependency)):
            try:
                return await approvals().reject(who.uid, approval_id)
            except ApprovalError as e:
                raise HTTPException(e.status, e.detail) from e

        @app.post("/v1/approvals/{approval_id}/approve")
        async def approve_approval(approval_id: Annotated[str, Path(pattern=APPROVAL_ID)],
                                   req: ApprovalDecision, who: Principal = Depends(principal_dependency)):
            """Carry out the action now, in your own session and with your own sign-in."""
            try:
                return await approvals().approve(who.uid, who.tenant_id, who.oid, who.assertion,
                                                 approval_id, req.obo_resources)
            except ApprovalError as e:
                raise HTTPException(e.status, e.detail) from e

    @app.post("/v1/triggers", status_code=201)
    async def create_trigger(req: TriggerCreate, who: Principal = Depends(principal_dependency)):
        try:
            sub = await triggers().create(
                who.uid, who.tenant_id, who.oid, who.upn, name=req.name, source=req.source,
                filters=req.filters, instruction=req.instruction,
                delegated_resources=req.delegated_resources, expires_in_days=req.expires_in_days,
                include_own=req.include_own_messages)
        except TriggerError as e:
            raise fail(e) from e
        return sub.public()

    @app.get("/v1/triggers")
    async def list_triggers(who: Principal = Depends(principal_dependency)):
        return [s.public() for s in await triggers().list(who.uid)]

    @app.get("/v1/triggers/{sub_id}")
    async def get_trigger(sub_id: Annotated[str, Path(pattern=SUB_ID)],
                          who: Principal = Depends(principal_dependency)):
        try:
            return (await triggers().get(who.uid, sub_id)).public()
        except TriggerError as e:
            raise fail(e) from e

    @app.post("/v1/triggers/{sub_id}/{action}")
    async def trigger_action(sub_id: Annotated[str, Path(pattern=SUB_ID)],
                             action: Annotated[str, Path(pattern=r"^(enable|disable)$")],
                             who: Principal = Depends(principal_dependency)):
        try:
            return (await triggers().set_state(who.uid, sub_id, action == "enable")).public()
        except TriggerError as e:
            raise fail(e) from e

    @app.delete("/v1/triggers/{sub_id}", status_code=204)
    async def delete_trigger(sub_id: Annotated[str, Path(pattern=SUB_ID)],
                             who: Principal = Depends(principal_dependency)):
        try:
            await triggers().delete(who.uid, sub_id)
        except TriggerError as e:
            raise fail(e) from e

    @app.get("/v1/triggers/{sub_id}/runs")
    async def trigger_runs(sub_id: Annotated[str, Path(pattern=SUB_ID)],
                           who: Principal = Depends(principal_dependency)):
        try:
            return await triggers().runs(who.uid, sub_id)
        except TriggerError as e:
            raise fail(e) from e
