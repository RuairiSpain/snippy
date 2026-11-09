import base64
import io
import json
import zipfile

import pytest
from openpyxl import Workbook, load_workbook

from eu_ai_action.api import Api, ApiError
from eu_ai_action.export import build_bundle, collect_approved
from eu_ai_action.notifications import build_card, notify_open_tasks
from eu_ai_action.notifications.teams import check_webhook_url
from tests.conftest import TENANT, scan_agent

PUB = "agent-public-nodisclosure"
FULL = {
    "business_purpose": "Answer product questions", "intended_users": "Visitors", "controller_name": "Contoso SA",
    "controller_contact": "dpo@contoso.example", "processing_purposes": "Support", "data_subjects": "Customers",
    "recipients": "None", "third_country_transfers": "None", "retention_period": "30 days",
}


def approve_all(api, pipeline, agent=PUB, types=("register-entry", "ropa-entry")):
    scan_agent(pipeline, agent)
    api.patch_inputs({}, TENANT, agent, FULL)
    ids = []
    for t in types:
        for lang in pipeline.s.languages:
            d = pipeline.latest_document(TENANT, agent, t, lang)
            api.transition_document({}, TENANT, d.id, "submit")
            api.transition_document({}, TENANT, d.id, "approve")
            ids.append(d.id)
    return ids


@pytest.fixture
def api(settings, pipeline):
    return Api(settings, pipeline)


def cm_template():
    wb = Workbook()
    ws = wb.active
    ws.append(["Some title row"])
    ws.append(["Improvement action", "Points", "Implementation status", "Test status", "Implementation notes"])
    ws.append(["Ensure transparency for AI interactions (Article 50)", 5, "Not implemented", "Not assessed", ""])
    ws.append(["Maintain a record of processing activities (Article 30)", 3, "Not implemented", "Not assessed", ""])
    ws.append(["Unrelated action", 1, "Not implemented", "Not assessed", ""])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_collect_approved_only(api, pipeline):
    ids = approve_all(api, pipeline)
    got = {d.id for d in collect_approved(pipeline, TENANT)}
    assert got == set(ids)


def test_bundle_manifest_and_files(api, pipeline):
    ids = approve_all(api, pipeline)
    z = zipfile.ZipFile(io.BytesIO(build_bundle(pipeline, TENANT)))
    names = set(z.namelist())
    assert "manifest.json" in names and "compliance-manager-update.xlsx" in names
    manifest = json.loads(z.read("manifest.json"))
    assert {d["id"] for d in manifest["documents"]} == set(ids)
    doc = manifest["documents"][0]
    assert doc["approvedBy"] == "dev-user" and doc["fileHash"] and doc["files"]["md"] in names
    assert manifest["complianceManager"]["unverifiedColumnMapping"] is True
    # the standalone sheet has one row per mapped rule that applies
    ws = load_workbook(io.BytesIO(z.read("compliance-manager-update.xlsx"))).active
    rows = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows["GDPR-30-ROPA"][1] == "Implemented"  # approved in both languages
    assert rows["AIA-50-1-disclosure"][1] == "Not implemented"  # the check failed and nothing is approved


def test_fill_customer_export_preserves_other_content(api, pipeline):
    approve_all(api, pipeline)
    z = zipfile.ZipFile(io.BytesIO(build_bundle(pipeline, TENANT, cm_template())))
    wb = load_workbook(io.BytesIO(z.read("compliance-manager-update.xlsx")))
    ws = wb.active
    assert ws["A1"].value == "Some title row"  # rows above the header are untouched
    by_name = {r[0].value: r for r in ws.iter_rows(min_row=3)}
    art50 = by_name["Ensure transparency for AI interactions (Article 50)"]
    assert (art50[2].value, art50[3].value) == ("Not implemented", "Failed")
    assert art50[1].value == 5  # unrelated columns preserved
    ropa = by_name["Maintain a record of processing activities (Article 30)"]
    assert (ropa[2].value, ropa[3].value) == ("Implemented", "Passed") and "approved documents" in ropa[4].value
    assert by_name["Unrelated action"][2].value == "Not implemented" and by_name["Unrelated action"][4].value in (None, "")
    manifest = json.loads(z.read("manifest.json"))
    assert "GDPR-30-ROPA" in manifest["complianceManager"]["matched"] and "AIA-26-2-OVERSIGHT" not in manifest["complianceManager"]["matched"]


