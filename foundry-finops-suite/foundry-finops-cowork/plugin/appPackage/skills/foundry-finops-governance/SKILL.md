---
name: foundry-finops-governance
description: "Safely create, change or delete Microsoft Foundry spend guardrails through the Foundry FinOps connector: Foundry cost controls (alert or block at a spending or token budget), Azure Cost Management budgets, resource tags for team chargeback, deployment capacity (TPM), and API Management gateway token limits. Use for 'cap spending for project X', 'block the agent at $500 a month', 'create a budget for the support team', 'tag the project with team=Risk', 'raise capacity on gpt-5.4-mini', 'limit team Y to 50k tokens a minute'. Always shows the current state and the exact change, and asks the user to confirm before writing."
---

# Foundry FinOps governance

Change spend guardrails with the **Foundry FinOps** connector. Writes run with the signed-in user's Azure
permissions; the host also asks for confirmation because these tools are marked destructive.

## Choose the right guardrail

| Goal | Tool | Notes |
|---|---|---|
| Stop or warn as requests run, per project/agent/deployment/session | `upsert_cost_control` | Preview. GPT and o-series models only; acts within ~15 min. Blocks return 429 + `x-ms-budget-cause: budget_exceeded`. |
| Monthly/quarterly alert on the bill | `upsert_budget` | Alerts only, 8–24 h behind usage. Can't block. |
| Per-team or per-app tokens per minute / quota | `set_gateway_token_limit` | Needs Azure API Management in front of Foundry. 429 over TPM, 403 over quota. |
| Throughput for one deployment | `set_deployment_capacity` | Bounded by regional quota. |
| Chargeback by team | `set_tags` | Replaces all tags on the scope – read them first. |

## Mandatory workflow for every write

1. **Read first**: call the matching list tool (`list_cost_controls`, `list_budgets`, `list_tags`,
   `get_quota_and_deployments`, `get_gateway_overview`).
2. **Show the plan**: one short table – item, current value, new value – plus what users will notice
   (for example "requests from agent X fail with HTTP 429 once $500 is reached this month").
3. **Check the rules** in `references/rules.md`; fix the request or explain what isn't possible.
4. **Ask once** for confirmation with the exact change. Don't write until the user agrees.
5. **Write**, then **re-read** and report the stored result and when it takes effect.

## Rules

- Never delete or replace something the user didn't name. For `set_tags`, merge the user's change into the
  current tags and send the full set.
- Don't loosen or remove a Block threshold without saying it re-enables spending.
- If a write fails with an access error, report the role needed (Foundry Owner/Account Owner or Project Manager for
  cost controls; Cost Management Contributor for budgets; Contributor on the APIM instance for token limits).
- Never place secrets, keys or tokens in arguments, chat or files.
