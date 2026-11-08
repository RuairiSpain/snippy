"""M1 feasibility spikes. Run against the real dev tenant with .env filled in:

    python spikes/run_spikes.py            # runs all checks, prints a table, writes spikes/output/*.json
    python spikes/run_spikes.py --record   # additionally writes scrubbed fixtures to tests/fixtures/recorded/

Each check reports: works / works with limits / not available, and why. Copy the table into docs/spike-results.md.
Nothing here writes to the tenant. Output files contain tenant metadata: they are git-ignored; review before sharing."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eu_ai_action.collector.auth import token_provider  # noqa: E402
from eu_ai_action.collector.dataverse import DataverseSource  # noqa: E402
from eu_ai_action.collector.graph_registry import GraphRegistrySource  # noqa: E402
from eu_ai_action.collector.purview import PurviewSource  # noqa: E402
from eu_ai_action.facts import extract_facts  # noqa: E402
from eu_ai_action.settings import get_settings  # noqa: E402

OUT = Path(__file__).resolve().parent / "output"
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str) -> None:
    results.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")


def scrub(obj, mapping: dict[str, str]):
    text = json.dumps(obj)
    for g in set(GUID.findall(text)):
        mapping.setdefault(g.lower(), f"00000000-0000-0000-0000-{len(mapping):012d}")
        text = text.replace(g, mapping[g.lower()])
    return json.loads(text)


def main() -> None:
    s = get_settings()
    OUT.mkdir(exist_ok=True)
    tenant = s.target_tenant_id
    dv = DataverseSource(s.dataverse_environment_url, s.dataverse_api_version, token_provider(s, f"{s.dataverse_environment_url.rstrip('/')}/.default"))
    graph_tok = token_provider(s, f"{s.graph_base_url.rstrip('/')}/.default")
    reg = GraphRegistrySource(s.graph_base_url, s.graph_api_version, s.graph_registry_path, graph_tok)
    pv = PurviewSource(s.graph_base_url, s.graph_api_version, s.purview_audit_path, graph_tok)

    # 1. Dataverse list and read, app-only
    try:
        agents = dv.list_agents(tenant)
        record("dataverse: list bots (app-only)", "works", f"{len(agents)} agents")
    except Exception as e:  # noqa: BLE001
        record("dataverse: list bots (app-only)", "not available", f"{type(e).__name__}: {e}. STOP and re-plan before M2.")
        return summary()
    evidence: dict[str, list] = {}
    for a in agents:
        ev = dv.collect(tenant, a, "spike")
        evidence[a.agent_id] = ev
        (OUT / f"dataverse-{a.agent_id}.json").write_text(json.dumps(ev[0].payload, indent=2))
    # 2. greeting topic and disclosure facts
    found = 0
    for a in agents:
        f = extract_facts(tenant, a, evidence[a.agent_id], "spike")
        found += 1 if f.value("greetingText") else 0
    record("dataverse: greeting topic text", "works" if found == len(agents) else "works with limits",
           f"greeting found for {found} of {len(agents)} agents; inspect spikes/output/ for the topic structure of the others")
    # 3. channels
    ch = sum(1 for a in agents if evidence[a.agent_id][0].payload.get("channels"))
    record("dataverse: channels and authentication mode", "works with limits" if not ch else "works",
           f"channels collected for {ch} agents; authenticationmode comes from the bot row. Channel source is still unconfirmed.")
    # 4. registry
    if not s.graph_registry_path:
        record("graph: agent registry", "not tested", "GRAPH_REGISTRY_PATH is empty: set it to the endpoint from Microsoft Learn")
    else:
        try:
            got = [reg.collect(tenant, a, "spike") for a in agents[:3]]
            record(
                "graph: agent registry", "works" if all(got) else "works with limits", f"{sum(1 for g in got if g)} of {len(got)} sampled agents returned data"
            )
            for g in got:
                if g:
                    (OUT / f"registry-{g[0].agent_id}.json").write_text(json.dumps(g[0].payload, indent=2))
        except Exception as e:  # noqa: BLE001
            record("graph: agent registry", "not available", f"{type(e).__name__}: {e}")
    # 5. purview
    if not s.purview_audit_path:
        record("purview: audit and labels", "not tested", "PURVIEW_AUDIT_PATH is empty: set it to the endpoint from Microsoft Learn")
    else:
        try:
            got = [pv.collect(tenant, a, "spike") for a in agents[:3]]
            n = sum(1 for g in got if g)
            record("purview: audit and labels", "works" if all(got) else "works with limits", f"{n} of {len(got)} sampled agents returned data")
        except Exception as e:  # noqa: BLE001
            record("purview: audit and labels", "not available", f"{type(e).__name__}: {e}")
    record("compliance manager: write-back API", "manual check", "Search the Microsoft Graph reference; see MVP_PLAN.md section 8a")
    if "--record" in sys.argv:
        rec = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "recorded" / "agents"
        rec.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, str] = {}
        for i, a in enumerate(agents):
            d = {"agent_id": f"recorded-{i}", "name": f"Recorded agent {i}"}
            for e in evidence[a.agent_id]:
                d[e.source] = e.payload
            (rec / f"recorded-{i}.json").write_text(json.dumps(scrub(d, mapping), indent=2, ensure_ascii=False))
        print(f"recorded {len(agents)} scrubbed fixtures to {rec}. Review them for names and emails before committing.")
    summary()


def summary() -> None:
    print("\n| Check | Result | Detail |\n|---|---|---|")
    for r in results:
        print("| " + " | ".join(r) + " |")


if __name__ == "__main__":
    main()
