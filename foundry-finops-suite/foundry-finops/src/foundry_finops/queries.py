"""Ready-made KQL for Application Insights / Log Analytics.

Tables follow the Application Insights (classic) names; for workspace-based queries replace
``traces`` → ``AppTraces``, ``dependencies`` → ``AppDependencies`` and ``customDimensions`` →
``Properties``.
"""

#: Token usage per model from Foundry GenAI tracing spans (gen_ai semantic conventions).
TOKENS_BY_MODEL = """
dependencies
| where isnotempty(customDimensions["gen_ai.usage.input_tokens"])
| extend model = tostring(customDimensions["gen_ai.request.model"]),
         input_tokens = toint(customDimensions["gen_ai.usage.input_tokens"]),
         output_tokens = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize input_tokens = sum(input_tokens), output_tokens = sum(output_tokens), calls = count()
          by model, bin(timestamp, 1h)
| order by timestamp asc
"""

#: Tokens and estimated cost per agent (Foundry sets gen_ai.agent.name on agent spans).
TOKENS_BY_AGENT = """
dependencies
| where isnotempty(customDimensions["gen_ai.usage.input_tokens"])
| extend agent = tostring(customDimensions["gen_ai.agent.name"]),
         tokens = toint(customDimensions["gen_ai.usage.input_tokens"]) + toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize tokens = sum(tokens), calls = count() by agent, bin(timestamp, 1d)
"""

#: Records written by foundry_finops.tracing.UsageTracker.
TOKENS_BY_TEAM = """
traces
| where message == "llm.usage"
| extend team = tostring(customDimensions["finops.team"]),
         model = tostring(customDimensions["model"]),
         tokens = toint(customDimensions["total_tokens"]),
         cost = todouble(customDimensions["cost"])
| summarize tokens = sum(tokens), cost = sum(cost), calls = count() by team, model, bin(timestamp, 1d)
"""

#: Token metrics emitted by APIM <llm-emit-token-metric> (customMetrics).
APIM_TOKENS_BY_DIMENSION = """
customMetrics
| where name in ("Prompt Tokens", "Completion Tokens", "Total Tokens")
| extend team = tostring(customDimensions["Team"]), subscription = tostring(customDimensions["Subscription ID"])
| summarize tokens = sum(valueSum) by name, team, subscription, bin(timestamp, 1h)
"""

#: Requests blocked by Foundry cost controls, from client-side dependency telemetry.
BUDGET_BLOCKS = """
dependencies
| where resultCode == "429"
| where customDimensions has "budget_exceeded" or tostring(customDimensions["error.type"]) has "Budget"
| summarize blocked = count() by target, bin(timestamp, 1h)
"""

ALL = {
    "Tokens by model (tracing)": TOKENS_BY_MODEL,
    "Tokens by agent (tracing)": TOKENS_BY_AGENT,
    "Tokens and cost by team (UsageTracker)": TOKENS_BY_TEAM,
    "APIM token metrics by team": APIM_TOKENS_BY_DIMENSION,
    "Cost-control blocks": BUDGET_BLOCKS,
}
