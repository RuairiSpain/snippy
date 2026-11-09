# Not built, and why

| Item | Status | Reason |
|---|---|---|
| Live collectors verified against a tenant | Written, never run | Needs tenant credentials. Scheduled for later (`spikes/run_spikes.py`). Response shapes in `facts/extract.py` and the fixtures are assumptions. |
| Compliance Manager write-back through an API | Not possible as far as found | No public API found; the product exports an Excel fill-in plus evidence files. The mapped column names and status values in `export/cm_mapping.yaml` are unverified. |
| Annex IV technical documentation pack | Not built, by decision | The competitive analysis rates it "Overlap": use the Agent Governance Toolkit exporter instead. |
| Copilot Studio pre-tool runtime webhook evidence | Not built | Preview API, generative-orchestration agents only. Needed for Art. 86 explanations from real interaction data; today a person copies the inputs in. |
| Microsoft Marketplace listing (SaaS offer, managed app for the collector, private offers) | Not built | Needs a Partner Center account, a landing page and webhook, certification, and a deployed service. |
| Billing and metering, plan limits | Not built | Pricing is still an assumption to validate with customers. |
| SSO beyond Entra sign-in; customer-managed keys; dedicated EU hosting per customer | Not built | Regulated-tier items. Cosmos and Storage in the Bicep use platform-managed keys. |
| ISO 27001 or SOC 2 for the vendor, penetration test | Not started | Organisational work, 9 to 12 months for certification. |
| Legal review of rules and templates | Not done | Every rule has an empty `reviewed_by` and every template an empty `template_reviewed_by`. With the default setting, approval is blocked until both are filled. |
| Deadline rules (DORA 4h/24h/72h, NIS2 24h/72h, GDPR 72h, AI Act 15 days) | Implemented as computed fields | The clock start (awareness, classification) is approximated by incident timestamps and "1 month" by 30 days. Counsel must confirm each, and DORA and NIS2 against the national authority's current requirements. |
| Official AESIA form | Not built | The layout is not an official form; check the national format and channel with AESIA. |
| Azure deployment (Bicep) | Written, not validated | No Bicep or Azure CLI available when written. Key Vault role id is a parameter to look up. |
