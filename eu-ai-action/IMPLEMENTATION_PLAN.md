# eu-ai-action: MVP implementation plan

Companion to `MVP_PLAN.md` (scope, go/no-go criteria, commercial assessment). This file is the build plan.
Status: draft, 8 November 2026. Microsoft API details are assumptions until milestone M1 confirms them; they are marked **[verify]**.

## Build status (9 November 2026)

Everything below that is marked "tested" runs offline against fixtures (94 pytest tests, plus a browser test of the portal).
Nothing was run against a Microsoft tenant or Azure.

| Phase | State |
|---|---|
| M0 setup | Scaffold, CI (passing on the PR), settings, 10 fixture agents done. **Not done:** real dev tenant, seeded Copilot Studio agents, Azure deployment. |
| M1 spikes | Scripts written (`spikes/run_spikes.py`). **Deferred by decision** until the tenant spike is scheduled. `docs/spike-results.md` is a blank template. |
| M2 collector and facts | Facts, provenance, disclosure detector, Annex III keyword suggestion tested. Dataverse, Graph and Purview clients, and five more sources (Entra access, retention, Foundry, Defender, learning) written from assumed shapes, **untested live**. |
| M3 rule engine | 30 rules, tested. Missing evidence never silently skips a duty. Counsel review not done (`reviewed_by` empty). |
| M4 documents | 30 document types in ES and EN, DOCX/PDF/MD, versioning, stale detection, workflow, audit, deadline calculation, per-incident and per-request documents: tested. Templates not reviewed by counsel. Azure OpenAI path tested with a fake client only. |
| M5 API, MCP, portal | Handlers, tenant authorisation, auditor read-only role, durable scan, MCP tools, portal (agents, tasks, settings and profile, audit): written and tested offline. Hosted behaviour (Functions host, Easy Auth, MCP) **not run**. |
| v1 | DPIA pack, oversight record, worker and affected-person notices, GDPR breach and AI Act incident drafts, risk classification, literacy record, log-retention attestation, provider status: done. Compliance Manager write-back: export bundle done (Excel fill-in with **unverified column names**). Teams tasks: card builder done, not tested against Teams. Marketplace listing: **not built**. |
| v2 | FRIA, Art. 86 explanations, EU database record, Art. 50(2) and 50(4), DORA incident and register rows, NIS2 asset and incident, ISO 42001 impact and SoA evidence, AESIA notice, ENS pack, Foundry evidence: done as document types and rules. Copilot Studio runtime webhook evidence and the Annex IV exporter: **not built** (see `docs/not-built.md`). |
| High-risk readiness | Auditor role, audit pack, hash verification, evidence bundle: done. ENS certification is a separate process. |
| M6 pilots | **Not started.** Bicep written but not validated. |

Deviations from the plan below: the Python package is `eu_ai_action/` at the project root (not `src/`); HTTP routes are all
under `/api/tenants/{tenant_id}/...`; MCP tools do not expose approval and run for `TARGET_TENANT_ID` only; the Art. 50(1) rule
reports `unknown` when the greeting says only "virtual assistant"; duties whose applicability depends on a fact that could not
be read (public-facing status, synthetic content) apply with an `unknown` result instead of being skipped; risk tier,
sector profile and several scope questions are human inputs, with a keyword-based Annex III suggestion only as a prompt.

## 1. Goal and definition of done

Connect one Microsoft tenant, inventory its Copilot Studio agents, and produce for each agent: an Art. 50(1) disclosure check, a register entry and a GDPR Art. 30 record-of-processing entry, as signed-off, versioned DOCX/PDF documents in Spanish and English, plus a readiness score.

Done when:
- A new tenant connects and a full scan of 8 test agents completes in under 10 minutes.
- At least 80% of the fields in the three documents are filled from API data. The rest appear as named human tasks.
- Every document records the rule-set version, the evidence it used, who approved it and when.
- Rule and template tests pass in CI, and counsel has reviewed the rules and templates.

## 2. Stack decisions

