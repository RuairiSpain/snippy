"""End-to-end tour of foundry-finops against your subscription (read-only)."""

import os

from foundry_finops import FinOpsClient, FoundryMetrics

sub, rg, acct = (os.environ[k] for k in ("FINOPS_SUBSCRIPTION_ID", "FINOPS_RESOURCE_GROUP", "FINOPS_FOUNDRY_ACCOUNT"))

with FinOpsClient(sub, resource_group=rg) as fo:
    account_id = fo.foundry.account_id(acct)
    print("Projects:", [p.name for p in fo.foundry.list_projects(acct)])
    print("Deployments:", [(d.name, d.tokens_per_minute) for d in fo.foundry.list_deployments(acct)])
    for cc in fo.cost_controls.list_all(acct):
        print(f"Cost control {cc.name}: {cc.amount} {cc.category} per {cc.time_grain}, filters={cc.filters}")
    print(fo.cost.cost_by("project", scope=account_id, days=30, daily=False).to_csv())
    tokens = fo.metrics.query(account_id, FoundryMetrics.TOKENS, hours=24)
    print("Tokens (24h):", sum(s.total for s in tokens if s.metric == FoundryMetrics.TOTAL_TOKENS))
    for q in fo.quota.usages(os.environ.get("FINOPS_LOCATION", "eastus2")):
        if q.percent_used > 50:
            print(f"Quota {q.display_name}: {q.percent_used}% used")
