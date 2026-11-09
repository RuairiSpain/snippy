"""Azure Functions entry point. Registers triggers only; logic lives in eu_ai_action."""
from __future__ import annotations

import json
import logging

import azure.durable_functions as df
import azure.functions as func

from eu_ai_action.api import ApiError
from eu_ai_action.api.auth import AuthError, authorize
from eu_ai_action.models import AgentRef
from eu_ai_action.runtime import get_api, get_pipeline
from eu_ai_action.settings import get_settings

# Authentication is enforced by App Service authentication (Easy Auth) in front of the app, which sets
# X-MS-CLIENT-PRINCIPAL; each handler then checks that the caller's Entra tenant equals the tenant in the path.
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _json(body, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, ensure_ascii=False), status_code=status, mimetype="application/json")


def _handle(fn) -> func.HttpResponse:
    try:
        return _json(fn())
    except ApiError as e:
        return _json({"error": e.message}, e.status)
    except Exception:  # noqa: BLE001
        logging.exception("unhandled error")
        return _json({"error": "internal error"}, 500)


def _body(req: func.HttpRequest) -> dict:
    try:
        b = req.get_json()
    except ValueError as e:
        raise ApiError(400, "invalid JSON body") from e
    if not isinstance(b, dict):
        raise ApiError(400, "JSON object expected")
    return b


