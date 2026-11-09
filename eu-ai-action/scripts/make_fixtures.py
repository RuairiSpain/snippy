"""Writes the 8 hand-written test agents. The payload shape is the ASSUMED shape of the collector evidence;
replace these files with recorded responses after spike M1 (see spikes/)."""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/agents")


def greeting(text):
    if text is None:
        return []
    return [{
        "name": "Conversation Start", "schemaname": "cr_agent.topic.ConversationStart", "componenttype": 9,
        "content": (
            "kind: AdaptiveDialog\nbeginDialog:\n  kind: OnConversationStart\n  actions:\n"
            f"    - kind: SendActivity\n      activity: {json.dumps(text, ensure_ascii=False)}\n"
        ),
    }]


def knowledge(name, site):
    return {"name": name, "schemaname": f"cr_agent.knowledge.{name}", "componenttype": 16,
            "content": f"kind: KnowledgeSourceConfiguration\nsource:\n  kind: SharePointSearchSource\n  site: {site}\n"}


def connector(name):
    return {"name": name, "schemaname": f"cr_agent.action.{name}", "componenttype": 9,
            "content": f"kind: ConnectorAction\nconnector: {name}\n"}


def instructions(text):
    return {"name": "Instructions", "schemaname": "cr_agent.gpt.default", "componenttype": 15,
            "content": f"kind: GptComponentMetadata\ninstructions: {json.dumps(text, ensure_ascii=False)}\n"}


def agent(aid, name, auth, channels, greet, comps=(), lcid=1033, owner="Ana Ruiz", sponsor="Luis Mora", labels=(), instr="Answer questions.", extra=None):
    d = {
        "agent_id": aid, "name": name,
        "dataverse": {
            "bot": {"botid": aid, "name": name, "schemaname": f"cr_{aid}", "authenticationmode": auth, "language": lcid,
                    "createdon": "2026-09-01T09:00:00Z", "modifiedon": "2026-10-20T15:30:00Z", "statecode": 0},
            "components": greeting(greet) + [instructions(instr)] + list(comps),
            "channels": channels,
        },
    }
    if owner:
        d["registry"] = {"owner": {"displayName": owner}, "sponsor": {"displayName": sponsor}, "lastActivity": "2026-11-05T10:00:00Z"}
    if labels:
        d["purview"] = {"labels": [{"source": s, "label": lab} for s, lab in labels]}
    d.update(extra or {})
    return d


def users(*rows):
    return {"users": [{"displayName": n, "role": r} for n, r in rows], "accessGroups": [{"name": "Agent users", "memberCount": len(rows)}]}


def learning(*names):
    return {"completions": [{"user": n, "course": "AI literacy", "date": "2026-05-01"} for n in names]}


FOUNDRY = {"deployments": [{"name": "gpt-4o", "provider": "Azure OpenAI", "region": "swedencentral", "fineTuned": False}],
           "evaluations": [{"name": "groundedness", "date": "2026-10-01", "score": 0.91}], "generatesContent": ["text"], "contentCredentials": False}

AGENTS = [
    agent("agent-public-disclosed", "Atencion al cliente web", 1, ["DirectLine"],
          "Hola, soy un asistente de inteligencia artificial. ¿En qué puedo ayudarte?",
          [knowledge("Customer FAQ", "https://contoso.sharepoint.com/sites/faq")], lcid=3082, labels=[("Customer FAQ", "General")]),
    agent("agent-public-nodisclosure", "Public Help Bot", 1, ["DirectLine"], "Hello! How can I help you today?",
          [knowledge("Product docs", "https://contoso.sharepoint.com/sites/docs")]),
    agent("agent-teams-internal", "IT Helpdesk", 2, ["MsTeams"], "Hi, I'm the IT helpdesk. What do you need?"),
    agent("agent-hr-faq", "HR Policy Assistant", 2, ["MsTeams"], "Hi, I am an AI assistant for HR policy questions.",
          [knowledge("HR-Policies", "https://contoso.sharepoint.com/sites/hr")], labels=[("HR-Policies", "Confidential")],
          instr="Answer employee questions about HR policies using the HR policy library only.",
          extra={
              "entra": users(("Ana Ruiz", "owner"), ("Luis Mora", "overseer"), ("Pia Sanz", "user")),
              "learning": learning("Ana Ruiz", "Luis Mora"),
              "retention": {"purviewAuditDays": 180, "logAnalyticsDays": 365, "appInsightsDays": 90},
              "foundry": FOUNDRY,
              "defender": {"incidents": [{
                  "id": "INC-100", "title": "Unusual data access by agent", "severity": "high", "status": "active",
                  "detectedTime": "2026-11-01T08:00:00Z", "classifiedTime": "2026-11-01T10:00:00Z",
                  "personalDataAffected": True, "seriousIncidentCandidate": False,
                  "description": "The agent read documents outside its configured sources."}]},
          }),
    agent("agent-finance-labels", "Finance Reports Agent", 2, ["MsTeams", "SharePoint"], "Welcome to the finance reports agent.",
          [knowledge("Finance-Invoices", "https://contoso.sharepoint.com/sites/finance"), connector("ERP Reports")],
          labels=[("Finance-Invoices", "Highly Confidential")]),
    agent("agent-order-status", "Order Status Agent", 1, ["Website"], "Soy un asistente virtual de pedidos.", [connector("Orders API")], lcid=3082),
    agent("agent-no-owner", "Unowned Prototype", 2, ["MsTeams"], None, owner=None),
    agent("agent-recruiting", "Recruiting Screener", 2, ["MsTeams"], "Hi, I am an AI assistant that helps the recruiting team.",
          [knowledge("Open-Roles", "https://contoso.sharepoint.com/sites/recruiting")],
          instr="Screen candidate CVs and rank candidates for open roles.",
          extra={
              "entra": users(("Ana Ruiz", "owner"), ("Marta Gil", "overseer")),
              "learning": learning("Ana Ruiz", "Marta Gil"),
              "retention": {"purviewAuditDays": 365, "logAnalyticsDays": 365},
              "foundry": {**FOUNDRY, "deployments": [{**FOUNDRY["deployments"][0], "fineTuned": True}], "generatesContent": []},
          }),
    agent("agent-loan-prescreen", "Loan Pre-screening", 2, ["MsTeams"], "Welcome to loan pre-screening.",
          [knowledge("Credit-Policies", "https://contoso.sharepoint.com/sites/credit")],
          instr="Pre-screen loan applications and support credit scoring decisions.",
          extra={
              "entra": users(("Luis Mora", "owner")),
              "foundry": {**FOUNDRY, "contentCredentials": True},
              "defender": {"incidents": [{
                  "id": "INC-200", "title": "Wrong scoring outcome affecting applicants", "severity": "critical", "status": "active",
                  "detectedTime": "2026-11-03T09:30:00Z", "classifiedTime": "2026-11-03T11:00:00Z",
                  "personalDataAffected": False, "seriousIncidentCandidate": True,
                  "description": "A configuration change produced incorrect pre-screening outcomes."}]},
          }),
    agent("agent-citizen-es", "Asistente Ciudadano", 1, ["DirectLine"], "Soy un asistente de IA del ayuntamiento. Puedo informarte de trámites.",
          [knowledge("Tramites", "https://ayto.sharepoint.com/sites/tramites")], lcid=3082, owner="Marta Gil", sponsor="Jorge Vidal"),
]

OUT.mkdir(parents=True, exist_ok=True)
for a in AGENTS:
    (OUT / f"{a['agent_id']}.json").write_text(json.dumps(a, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {len(AGENTS)} agents to {OUT}")
