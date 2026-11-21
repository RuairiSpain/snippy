# Foundry FinOps dashboard (example)

A zero-dependency web app (Python standard library server + vanilla JS + Chart.js) on top of
`foundry_finops.service`. The look follows the Azure portal and Log Analytics.

```bash
python server.py            # demo data on http://127.0.0.1:8765 – no Azure needed
```

Live mode signs in with `DefaultAzureCredential` (for example `az login`):

```bash
export FINOPS_SUBSCRIPTION_ID=<sub> FINOPS_RESOURCE_GROUP=rg-ai FINOPS_FOUNDRY_ACCOUNT=contoso-foundry
export FINOPS_APIM_SERVICE=contoso-ai-gateway             # optional – AI gateway page
export FINOPS_LOGS_TARGET=<app-insights-resource-id>      # optional – Logs page
export FINOPS_LOCATION=eastus2 FINOPS_TEAM_TAG=team       # optional
export FINOPS_READ_ONLY=1                                 # optional – hide all writes
python server.py
```

## Pages

| Page | What you can do |
|---|---|
| Overview | KPIs (spend MTD, forecast, tokens 24 h, cost controls needing attention, quota), 4 charts, status grids |
| Logs | Log Analytics–style KQL editor: query tabs, highlighting, line numbers, Tables/Queries pane, saved queries, time range, Results grid and Chart tab with chart formatting (type, X, Y, split, aggregation), CSV/JSON/PNG export. Shift+Enter runs. |
| Metrics | Tokens / requests by deployment or model (plus project, agent, team in demo) as line, area, column or donut |
| Cost analysis | Billed cost grouped by project, agent, deployment, model or team; summary and daily detail |
| Cost controls | Search, create, edit, delete Foundry cost controls; usage bars and state |
| Budgets | Search, create, edit, delete Cost Management budgets; spend vs forecast vs budget chart |
| Alerts | Metric alerts (create/edit/delete), log alerts (open in Logs, delete), action groups |
| Tags | Tag inventory and tag summary; edit or clear tags on any scope |
| Quota & deployments | Regional quota chart, deployments, change capacity |
| AI gateway | APIM products and subscriptions (CRUD) and llm-token-limit policies (set, edit, remove) |

Every grid has search, sort, paging and CSV/JSON download; every chart has PNG download.

## Notes

* Demo mode KQL is a small subset (where, extend, project, summarize … by bin(), order, top, take, distinct,
  count, render). Live mode sends your KQL to Log Analytics unchanged.
* Charts load Chart.js from jsDelivr. Offline or blocked? Put `chart.umd.min.js` (v4) in `static/vendor/`.
* The server binds to 127.0.0.1 and has no sign-in of its own; everyone who reaches it acts as the server's Azure
  identity. Host it behind Entra ID authentication (for example App Service Easy Auth) before sharing, and use
  `FINOPS_READ_ONLY=1` for viewers. Writes require an `X-Requested-With` header as a basic CSRF guard.
