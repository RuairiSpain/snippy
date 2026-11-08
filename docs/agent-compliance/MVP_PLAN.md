# Agent Compliance Files: MVP plan, build complexity and commercial assessment

Status: draft for discussion, 8 November 2026. Not legal advice.
Inputs: Product Brief (docx), Competitive Analysis (xlsx), Pitch Deck (pptx).
Everything about Microsoft APIs below is an assumption until the week-1 spikes confirm it.

## 1. What is being built

A service that reads the evidence Microsoft already produces for each agent (Copilot Studio, M365 agents, Foundry), decides which duties apply to that agent (EU AI Act, GDPR, later DORA/NIS2/ISO 42001), drafts the documents those duties require, routes judgement calls to a person, and exports the result. The brief's five-step loop is: read, decide applicability, generate, route to humans, write back.

## 2. Gating items before any build

1. **Conflict-of-interest clearance.** The brief says the founder is a Microsoft employee and requires written clearance before building. Treat this as step zero. Building in this repository before clearance contradicts the brief's own mitigation.
2. **Discovery interviews run in parallel with the build.** The brief plans 10 DPO/compliance interviews. The MVP is only useful for the go/no-go decision if those interviews happen while it is built, so the demo can be shown to them.
3. **Counsel review of the rule set.** The MVP states legal conclusions (applicability, Art. 50 compliance). A lawyer reviews the rule definitions and templates before any demo to a prospect.

## 3. Inconsistencies in the source files to resolve first

These change what the MVP contains, so settle them before scoping.

| Topic | Brief | Evidence-to-Documents sheet | Regulations Matrix sheet |
|---|---|---|---|
| Plan names | Team / Business / Regulated | Community / Essentials / Professional / Enterprise | Team / Business / Regulated |
| Risk classification | not in MVP list | MVP | MVP |
| Log-retention attestation (Art. 26(6)) | not in MVP list | MVP | v1 |
| Serious incident report (Art. 73) | v1 (incident drafts) and v2 (listed with FRIA) | v2 | v1 |
| FRIA (Art. 27) | v2 | v1 | v2 |
| Affected-person notice (Art. 26(11)) | not stated | v1 | v2 |
| Art. 50(2) marking | in MVP scope as "Art. 50" | MVP, applies 2 Dec 2026 | not listed |

Also verify with counsel: whether Art. 4 (AI literacy) still binds deployers directly after Regulation (EU) 2026/1744, since the brief and sheets treat it as an MVP duty "now".

## 4. Recommended MVP scope

The brief's MVP is: collector (Agent 365, Copilot Studio, Purview), per-agent file, Art. 4 and 50, register entry, GDPR RoPA entry, Spanish and English templates. For the purpose of deciding whether to continue, I recommend a thinner slice than that, built in a single dev tenant, with no Marketplace packaging and no write-back.

**In scope**

| # | Capability | Source of truth | Why it is in |
|---|---|---|---|
| 1 | Inventory of Copilot Studio agents in one tenant | Dataverse `bot` and `botcomponent` tables via Power Platform/Dataverse API; Agent 365 registry and Entra Agent ID via Graph if available | Foundation for everything else |
| 2 | Per-agent facts: owner, channels, public-facing flag, knowledge sources, connectors, greeting text, data sensitivity | Same, plus Purview sensitivity labels | Inputs to rules |
| 3 | Applicability engine (rules as versioned YAML) | Own content | Core asset in the brief |
| 4 | D04 Art. 50(1) disclosure check, with suggested ES/EN wording | Greeting topic and channel config | Duty is live today; fully automatable; clearest "Gap" in the competitive sheet |
| 5 | D01 register entry | Registry metadata plus owner-supplied business purpose | Prerequisite record |
| 6 | R15 GDPR RoPA entry | Knowledge sources, labels, Purview DSPM for AI if available | Duty is live today |
| 7 | Readiness score per agent (the free "Scan" tier) | Rule results | This is the top-of-funnel product in the pricing model |
| 8 | Human sign-off state machine: draft, in review, approved, superseded, with version history | Own | Brief requires human sign-off on every document |
| 9 | DOCX/PDF export, ES and EN | Templates | What the DPO actually receives |

