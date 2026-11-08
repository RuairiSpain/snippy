# EU-AI Action

Reads the evidence Microsoft already holds about each Copilot Studio agent, decides which duties apply, and drafts the
documents those duties need (EU AI Act Art. 50(1) disclosure check, register entry, GDPR Art. 30 record of processing)
in Spanish and English, with a human review and approval step. Drafts only; not legal advice.

Plans: `MVP_PLAN.md` (scope and commercial assessment), `IMPLEMENTATION_PLAN.md` (build plan and status).

## Run locally (no Azure, no Microsoft tenant)

```bash
cd eu-ai-action
pip install -r requirements-dev.txt
pytest -q                       # 59 tests
python scripts/dev_server.py    # portal on http://127.0.0.1:8080/?tenant=00000000-0000-0000-0000-000000000001
```

The dev server replays 8 hand-written agents from `tests/fixtures/agents/` (regenerate with `python scripts/make_fixtures.py`).
Click "Scan tenant", open an agent, fill the inputs, then submit and approve a document.
Browser test: `node tests/e2e/portal_smoke.js http://127.0.0.1:8080` against a freshly started dev server.

## Configuration

Copy `.env.example` to `.env`. Every environment-dependent value is read through `eu_ai_action/settings.py`.
`REQUIRE_COUNSEL_REVIEW_FOR_RELEASE` defaults to `true`: documents cannot be approved until counsel has signed off the rules
(`reviewed_by` in `eu_ai_action/rules/content/*.yaml`) and the templates (`template_reviewed_by` in `documents/content/types.yaml`).
The local dev server turns it off.

## Layout

| Path | Purpose |
|---|---|
| `eu_ai_action/collector/` | Evidence sources: Dataverse, Graph registry, Purview, fixtures. Metadata only |
| `eu_ai_action/facts/` | Evidence to typed facts with provenance; AI-disclosure detector |
| `eu_ai_action/rules/` | YAML rules and the engine (no `eval`, no model calls) |
| `eu_ai_action/documents/` | Types, templates (`templates/{en,es}`), narrative, DOCX/PDF render |
| `eu_ai_action/review/` | draft, in_review, approved, rejected, superseded |
| `eu_ai_action/services/pipeline.py` | Scan, rules, documents, tasks, readiness |
| `eu_ai_action/api/` | Framework-free handlers and the tenant authorisation check |
| `function_app.py` | Azure Functions: HTTP routes, durable scan, MCP tools |
| `portal/` | Static web app |
| `infra/` | Bicep for `azd` (not validated, see status) |
| `spikes/` | Live feasibility checks for M1 |

## Status

Verified by tests in this repository: facts, disclosure detector, rules, documents (DOCX/PDF/Markdown, ES and EN),
versioning, review workflow, API authorisation, the LLM wrapper against a fake client, and the portal in a real browser.

Not verified, because it needs a Microsoft tenant or Azure: the Dataverse, Graph and Purview collectors, the Cosmos and Blob
stores, the Azure OpenAI call, the Durable Functions and MCP wiring when hosted, Easy Auth, and the Bicep. See `docs/spike-results.md`.