| Area | Choice | Reason |
|---|---|---|
| Language and host | Python 3.11, Azure Functions v2 programming model | Same as the Snippy sample, so its patterns copy over |
| Orchestration | Durable Functions | Fan-out per agent, retries, resumable scans |
| Store | Cosmos DB (NoSQL), partition key `/tenantId` | Document-shaped data; tenant isolation built in from the start |
| Secrets | Key Vault; managed identity for Azure resources | No secrets in app settings |
| Microsoft access | App registration, client-credentials, read-only permissions | Matches the "read-only, metadata only" claim |
| Documents | Jinja2 templates to DOCX (`python-docx` or `docxtpl`), PDF via LibreOffice headless or WeasyPrint | Customers expect Word they can edit |
| LLM | Azure OpenAI, structured output (JSON schema), narrative fields only | Rules decide, the model only drafts text |
| API and tools | HTTP routes plus MCP tool triggers | Same two interfaces Snippy already uses |
| Portal | Azure Static Web Apps, plain TypeScript or HTMX, Entra login | Smallest thing a DPO can click through |
| IaC | `azd` + Bicep, copied from Snippy and trimmed | Repeatable dev and demo environments |
| CI | GitHub Actions: lint (`ruff`), types (`mypy`), `pytest` | Rules and templates must be testable without Microsoft |

## 3. Architecture

```
Microsoft tenant                     eu-ai-action (Azure Functions)                 Output
-----------------                    -------------------------------                ------
Dataverse (bots, botcomponents) --\
Graph (agent registry, Entra) ----+-> collector -> evidence snapshot (raw JSON)
Purview audit / labels ----------/        |
                                          v
                                   fact extractor -> agent facts (typed)
                                          |
                                          v
                                   rule engine (YAML rules) -> duty results
                                          |
                                          v
                                   document engine (templates + LLM narrative)
                                          |
                                          v
                                   review workflow (draft > in_review > approved)
                                          |
                          portal / HTTP API / MCP tools  ->  DOCX / PDF
```

Principles (from `MVP_PLAN.md` section 6): rules not prompts decide applicability; templates first, LLM second; deterministic checks where possible; every output versioned with its rule-set version; metadata only; Microsoft access behind an `EvidenceSource` interface.

## 4. Repository layout

All paths below are inside `eu-ai-action/`. Nothing imports from the Snippy code.

```
eu-ai-action/
  azure.yaml
  infra/                      main.bicep, cosmos, keyvault, function app, static web app, app registration
  src/
    function_app.py           registers triggers only; no logic
    api/                      HTTP routes and MCP tools (thin)
    orchestration/            durable orchestrators and activities
    collector/
      base.py                 EvidenceSource protocol, Evidence dataclass
      dataverse.py            Copilot Studio agents
      graph_registry.py       Agent 365 / Entra agent identity   [verify]
      purview.py              audit, sensitivity labels          [verify]
      fixtures.py             loads recorded JSON for tests and offline demos
    facts/                    evidence -> AgentFacts (pydantic models)
    rules/
      schema.py               pydantic model for a rule
      engine.py               evaluates conditions, returns DutyResult
      content/
        ai_act.yaml           D01, D04 (Art. 50(1))
        gdpr.yaml             R15 (Art. 30)
    documents/
      templates/{es,en}/      *.docx.j2 or docxtpl templates
      narrative.py            LLM calls with JSON schema
      render.py               DOCX and PDF output
    store/                    Cosmos repositories
    review/                   state machine, versioning
  tests/
    fixtures/                 recorded evidence for the 8 test agents
    unit/, golden/, e2e/
  portal/                     static web app
  docs/                       this plan, MVP_PLAN.md, rule authoring guide
```

## 5. Data model

All containers are partitioned by `/tenantId`. Every document has `id`, `tenantId`, `createdAt`, `schemaVersion`.

| Container | Contents |
|---|---|
| `tenants` | Tenant id, display name, consent status, scan settings |
| `scans` | Scan id, started/finished, status, agent count, error list |
| `evidence` | One snapshot per agent per scan: source, endpoint, retrieved-at, raw JSON hash and body |
| `agents` | Current `AgentFacts` per agent, last scan id, history pointer |
| `dutyResults` | Per agent, per rule: status (`applies`, `not_applicable`, `needs_input`), result (`pass`, `fail`, `unknown`), evidence refs, rule id and version |
| `documents` | Document id, type, agent id, language, version, state, field values with provenance, rendered file blob path, rule-set version |
| `tasks` | Human tasks: missing field, review request, assignee, due date |
| `audit` | Append-only record of who approved or edited what |