**Deferred from the brief's MVP**: D07 AI literacy record (needs a learning-platform integration, which the brief does not specify), D02 risk classification (judgement-heavy; do it as a rule-suggested draft only after the first six work), Art. 50(2) marking (needs Foundry, not Copilot Studio), write-back, Marketplace, auditor portal.

**Not built in the MVP**: multi-tenant onboarding, billing, SSO, customer-managed keys, the Copilot Studio runtime webhook.

## 5. Reuse from this repository

Snippy is an Azure Functions (Python) app with Durable Functions, Cosmos DB, Azure OpenAI, MCP tool triggers and `azd` + Bicep infrastructure. That is close to the platform layer the brief describes.

| Brief component | Snippy asset | Work needed |
|---|---|---|
| Per-agent compliance file store | `src/data/cosmos_ops.py`, `infra/app/cosmos-db.bicep` | New containers: `agents`, `facts`, `documents`, `tasks`. Partition by `tenantId` from day one even with one tenant. |
| Pipeline per agent | Durable orchestration patterns in `src/durable_agents.py` | New orchestration: collect, derive facts, evaluate rules, generate, store. |
| AI drafting | Azure OpenAI wiring in `function_app.py` | Replace embeddings usage with structured-output drafting. |
| API and MCP server | `@app.route` and `@app.mcp_tool_trigger` in `src/function_app.py` | New tools: `list_agents`, `get_compliance_file`, `generate_document`, `approve_document`. |
| Infra and deployment | `infra/main.bicep`, `azure.yaml`, `infra/app/app-registration.bicep` | Add Key Vault, an Entra app registration with read-only Graph/Dataverse permissions, Static Web App for the portal. |

The snippet-specific code (`tools/vector_search.py`, snippet routes) should be removed or left untouched in a separate path. Suggested new layout: `src/collector/`, `src/rules/` (YAML), `src/engine/` (applicability, facts), `src/documents/` (Jinja2 templates in `es/` and `en/`, renderers), `src/api/`.

## 6. Design decisions for the MVP

1. **Rules, not prompts, decide applicability.** Each duty is a YAML record: id, regulation, article, applies-from date, `applies_when` expression over agent facts, required evidence, template id, source citation. The LLM never decides whether a duty applies.
2. **Templates first, LLM second.** A document is a Jinja2 template with fixed legal text. The LLM fills only narrative fields (for example a plain-language description of what the agent does), returns structured output, and each field carries a pointer to the evidence it came from. Fields with no evidence stay blank and appear as human tasks.
3. **Deterministic checks where possible.** The Art. 50(1) check is a parse of the greeting topic and channel settings, not an LLM judgement. Use the LLM only to suggest replacement wording.
4. **Every output is versioned and carries its rule-set version**, so a document can be reproduced and a rule change can flag stale documents.
5. **Metadata only.** The collector reads configuration and audit metadata, not conversation content. This keeps the GDPR processor exposure low in the MVP and matches the brief.
6. **Abstraction over Microsoft APIs** (`EvidenceSource` interface). Preview APIs will change; the brief already lists this as a risk.

## 7. Work plan and timeline

Estimate: about 8 weeks for one full-time developer, or the brief's 4 months part-time. These are my estimates, not data from the files.

