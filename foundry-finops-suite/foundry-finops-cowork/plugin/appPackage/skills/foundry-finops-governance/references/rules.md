# Guardrail rules (enforced by the service – check before writing)

## Foundry cost controls (preview)
- Name: 1–63 letters, numbers, hyphens or underscores; can't be renamed.
- Budget type (`Cost` in USD or `Tokens`) can't be changed after creation – create a new control instead.
- Token limits are whole numbers; `tokenType` filters (input/output) only apply to Tokens budgets.
- An `agent` filter needs a `project` filter with exactly one project.
- Up to 20 hosted-agent session IDs per control.
- Per-project budgets (`sharing=project`) only on the Foundry resource's controls, not inside a project.
- Alert threshold must be at or below the block threshold. Alerts need the Event Grid connection ID Foundry
  created (reuse one from an existing control); connections you create yourself are silently skipped.
- Changing the reset period or how the budget is shared restarts the usage count.
- Every control has an end date; extend it before it passes.
- Not counted: provisioned (PTU) deployments, non-OpenAI models, agent tool charges, hosted-agent compute.
- A project's own controls count but don't block chat completions sent to the project endpoint; use a
  resource-level control to block those.

## Cost Management budgets
- Start date is the first of a month; up to 5 Actual and 5 Forecasted alert conditions.
- Alerts can email recipients or call action groups; budgets never block.

## API Management token limits
- Either tokens-per-minute, a quota with a period (Hourly/Daily/Weekly/Monthly/Yearly), or both.
- One counter per counter-key value across all scopes that use it; use different keys per scope for separate counters.
- Prompt tokens are always estimated for streaming requests.

## Deployment capacity
- Sum of capacities per model and deployment type can't exceed the regional quota.