def test_bundle_rejects_bad_workbook(api, pipeline):
    scan_agent(pipeline, PUB)
    with pytest.raises(ApiError) as e:
        api.export_bundle({}, TENANT, b"not an xlsx file")
    assert e.value.status == 400


def test_verify_detects_tampering(api, pipeline):
    ids = approve_all(api, pipeline)
    doc_id = ids[0]
    assert api.verify({}, TENANT, doc_id)["intact"] is True
    doc = pipeline.store.get("documents", TENANT, doc_id)
    path = doc["filePaths"]["md"]
    pipeline.files.put(TENANT, path.split("/", 1)[1], b"# altered after approval\n")
    v = api.verify({}, TENANT, doc_id)
    assert v["intact"] is False and v["currentHash"] != v["approvalHash"]


def test_audit_pack_lists_approvals_and_log(api, pipeline):
    approve_all(api, pipeline)
    pack = api.audit_pack({}, TENANT)
    assert len(pack["documents"]) == 4
    actions = [a["action"] for a in pack["auditLog"]]
    assert actions.count("approve") == 4 and actions.count("submit") == 4
    assert all(a["fileHash"] for a in pack["auditLog"] if a["action"] == "approve")


def principal(tid, roles=()):
    p = {"userDetails": "auditor@firm.example", "claims": [{"typ": "tid", "val": tid}] + [{"typ": "roles", "val": r} for r in roles]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(p).encode()).decode()}


def test_auditor_is_read_only(settings, pipeline):
    approve_all(Api(settings, pipeline), pipeline)
    api = Api(settings.model_copy(update={"auth_disabled": False}), pipeline)
    h = principal(TENANT, ["Auditor"])
    assert api.audit_pack(h, TENANT)["documents"] and api.list_agents(h, TENANT)
    assert api.export_bundle(h, TENANT)
    d = pipeline.latest_document(TENANT, PUB, "register-entry", "en")
    for call in (
        lambda: api.start_scan(h, TENANT), lambda: api.patch_inputs(h, TENANT, PUB, {}), lambda: api.patch_profile(h, TENANT, {}),
        lambda: api.patch_defaults(h, TENANT, {}), lambda: api.regenerate(h, TENANT, PUB),
        lambda: api.transition_document(h, TENANT, d.id, "reject"), lambda: api.notify(h, TENANT),
    ):
        with pytest.raises(ApiError) as e:
            call()
        assert e.value.status == 403
    # a user without the role can write
    api.patch_defaults(principal(TENANT), TENANT, {"controller_name": "Contoso SA"})


def test_audit_routes_are_tenant_scoped(settings, pipeline):
    api = Api(settings.model_copy(update={"auth_disabled": False}), pipeline)
    h = principal("22222222-2222-2222-2222-222222222222")
    for call in (lambda: api.audit_pack(h, TENANT), lambda: api.verify(h, TENANT, "x"), lambda: api.export_bundle(h, TENANT), lambda: api.notify(h, TENANT)):
        with pytest.raises(ApiError) as e:
            call()
        assert e.value.status == 403


# ---- Teams ---------------------------------------------------------------------------------------
def test_card_structure_and_truncation():
    tasks = [{"id": str(i), "title": f"Provide field {i}"} for i in range(25)]
    card = build_card(tasks, "https://portal.example")
    content = card["attachments"][0]["content"]
    assert card["type"] == "message" and content["type"] == "AdaptiveCard" and content["version"] == "1.4"
    texts = [b["text"] for b in content["body"]]
    assert texts[0].endswith("25 open task(s)") and "and 5 more" in texts[-1]
    assert content["actions"][0]["url"] == "https://portal.example"


@pytest.mark.parametrize("url", ["http://x.webhook.office.com/a", "https://127.0.0.1/a", "https://localhost/a", "https://db.internal/a", "ftp://x.com", ""])
def test_webhook_url_checks(url):
    with pytest.raises(ValueError):
        check_webhook_url(url)


def test_notify_posts_card_only_when_there_are_tasks(pipeline):
    import httpx

    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify_open_tasks(pipeline, TENANT, "", client=client)["sent"] is False
    assert notify_open_tasks(pipeline, TENANT, "https://x.logic.azure.com/w", client=client) == {"sent": False, "reason": "no open tasks"}
    scan_agent(pipeline, PUB)
    r = notify_open_tasks(pipeline, TENANT, "https://x.logic.azure.com/w", "https://portal", client=client)
    assert r["sent"] is True and len(sent) == 1 and sent[0]["attachments"]
