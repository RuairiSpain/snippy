import base64
import json

import pytest

from eu_ai_action.api import Api, ApiError
from tests.conftest import TENANT

OTHER = "22222222-2222-2222-2222-222222222222"


def principal(tid, user="ana@contoso.example"):
    p = {"userDetails": user, "claims": [{"typ": "tid", "val": tid}]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(p).encode()).decode()}


@pytest.fixture
def secured(settings, pipeline):
    s = settings.model_copy(update={"auth_disabled": False})
    return Api(s, pipeline)


def test_requires_principal(secured):
    with pytest.raises(ApiError) as e:
        secured.list_agents({}, TENANT)
    assert e.value.status == 401


def test_cross_tenant_access_is_forbidden_on_every_route(secured):
    h = principal(OTHER)
    calls = [
        lambda: secured.start_scan(h, TENANT),
        lambda: secured.list_agents(h, TENANT),
        lambda: secured.get_agent(h, TENANT, "x"),
        lambda: secured.patch_inputs(h, TENANT, "x", {}),
        lambda: secured.patch_defaults(h, TENANT, {}),
        lambda: secured.regenerate(h, TENANT, "x"),
        lambda: secured.transition_document(h, TENANT, "x", "submit"),
        lambda: secured.get_file(h, TENANT, "x", "pdf"),
        lambda: secured.list_tasks(h, TENANT),
    ]
    for c in calls:
        with pytest.raises(ApiError) as e:
            c()
        assert e.value.status == 403


def test_allowed_tenant_list(settings, pipeline):
    s = settings.model_copy(update={"auth_disabled": False, "portal_allowed_tenant_ids": OTHER})
    with pytest.raises(ApiError) as e:
        Api(s, pipeline).list_agents(principal(TENANT), TENANT)
    assert e.value.status == 403


def test_end_to_end_through_api(secured):
    h = principal(TENANT)
    secured.p.s.render_formats = "docx,pdf,md"
    assert secured.start_scan(h, TENANT)["status"] == "completed"
    agents = secured.list_agents(h, TENANT)
    assert len(agents) == 10
    aid = "agent-public-nodisclosure"
    detail = secured.get_agent(h, TENANT, aid)
    assert detail["readiness"]["fail"] == 1
    # bad input rejected
    with pytest.raises(ApiError) as e:
        secured.patch_inputs(h, TENANT, aid, {"not_a_field": "x"})
    assert e.value.status == 400
    secured.patch_defaults(h, TENANT, {"controller_name": "Contoso SA", "controller_contact": "dpo@contoso.example"})
    secured.patch_inputs(h, TENANT, aid, {
        "business_purpose": "Answer product questions", "intended_users": "Visitors", "processing_purposes": "Support",
        "data_subjects": "Customers", "recipients": "None", "third_country_transfers": "None", "retention_period": "30 days",
    })
    docs = {(d["docType"], d["language"]): d for d in secured.get_agent(h, TENANT, aid)["documents"]}
    reg = docs[("register-entry", "en")]
    with pytest.raises(ApiError) as e:
        secured.transition_document(h, TENANT, reg["id"], "approve")  # not in review yet
    assert e.value.status == 409
    secured.transition_document(h, TENANT, reg["id"], "submit")
    done = secured.transition_document(h, TENANT, reg["id"], "approve")
    assert done["state"] == "approved" and done["approvedBy"] == "ana@contoso.example"
    data, ctype = secured.get_file(h, TENANT, reg["id"], "pdf")
    assert data.startswith(b"%PDF") and ctype == "application/pdf"
    md, _ = secured.get_file(h, TENANT, reg["id"], "md")
    assert b"Approved by ana@contoso.example" in md
    with pytest.raises(ApiError) as e:
        secured.get_file(h, TENANT, reg["id"], "exe")
    assert e.value.status == 404


def test_input_length_limit(secured):
    h = principal(TENANT)
    secured.start_scan(h, TENANT)
    with pytest.raises(ApiError) as e:
        secured.patch_inputs(h, TENANT, "agent-hr-faq", {"business_purpose": "x" * 2001})
    assert e.value.status == 400