| Week | Work | Exit criterion |
|---|---|---|
| 0 | COI clearance; dev tenant with Copilot Studio; 5 to 8 seeded test agents covering: public-facing with disclosure, public-facing without, HR purpose, sensitive-label knowledge source, internal-only | Tenant ready, clearance in writing |
| 1 | **API feasibility spikes** (see section 8) | Written result per API: works / works with limits / not available |
| 2 to 3 | Collector and fact model; Cosmos schema; Entra app and permissions | Facts for all test agents stored |
| 3 to 5 | Rules YAML for D01, D04, R15; applicability engine; document templates ES/EN; DOCX/PDF render | Correct output for all test agents; unit tests per rule |
| 5 to 6 | LLM drafting for narrative fields; sign-off state machine; MCP tools; minimal portal | End-to-end run in under 10 minutes from tenant connect |
| 6 to 8 | Counsel review; demo to interviewees; 2 to 3 pilots in the dev-tenant-equivalent of a prospect | Go/no-go data collected |

## 8. Week-1 feasibility spikes (highest technical risk)

The whole MVP depends on being able to read these things programmatically. Check them before writing any other code.

1. Is there a supported API for the Agent 365 registry, and does it return owner, sponsor, platform and last activity? (The brief names Graph; confirm endpoint, permission, GA or beta.)
2. Can Copilot Studio agent definitions, including greeting topic text and channel configuration, be read through Dataverse with an app-only token? Which roles are required?
3. Can Purview audit records for agents be queried with an app-only identity, and at what latency?
4. What does Purview DSPM for AI expose through an API, if anything? If nothing, RoPA data categories come from sensitivity labels and knowledge-source metadata only.
5. Does Compliance Manager have any public API to upload evidence or update improvement actions? I am not aware of one. If not, the "write back" step in the brief becomes file export or a SharePoint/Purview upload workaround, which changes the v1 estimate.
6. Admin consent and least-privilege permission set a customer security team will accept.

## 9. Go/no-go criteria for the MVP

Define these before building so the result is not rationalised afterwards.

| Area | Continue if | Stop or re-scope if |
|---|---|---|
| Technical | At least 80% of fields in the three documents are filled from APIs without manual entry | Registry or Copilot Studio data cannot be read app-only |
| Quality | A DPO or lawyer rates the RoPA entry and Art. 50 check usable with under 15 minutes of edits | Edits take longer than writing from scratch |
| Demand | At least 3 of 10 interviewees ask to pilot; at least 1 will sign a paid pilot or letter of intent | Interviewees see it as a feature of Purview or Compliance Manager |
| Gap | Demos of Veritome, Rencore and AvePoint confirm the "Not found" items | A competitor already generates Art. 50 checks from Microsoft telemetry |

## 10. Complexity of the later stages

Sizes: S under 1 person-month, M 1 to 3, L 3 to 6, XL over 6. Estimates are mine and exclude legal review cost.

| Stage (brief timing) | Items | Engineering size | Main difficulty |
|---|---|---|---|
| v1 (months 5 to 9) | DPIA pack, oversight record, worker notice, AI Act and GDPR incident drafts | M each, L together | Legal content per language, and works-council rules that differ by country |
| v1 | Compliance Manager write-back | S to XL, unknown | Depends on spike 5. If no API exists this is a workaround, not an integration. Vanta and OneTrust APIs exist but each is a separate integration. |
| v1 | Multi-tenant SaaS, Marketplace listing (managed app for collector, SaaS offer, private offers) | L | Marketplace certification, tenant isolation, billing/metering, customer onboarding with admin consent |
| v1 | Teams tasks for human routing | M | Bot or Adaptive Card flows, identity mapping |
| v2 (months 10 to 15) | Foundry and Agent Governance Toolkit evidence | M to L | Public-preview APIs with expected breaking changes |
| v2 | FRIA | L | Rights-impact judgement is human; the product value is structure and evidence |
| v2 | Art. 86 explanations | L to XL | Needs interaction-level data (chat history, tool outputs), which moves the product into processor territory under GDPR and needs the Copilot Studio webhook (preview) |
| v2 | DORA and NIS2 incident packs, DORA Register of Information rows | L each | Per-country transposition and regulator formats; incident clocks of 4h/24h make reliability a product requirement |
| v2 | ISO 42001 evidence, Spain AESIA and ENS | M each | Mostly content; ENS certification is a separate cost and timeline |
| High-risk readiness (before 2 Dec 2027) | Full deployer set, auditor portal | L | Auditor-facing access control and immutable evidence history |