`AgentFacts` (typed, minimal for the MVP):
`agentId, name, platform, environmentId, owner, sponsor, createdAt, modifiedAt, channels[], isPublicFacing, authMode, greetingText, hasAiDisclosure, knowledgeSources[{type,url,sensitivityLabels[]}], connectors[], dataCategories[], languages[], lastActivity, businessPurpose (human-supplied), intendedUsers (human-supplied)`.

Each fact value stores `{value, source, evidenceId, confidence}` so a document field can always point back to where it came from.

## 6. Rule format

Structured conditions, no `eval`. Example:

```yaml
id: AIA-50-1-disclosure
regulation: EU AI Act
article: "Art. 50(1)"
applies_from: 2026-08-02
role: provider
version: 1
applies_when:
  all:
    - {fact: isPublicFacing, op: eq, value: true}
check:
  pass_when:
    - {fact: hasAiDisclosure, op: eq, value: true}
  unknown_when:
    - {fact: greetingText, op: missing}
evidence: [greetingText, channels]
document: ai-interaction-disclosure
source_citation: "Regulation (EU) 2024/1689, Art. 50(1)"
reviewed_by: ""        # counsel name and date, required before release
```

Operators for the MVP: `eq, ne, in, contains, missing, exists, gt, lt`, combinators `all, any, not`. `hasAiDisclosure` is computed by a small deterministic detector in `facts/` (patterns for ES and EN such as "soy un asistente virtual", "I am an AI"), with the pattern list in a data file and tests. The rule file is loaded at startup and hashed to produce the rule-set version stored with every result.

## 7. Document engine

1. A document type maps to a template and a list of fields. Each field has a source: `fact`, `rule_result`, `llm`, or `human`.
2. `fact` and `rule_result` fields are filled directly with provenance.
3. `llm` fields: one call per document with a JSON schema listing only the narrative fields. The prompt contains the relevant facts only. Output is validated against the schema; a field with no supporting facts must come back as `null` and becomes a human task. Store the prompt hash and model name with the document.
4. `human` fields (business purpose, intended use) become tasks and block approval until filled.
5. Render DOCX in the requested language, then PDF. Stamp the footer with document id, version, rule-set version and "Draft, not legal advice" until approved.
6. Fixed legal wording lives in templates, never in the model output.

Review state machine: `draft -> in_review -> approved -> superseded`, plus `rejected -> draft`. Approval requires all human fields filled and records user, time and a hash of the rendered file. A new scan that changes any fact used by an approved document creates a new draft version and marks the approved one `stale`, not deleted.

## 8. API and MCP surface

| HTTP route | Purpose |
|---|---|
| `POST /api/tenants/{id}/scans` | Start a scan; returns scan id |
| `GET /api/tenants/{id}/agents` | List agents with readiness score |
| `GET /api/agents/{id}` | Facts, duty results, documents |
| `PATCH /api/agents/{id}/inputs` | Supply human fields (purpose, intended users) |
| `POST /api/documents/{id}/generate` | Regenerate a draft |
| `POST /api/documents/{id}/transition` | Submit, approve, reject |
| `GET /api/documents/{id}/file?lang=es&format=pdf` | Download |

MCP tools mirror these: `list_agents`, `get_compliance_file`, `generate_document`, `approve_document`.

Readiness score per agent: share of applicable duties with result `pass` and an approved document, shown as pass / fail / unknown counts, not a single opaque number.

## 9. Infrastructure and security

- Bicep: Function App (Flex or Premium), Storage, Cosmos, Key Vault, Application Insights, Static Web App, Azure OpenAI in an EU region.
- Entra app registration for the collector with the smallest permission set found in M1. Admin consent flow documented as a one-page checklist for the customer.
- Managed identity for Cosmos, Key Vault and storage. No connection strings.
- The collector stores metadata only. Conversation content, message transcripts and file contents are not requested and not stored. Add a unit test that fails if a collector response schema contains a transcript field.
- Customer-facing Entra login on the portal; per-tenant authorisation on every route; no cross-tenant queries (enforced by always passing `tenantId` as the partition key).
- Log every Microsoft call (endpoint, status, latency) without response bodies.

## 10. Milestones

Timeline assumes one full-time developer. Each milestone ends with a demo and a merged PR.