# ---- HTTP API ----------------------------------------------------------------------------------
@app.route(route="tenants/{tenant_id}/scans", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_scan(req: func.HttpRequest, client) -> func.HttpResponse:
    tenant_id = req.route_params["tenant_id"]
    try:
        authorize(req.headers, tenant_id, get_settings())
    except AuthError as e:
        return _json({"error": e.message}, e.status)
    instance_id = await client.start_new("scan_orchestrator", None, tenant_id)
    return client.create_check_status_response(req, instance_id)


@app.route(route="tenants/{tenant_id}/agents", methods=["GET"])
def list_agents(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().list_agents(req.headers, req.route_params["tenant_id"]))


@app.route(route="tenants/{tenant_id}/agents/{agent_id}", methods=["GET"])
def get_agent(req: func.HttpRequest) -> func.HttpResponse:
    p = req.route_params
    return _handle(lambda: get_api().get_agent(req.headers, p["tenant_id"], p["agent_id"]))


@app.route(route="tenants/{tenant_id}/agents/{agent_id}/inputs", methods=["PATCH"])
def patch_inputs(req: func.HttpRequest) -> func.HttpResponse:
    p = req.route_params
    return _handle(lambda: get_api().patch_inputs(req.headers, p["tenant_id"], p["agent_id"], _body(req)))


@app.route(route="tenants/{tenant_id}/agents/{agent_id}/generate", methods=["POST"])
def regenerate(req: func.HttpRequest) -> func.HttpResponse:
    p = req.route_params
    return _handle(lambda: get_api().regenerate(req.headers, p["tenant_id"], p["agent_id"]))


@app.route(route="tenants/{tenant_id}/defaults", methods=["PATCH"])
def patch_defaults(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().patch_defaults(req.headers, req.route_params["tenant_id"], _body(req)))


@app.route(route="tenants/{tenant_id}/documents/{doc_id}/transition", methods=["POST"])
def transition_document(req: func.HttpRequest) -> func.HttpResponse:
    p = req.route_params
    return _handle(lambda: get_api().transition_document(req.headers, p["tenant_id"], p["doc_id"], str(_body(req).get("action", ""))))


@app.route(route="tenants/{tenant_id}/documents/{doc_id}/file", methods=["GET"])
def get_file(req: func.HttpRequest) -> func.HttpResponse:
    p = req.route_params
    try:
        data, ctype = get_api().get_file(req.headers, p["tenant_id"], p["doc_id"], req.params.get("format", "pdf"))
    except ApiError as e:
        return _json({"error": e.message}, e.status)
    return func.HttpResponse(data, mimetype=ctype, headers={"Content-Disposition": f'attachment; filename="{p["doc_id"]}.{req.params.get("format", "pdf")}"'})


@app.route(route="tenants/{tenant_id}/profile", methods=["GET"])
def get_profile(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().get_profile(req.headers, req.route_params["tenant_id"]))


@app.route(route="tenants/{tenant_id}/profile", methods=["PATCH"])
def patch_profile(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().patch_profile(req.headers, req.route_params["tenant_id"], _body(req)))


@app.route(route="tenants/{tenant_id}/audit", methods=["GET"])
def audit_pack(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().audit_pack(req.headers, req.route_params["tenant_id"]))


@app.route(route="tenants/{tenant_id}/documents/{doc_id}/verify", methods=["GET"])
def verify_document(req: func.HttpRequest) -> func.HttpResponse:
    p = req.route_params
    return _handle(lambda: get_api().verify(req.headers, p["tenant_id"], p["doc_id"]))


@app.route(route="tenants/{tenant_id}/export", methods=["POST"])
def export_bundle(req: func.HttpRequest) -> func.HttpResponse:
    """Evidence bundle (zip). The body may hold the customer's Compliance Manager export (.xlsx) to be filled in."""
    try:
        data = get_api().export_bundle(req.headers, req.route_params["tenant_id"], req.get_body() or None)
    except ApiError as e:
        return _json({"error": e.message}, e.status)
    return func.HttpResponse(data, mimetype="application/zip", headers={"Content-Disposition": 'attachment; filename="eu-ai-action-evidence.zip"'})


@app.route(route="tenants/{tenant_id}/notify", methods=["POST"])
def notify(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().notify(req.headers, req.route_params["tenant_id"], req.params.get("portal_url", "")))


@app.route(route="tenants/{tenant_id}/tasks", methods=["GET"])
def list_tasks(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: get_api().list_tasks(req.headers, req.route_params["tenant_id"]))


# ---- scan orchestration ------------------------------------------------------------------------
@app.orchestration_trigger(context_name="context")
def scan_orchestrator(context: df.DurableOrchestrationContext):
    tenant_id = context.get_input()
    agents = yield context.call_activity("act_list_agents", tenant_id)
    scan_id = yield context.call_activity("act_start_scan", {"tenant_id": tenant_id, "count": len(agents)})
    tasks = [context.call_activity("act_process_agent", {"tenant_id": tenant_id, "scan_id": scan_id, "agent": a}) for a in agents]
    results = yield context.task_all(tasks)
    errors = [e for r in results for e in r]
    return (yield context.call_activity("act_finish_scan", {"tenant_id": tenant_id, "scan_id": scan_id, "errors": errors}))


@app.activity_trigger(input_name="tenant_id")
def act_list_agents(tenant_id: str) -> list[dict]:
    return [a.to_doc() for a in get_pipeline().list_agents(tenant_id)]


@app.activity_trigger(input_name="payload")
def act_start_scan(payload: dict) -> str:
    return get_pipeline().new_scan(payload["tenant_id"], payload["count"])


@app.activity_trigger(input_name="payload")
def act_process_agent(payload: dict) -> list[str]:
    return get_pipeline().process_agent(payload["tenant_id"], AgentRef.model_validate(payload["agent"]), payload["scan_id"])


@app.activity_trigger(input_name="payload")
def act_finish_scan(payload: dict) -> dict:
    return get_pipeline().finish_scan(payload["tenant_id"], payload["scan_id"], payload["errors"])


# ---- MCP tools ---------------------------------------------------------------------------------
# MCP clients authenticate to the Functions MCP endpoint with the system key, not as an Entra user, so these
# tools run for the tenant in settings.TARGET_TENANT_ID only and approval is not exposed.
def _mcp_args(context: str) -> dict:
    return json.loads(context).get("arguments", {})


def _mcp_tenant() -> str:
    t = get_settings().target_tenant_id
    if not t:
        raise ApiError(400, "TARGET_TENANT_ID is not configured")
    return t


class _Prop:
    def __init__(self, name: str, desc: str):
        self.d = {"propertyName": name, "propertyType": "string", "description": desc}


def _props(*items: _Prop) -> str:
    return json.dumps([i.d for i in items])


@app.mcp_tool_trigger(
    arg_name="context", tool_name="list_agents",
    description="Lists the Microsoft agents known for the configured tenant with owner, public-facing flag and readiness counts.",
    tool_properties="[]",
)
def mcp_list_agents(context: str) -> str:
    return json.dumps(get_api().list_agents_internal(_mcp_tenant()), ensure_ascii=False)


@app.mcp_tool_trigger(
    arg_name="context", tool_name="get_compliance_file",
    description="Returns the compliance file of one agent: facts with provenance, duty results, documents and readiness.",
    tool_properties=_props(_Prop("agentid", "The agent id from list_agents.")),
)
def mcp_get_compliance_file(context: str) -> str:
    return json.dumps(get_api().get_agent_internal(_mcp_tenant(), _mcp_args(context).get("agentid", "")), ensure_ascii=False)


@app.mcp_tool_trigger(
    arg_name="context", tool_name="generate_document",
    description="Regenerates the draft compliance documents of one agent from current facts and human inputs. Does not approve anything.",
    tool_properties=_props(_Prop("agentid", "The agent id from list_agents.")),
)
def mcp_generate_document(context: str) -> str:
    t, a = _mcp_tenant(), _mcp_args(context).get("agentid", "")
    get_pipeline().regenerate(t, a)
    return json.dumps(get_api().get_agent_internal(t, a), ensure_ascii=False)