Cross-cutting cost that grows with each stage is **regulation content maintenance**: rule updates, template updates in each language, and counsel review. This is the brief's stated moat, and it is also a recurring operating cost that scales with the number of regulations and countries, not with the number of customers.

## 11. SaaS or PaaS for CISO/CIO and compliance buyers

**Recommendation: SaaS first, with an API-first internal design. Do not build a PaaS yet.**

- The brief's buyer is the DPO, Head of Compliance or Legal, and the AI centre of excellence lead. CISOs and CIOs are approvers: they decide whether a vendor may read their tenant. Their questions are about the vendor, not the documents.
- The "PaaS" in the brief is the connector licence (selling the Microsoft evidence layer to Veritome, OneTrust, Vanta). That is a separate business that needs a partner asking for it. Keep the collector and fact model behind a clean API (and the MCP server) so this option stays open, and revisit after one partner conversation.

What the CISO/CIO gate requires, and when to build it:

| Requirement | When |
|---|---|
| Read-only, metadata-only collector with a documented permission list | MVP |
| EU data residency, DPA, sub-processor list | Before first paid pilot |
| Entra SSO and role-based access in the portal | v1 |
| Audit log of every action in the product | v1 |
| Customer-managed keys, dedicated EU hosting | Regulated tier (v2) |
| Independent pen test | Before Marketplace listing |
| ISO 27001 or SOC 2 for the vendor | Start in v1; certification typically takes 9 to 12 months, so plan it as a critical-path item for large banks and public bodies |

## 12. Commercial assessment

This is my reading of the three files, not a conclusion the data forces.

**What supports continuing**
- The competitive sheet finds Art. 50 checks, Art. 26 notices, Art. 86 explanations and FRIA generation from Microsoft evidence as "Gap" against Microsoft, the toolkit, AvePoint and Rencore.
- Art. 50 and GDPR duties apply now, so there is something to sell before the December 2027 high-risk date.
- The first MVP slice is cheap to build on this repository and answers the key question (can the evidence be read and turned into a usable document) in about two months.

**What weakens the case**
- **Revenue size.** With the pricing sheet's own assumptions (30 Team, 15 Business, 6 Regulated customers in year 2), annual revenue is 30 x 4,680 + 15 x 11,880 + 6 x 30,000 = about €499k from 51 customers. That supports a small team, not a venture-scale company, unless partner or connector revenue is added. The exit section relies on acquirers rather than on this revenue.
- **The MVP documents are the lowest-value ones.** The Art. 50 check and RoPA entry are commodity-level and mostly configuration checks. The documents customers will pay most for (FRIA, notices, Art. 86, DORA/NIS2 incident packs) are v1/v2 and mostly apply from December 2027.
- **Most agents are not high-risk.** For most enterprise agents the applicability engine will return "limited risk: Art. 4 and 50 only". Demand before 2027 depends on how many customers have HR, credit, insurance or public-service agents. The interviews should ask for this count.
- **"Not found" is not "absent".** The competitive analysis says it covers public material only. Rencore is marked "Unverified" on several duties and is described as the closest competitor.
- **Platform dependency.** The core value depends on Microsoft APIs that may be preview, may change, or may be replaced by Microsoft document generation in Purview or Agent 365.
- **Sales cycle.** Target customers are Spanish banks, insurers and public bodies. Procurement plus the security review in section 11 is likely long; the Marketplace and partner channel in the brief addresses this but adds revenue share.

**Decision rule.** Run the MVP as an 8-week test with the section 9 criteria. If the technical and quality criteria pass but demand is below threshold, the connector-licence or partner route is the fallback to test, not a larger build.
