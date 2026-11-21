# KQL cookbook

Call `list_log_tables_and_queries` first – it returns the live tables and tested examples for this server.

Application Insights (Foundry tracing, gen_ai semantic conventions):

```kql
dependencies
| where isnotempty(customDimensions["gen_ai.usage.input_tokens"])
| extend model = tostring(customDimensions["gen_ai.request.model"]),
         tokens = toint(customDimensions["gen_ai.usage.input_tokens"]) + toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize tokens = sum(tokens), calls = count() by model, bin(timestamp, 1d)
```

Usage records written by foundry-finops `UsageTracker` (team / cost-centre attribution):

```kql
traces
| where message == "llm.usage"
| summarize tokens = sum(toint(customDimensions["total_tokens"])), cost = sum(todouble(customDimensions["cost"]))
          by team = tostring(customDimensions["finops.team"]), bin(timestamp, 1d)
```

API Management `llm-emit-token-metric` (per gateway subscription / team):

```kql
customMetrics
| where name in ("Prompt Tokens", "Completion Tokens", "Total Tokens")
| summarize tokens = sum(valueSum) by name, team = tostring(customDimensions["Team"]), bin(timestamp, 1h)
```