### M0: Setup (week 0 to 1)
- [ ] Written conflict-of-interest clearance recorded in `docs/`.
- [ ] Dev tenant with Copilot Studio; test environment in Dataverse.
- [ ] Create 8 seeded agents: (1) public web chat with disclosure, (2) public web chat without, (3) Teams internal assistant, (4) HR FAQ agent with an HR knowledge source, (5) agent with sensitivity-labelled SharePoint source, (6) agent with an HTTP/connector tool, (7) agent with no owner set, (8) Spanish-language public agent with disclosure in Spanish.
- [ ] Scaffold `eu-ai-action/` with `azd`, CI, lint, test runner, empty function app deployed to a dev subscription.
- Exit: CI green; empty deploy works; agents listed in the Copilot Studio UI.

### M1: Feasibility spikes (week 1 to 2)
Scripts in `eu-ai-action/spikes/`, one per question, each producing a written result in `docs/spike-results.md`.
- [ ] Dataverse: read `bots` and `botcomponents` app-only; locate the Conversation Start topic and its message text; list channels and authentication mode **[verify]**.
- [ ] Graph: agent registry / Entra agent identity endpoints, fields returned, permission names, v1.0 or beta **[verify]**.
- [ ] Purview: audit query for agent activity with app-only identity; sensitivity label lookup for a SharePoint source **[verify]**.
- [ ] Minimum permission set and admin-consent steps.
- [ ] Record each API response as a fixture under `tests/fixtures/`, with tenant-specific ids replaced.
- Exit: result per API (works / limited / not available). If Dataverse cannot be read app-only, stop and re-plan before M2. If the registry or Purview is unavailable, continue with Dataverse-only facts and mark the dependent fields as human input.

### M2: Collector and facts (week 2 to 3)
- [ ] `EvidenceSource` interface, Dataverse source, Graph source, Purview source, fixture source.
- [ ] Retry with backoff, throttling handling, pagination, per-source error isolation (one failing source does not fail the scan).
- [ ] Fact extractors and the AI-disclosure detector, with tests against all 8 fixtures.
- [ ] Cosmos repositories and schema; `scans`, `evidence`, `agents`.
- [ ] Durable orchestrator: `scan_tenant` fans out `collect_agent` per agent, then `extract_facts`.
- Exit: scan of the dev tenant stores facts for all 8 agents; the facts match a hand-checked expected file.

### M3: Rule engine (week 3 to 4)
- [ ] Rule schema, loader, validator, rule-set hash.
- [ ] Engine returning `DutyResult` with evidence refs.
- [ ] Rules: AIA-50-1 disclosure, register entry, GDPR Art. 30 RoPA, each with a citation and counsel review field.
- [ ] Parameterised unit tests: one test per rule per test agent (expected applies / result).
- [ ] Rule authoring guide in `docs/`.
- Exit: results for all 8 agents match the expected table; counsel has received the rule files.

### M4: Documents (week 4 to 6)
- [ ] Templates for the three documents in ES and EN (counsel-supplied wording for fixed text).
- [ ] Narrative generation with JSON-schema output; null handling; prompt and model recorded.
- [ ] DOCX and PDF rendering; footer stamping.
- [ ] Review state machine, versioning, stale detection, audit log.
- [ ] Golden-file tests: render each document for each test agent from fixtures with a stubbed LLM, compare extracted text.
- [ ] LLM regression set: 10 agent descriptions with expected "must not claim" checks (for example, the draft must not state an agent is compliant).
- Exit: all documents generate for all 8 agents in both languages; a non-developer can open and edit the DOCX.

### M5: API, MCP, portal (week 6 to 7)
- [ ] HTTP routes and MCP tools from section 8, with per-tenant authorisation tests.
- [ ] Portal: agent list with readiness, agent page with duty results and documents, input form for human fields, approve / reject buttons, downloads.
- [ ] Task list for missing inputs.
- Exit: the end-to-end path (connect, scan, fill inputs, approve, download) works from the portal without using the API directly.

### M6: Pilot and decision (week 7 to 8)
- [ ] Deploy to a clean subscription from `azd up` in under 30 minutes using the README.
- [ ] Customer-facing one-pager: permissions requested, data read, data not read, where it is stored.
- [ ] Demo to interviewees; 2 to 3 pilots on a real tenant with the customer's admin running the consent step.
- [ ] Collect metrics for the `MVP_PLAN.md` go/no-go table: percentage of fields auto-filled, minutes of edits per document, pilot requests.
- Exit: go/no-go decision recorded.

## 11. Testing strategy

| Layer | Approach |
|---|---|
| Collector | Contract tests against recorded fixtures; a scheduled live smoke test against the dev tenant that alerts when an API response shape changes |
| Facts | Table tests: fixture in, expected facts out, per agent |
| Rules | One parameterised test per rule and agent; a test that every rule has a citation and a reviewer field |
| Documents | Golden-file text comparison per document, language and agent; LLM stubbed |
| LLM output | Schema validation plus a small regression set of forbidden claims |
| Security | Test that no collector model has a transcript or content field; per-tenant authorisation tests on every route |
| End to end | One scripted run: scan fixtures, supply inputs, approve, download |

## 12. Not in the MVP

Multi-tenant self-service onboarding, billing, Marketplace packaging, SSO beyond Entra login on the portal, customer-managed keys, Foundry and toolkit evidence, AI literacy record (D07), risk classification (D02), Art. 50(2) marking, runtime webhook, Compliance Manager export bundle (planned for v1, see `MVP_PLAN.md` section 8a).

## 13. Risks specific to the build

| Risk | Handling |
|---|---|
| Dataverse greeting-topic structure differs between agents (classic vs generative, edited topics) | M1 records variants; detector returns `unknown` rather than a wrong `pass`; unknown becomes a human task |
| Disclosure detector gives false passes | Prefer false `unknown`; keep the pattern list in data with tests; show the matched text in the document |
| LLM states legal conclusions | Templates fix legal text; schema allows narrative only; regression set; footer says draft until approved |
| Preview or beta API changes | `EvidenceSource` abstraction; fixtures; scheduled smoke test |
| Test tenant is not representative | Run the pilots in real tenants before judging data quality |
| Scope creep into later stages | Anything outside section 1 goes to a backlog file; no new document types before M6 |

## 14. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Folder name | `eu-ai-action` (no spaces). Done. |
| 2 | Dev tenant | Available. |
| 3 | Subscription, region and other deployment inputs | All parameterised in `.env.example` (section 15); no values are hard-coded in Bicep, code or CI. |
| 4 | Counsel for rule and template review | Boutique AI/data protection firm or independent privacy lawyer, fixed-fee engagement (section 16). Specific person or firm still to be chosen. |
| 5 | Portal | Build the minimal static portal in M5. |

## 15. Configuration

Every environment-dependent value is read from environment variables. `.env.example` lists them; `.env` is git-ignored. `azd` reads the same names through `azd env set`, Bicep takes them as parameters, and CI takes them from GitHub environment variables and secrets. Code reads settings only through `src/settings.py` (pydantic-settings), which fails fast on a missing required value. Secrets are never in the file; the file holds Key Vault secret names instead.

## 16. Counsel options for rule and template review

| Option | Fit | Trade-off |
|---|---|---|
| Spanish law firm with a technology and data protection practice (for example the large national firms) | Credible with banks and public bodies; can review ES templates and AESIA points | Highest cost; slower; check that the team has AI Act experience |
| Boutique AI or data protection firm | Specialist depth; flexible fixed-scope work | Less brand weight with large customers |
| Independent privacy lawyer or DPO consultant with AI Act experience | Practical for Art. 30 and Art. 50; cheapest and fastest | Single person; may not cover labour law for worker notices |
| Fractional or retained counsel | Review of every rule change over time, which the content-maintenance cost needs | Ongoing cost from the start |
| Design-partner customer's legal team | Free; tests against a real buyer | Not independent and not an endorsement; do not treat as sign-off |

I have not checked any named firm's AI Act experience. Recommended approach: start with a fixed-fee review by an independent privacy lawyer or boutique for the three MVP rules and templates in ES and EN, with written sign-off recorded in each rule's `reviewed_by` field and each template header. Move to a retained arrangement if the MVP passes go/no-go. Ask each candidate for: AI Act and GDPR experience, Spanish-language review, turnaround time, fixed fee, and whether their insurance covers advice used in a product. The product terms of service should state that documents are drafts for human review and not legal advice.
