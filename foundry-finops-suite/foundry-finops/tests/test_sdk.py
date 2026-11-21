import unittest

from foundry_finops import (
    Budget,
    BudgetCategory,
    BudgetExceededError,
    BudgetNotification,
    BudgetSharing,
    CostControl,
    CostControlAlertEvent,
    CostQuery,
    LlmEmitTokenMetric,
    LlmTokenLimit,
    MetricAlert,
    NotFoundError,
    PriceSheet,
    ResourceId,
    Threshold,
    ThrottledError,
    TimeGrain,
    is_budget_exceeded,
)
from foundry_finops.operations.apim import find_token_limits, upsert_policy_fragment
from foundry_finops.tracing import UsageTracker, extract_usage, finops_context

from .fakes import FakeSession, make_client

ACCT = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct"
CONN = f"{ACCT}/connections/eg"


class CostControlModelTests(unittest.TestCase):
    def test_body_matches_documented_example(self):
        cc = CostControl(
            name="fraud-risk-monthly", category=BudgetCategory.COST, amount=500, time_grain=TimeGrain.MONTHLY,
            start_date="2026-12-01T00:00:00Z", end_date="2027-07-01T00:00:00Z",
            filters={"project": ["fraud-risk"]}, sharing=BudgetSharing.PER_AGENT,
            alert=Threshold(80, event_grid_connection_id="<conn>"), block=Threshold(100),
        )
        expected = {"properties": {
            "category": "Cost", "amount": 500, "timeGrain": "Monthly",
            "timePeriod": {"startDate": "2026-12-01T00:00:00Z", "endDate": "2027-07-01T00:00:00Z"},
            "filter": {"additionalInfo": {"name": "project", "operator": "In", "values": ["fraud-risk"]}},
            "grouping": [{"name": "agent", "type": "AdditionalInfoKey"}],
            "notifications": {"Actual_GreaterThanOrEqualTo_80_Percent": {
                "enabled": True, "operator": "GreaterThanOrEqualTo", "threshold": 80,
                "thresholdType": "Percentage", "eventGridConnectionId": "<conn>"}},
            "providerProperties": {"actions": {"Block_GreaterThanOrEqualTo_100_Percent": {
                "type": "Block", "operator": "GreaterThanOrEqualTo", "threshold": 100,
                "thresholdType": "Percentage"}}},
        }}
        self.assertEqual(cc.to_dict(), expected)
        back = CostControl.from_dict({"name": "fraud-risk-monthly", **expected})
        self.assertEqual(back.sharing, BudgetSharing.PER_AGENT)
        self.assertEqual(back.alert.threshold, 80)
        self.assertEqual(back.filters, {"project": ["fraud-risk"]})

    def test_multiple_filters_use_and(self):
        cc = CostControl(name="x", amount=10, category=BudgetCategory.TOKENS,
                         filters={"project": ["p"], "agent": ["a"], "tokenType": ["output"]})
        f = cc.to_dict()["properties"]["filter"]
        self.assertEqual(len(f["and"]), 3)

    def test_validation_rules(self):
        with self.assertRaises(ValueError):
            CostControl(name="x", amount=10, filters={"agent": ["a"]}).to_dict()  # agent needs one project
        with self.assertRaises(ValueError):
            CostControl(name="x", amount=10.5, category=BudgetCategory.TOKENS).to_dict()
        with self.assertRaises(ValueError):
            CostControl(name="x", amount=10, alert=Threshold(90, event_grid_connection_id="c"),
                        block=Threshold(80)).to_dict()
        with self.assertRaises(ValueError):
            CostControl(name="x", amount=10, alert=Threshold(80)).to_dict()  # missing connection
        with self.assertRaises(ValueError):
            CostControl(name="bad name!", amount=10).to_dict()
        with self.assertRaises(ValueError):
            CostControl(name="x", amount=10, project="p", sharing=BudgetSharing.PER_PROJECT).to_dict()

    def test_alert_event(self):
        ev = CostControlAlertEvent.from_cloud_event({
            "type": "costControl.alert", "source": ACCT, "time": "t", "subject": "s",
            "data": {"unit": "usd", "threshold": "80.0%", "action": "alert", "severity": "warning",
                     "totalConsumed": 400.12, "limit": 500, "remaining": 99.88, "agentName": "a",
                     "windowEnd": "w"}})
        self.assertEqual(ev.remaining, 99.88)
        self.assertEqual(ev.dedupe_key[-2], "a")


class CostControlOperationTests(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.fo = make_client(self.s)

    def test_create_project_cost_control_url_and_headers(self):
        self.s.add("PUT", "/costControls/", 201, {"name": "cap", "properties": {"category": "Cost", "amount": 5,
                                                                                  "timeGrain": "Daily"}})
        out = self.fo.cost_controls.create_or_update("acct", CostControl(name="cap", amount=5), project="p1",
                                                     only_if_new=True)
        call = self.s.calls[-1]
        self.assertIn("/accounts/acct/projects/p1/costControls/cap", call["url"])
        self.assertEqual(call["params"]["api-version"], "2026-11-15-preview")
        self.assertEqual(call["headers"]["If-None-Match"], "*")
        self.assertTrue(call["headers"]["Authorization"].startswith("Bearer "))
        self.assertEqual(out.time_grain, TimeGrain.DAILY)

    def test_list_pages(self):
        self.s.add("GET", "page2", 200, {"value": [{"name": "b", "properties": {"amount": 2}}]})
        self.s.add("GET", "/costControls", 200, {"value": [{"name": "a", "properties": {"amount": 1}}],
                                                 "nextLink": "https://management.azure.com/x?page2"})
        names = [c.name for c in self.fo.cost_controls.list("acct")]
        self.assertEqual(names, ["a", "b"])

    def test_patch_refuses_category_change(self):
        with self.assertRaises(ValueError):
            self.fo.cost_controls.update("acct", "x", category="Tokens")

    def test_find_connection(self):
        self.s.add("GET", "/projects", 200, {"value": []})
        self.s.add("GET", "/costControls", 200, {"value": [{"name": "a", "properties": {
            "amount": 1, "notifications": {"n": {"threshold": 80, "eventGridConnectionId": CONN}}}}]})
        self.assertEqual(self.fo.cost_controls.find_event_grid_connection_id("acct"), CONN)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.fo = make_client(self.s)

    def test_retries_on_429_then_succeeds(self):
        self.s.add("GET", "/deployments/d1", 200, {"name": "d1", "sku": {"name": "GlobalStandard", "capacity": 5},
                                                   "properties": {"model": {"name": "gpt"}}})
        self.s.add("GET", "/deployments/d1", 429, {"error": {"code": "Throttled", "message": "slow"}},
                   {"Retry-After": "0"}, times=1)
        d = self.fo.foundry.get_deployment("acct", "d1")
        self.assertEqual(d.capacity, 5)
        self.assertEqual(d.tokens_per_minute, 5000)
        self.assertEqual(len(self.s.calls), 2)

    def test_budget_block_is_not_retried(self):
        self.s.add("GET", "/deployments/d1", 429, {"error": {"message": "Budget exceeded for cost control."}},
                   {"x-ms-budget-cause": "budget_exceeded", "x-ms-budget-window-reset": "2026-12-01T00:00:00Z",
                    "x-ms-applied-cost-control-id": "cc1"})
        with self.assertRaises(BudgetExceededError) as ctx:
            self.fo.foundry.get_deployment("acct", "d1")
        self.assertEqual(len(self.s.calls), 1)
        self.assertEqual(ctx.exception.reset_at, "2026-12-01T00:00:00Z")
        self.assertEqual(ctx.exception.cost_control_id, "cc1")

    def test_throttle_exhausts(self):
        self.s.add("GET", "/deployments/d1", 429, {"error": {"message": "slow"}})
        with self.assertRaises(ThrottledError):
            self.fo.foundry.get_deployment("acct", "d1")
        self.assertEqual(len(self.s.calls), 3)

    def test_not_found(self):
        with self.assertRaises(NotFoundError):
            self.fo.foundry.get_deployment("acct", "nope")

    def test_is_budget_exceeded_helper(self):
        self.assertTrue(is_budget_exceeded({"X-MS-Budget-Cause": "budget_exceeded"}))
        self.assertTrue(is_budget_exceeded({}, "error: Budget exceeded for cost control."))
        self.assertFalse(is_budget_exceeded({"retry-after": "1"}))


class CostManagementTests(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.fo = make_client(self.s)

    def test_query_body_and_paging(self):
        self.s.add("POST", "skiptoken", 200, {"properties": {"columns": [], "rows": [[2.0, 20261102, "p2", "USD"]]}})
        self.s.add("POST", "/Microsoft.CostManagement/query", 200, {"properties": {
            "columns": [{"name": "Cost", "type": "Number"}, {"name": "UsageDate", "type": "Number"},
                        {"name": "TagValue", "type": "String"}, {"name": "Currency", "type": "String"}],
            "rows": [[1.5, 20261101, "p1", "USD"]],
            "nextLink": "https://management.azure.com/q?$skiptoken=abc"}})
        q = CostQuery.between("2026-11-01", "2026-11-30").group_by_tag("project").where_tag("deployment", ["d"])
        t = self.fo.cost.query(q, scope=ACCT)
        body = self.s.calls[0]["json"]
        self.assertEqual(body["timeframe"], "Custom")
        self.assertEqual(body["dataset"]["grouping"], [{"type": "TagKey", "name": "project"}])
        self.assertEqual(body["dataset"]["filter"]["tags"]["name"], "deployment")
        self.assertIn(ACCT + "/providers/Microsoft.CostManagement/query", self.s.calls[0]["url"])
        self.assertEqual(t.to_records()[1]["UsageDate"], "2026-11-02")
        self.assertEqual(len(t), 2)
        self.assertIn("Cost,UsageDate", t.to_csv())

    def test_budget_roundtrip(self):
        b = Budget(name="b1", amount=100, start_date="2026-11-01",
                   notifications=[BudgetNotification(80, ["a@b.com"]),
                                  BudgetNotification(100, threshold_type="Forecasted")])
        body = b.to_dict()
        self.assertEqual(body["properties"]["timePeriod"]["startDate"], "2026-11-01T00:00:00Z")
        self.assertEqual(len(body["properties"]["notifications"]), 2)
        self.s.add("PUT", "/budgets/b1", 200, {"name": "b1", **body, "properties": {
            **body["properties"], "currentSpend": {"amount": 12.5, "unit": "USD"}}})
        out = self.fo.cost.create_or_update_budget(b)
        self.assertEqual(out.current_spend, 12.5)
        self.assertIn("/resourceGroups/rg/providers/Microsoft.CostManagement/budgets/b1", self.s.calls[-1]["url"])


class MonitorTests(unittest.TestCase):
    def test_metrics_parse(self):
        s = FakeSession()
        fo = make_client(s)
        s.add("GET", "/providers/Microsoft.Insights/metrics", 200, {"value": [{
            "name": {"value": "TokenTransaction"}, "unit": "Count",
            "timeseries": [{"metadatavalues": [{"name": {"value": "modeldeploymentname"}, "value": "gpt"}],
                            "data": [{"timeStamp": "t1", "total": 5}, {"timeStamp": "t2", "total": 7}]}]}]})
        series = fo.metrics.query(ACCT, ["TokenTransaction"], hours=2)
        self.assertEqual(series[0].total, 12)
        self.assertIn("ModelDeploymentName eq '*'", s.calls[0]["params"]["$filter"])
        self.assertEqual(fo.metrics.query_table(ACCT).column_names[-1], "Value")

    def test_logs_resource_query(self):
        s = FakeSession()
        fo = make_client(s)
        s.add("POST", "api.loganalytics.io/v1/subscriptions", 200, {"tables": [{"name": "PrimaryResult",
              "columns": [{"name": "n", "type": "long"}], "rows": [[3]]}]})
        t = fo.logs.query("/subscriptions/s/resourceGroups/r/providers/microsoft.insights/components/ai", "traces | count")
        self.assertEqual(t.rows, [[3]])


class TagAndAlertTests(unittest.TestCase):
    def test_merge_tags(self):
        s = FakeSession()
        fo = make_client(s)
        s.add("PATCH", "/providers/Microsoft.Resources/tags/default", 200, {"properties": {"tags": {"team": "risk"}}})
        self.assertEqual(fo.tags.merge(ACCT, {"team": "risk"}), {"team": "risk"})
        self.assertEqual(s.calls[0]["json"]["operation"], "Merge")
        self.assertTrue(s.calls[0]["url"].startswith("https://management.azure.com/subscriptions/"))

    def test_metric_alert_body(self):
        a = MetricAlert(name="a", scopes=[ACCT], threshold=10,
                        dimensions=[{"name": "ModelDeploymentName", "operator": "Include", "values": ["gpt"]}])
        b = a.to_dict()["properties"]
        self.assertEqual(b["criteria"]["allOf"][0]["metricName"], "TokenTransaction")
        self.assertEqual(MetricAlert.from_dict({"name": "a", "properties": b}).threshold, 10)


class ApimTests(unittest.TestCase):
    def test_token_limit_xml(self):
        xml = LlmTokenLimit(tokens_per_minute=5000, token_quota=100000, token_quota_period="Monthly").to_xml()
        self.assertIn('tokens-per-minute="5000"', xml)
        self.assertIn('token-quota-period="Monthly"', xml)
        with self.assertRaises(ValueError):
            LlmTokenLimit().to_xml()
        with self.assertRaises(ValueError):
            LlmTokenLimit(token_quota=5).to_xml()

    def test_upsert_replaces_existing(self):
        p1 = upsert_policy_fragment(None, LlmTokenLimit(tokens_per_minute=1).to_xml())
        p2 = upsert_policy_fragment(p1, LlmTokenLimit(tokens_per_minute=2).to_xml())
        lims = find_token_limits(p2)
        self.assertEqual([x.tokens_per_minute for x in lims], [2])
        p3 = upsert_policy_fragment(p2, LlmEmitTokenMetric().to_xml())
        self.assertIn("llm-emit-token-metric", p3)
        self.assertLess(p3.index("<base"), p3.index("llm-token-limit"))

    def test_too_many_dimensions(self):
        with self.assertRaises(ValueError):
            LlmEmitTokenMetric(dimensions={f"d{i}": "x" for i in range(6)}).to_xml()

    def test_set_token_limit_calls(self):
        s = FakeSession()
        fo = make_client(s)
        s.add("GET", "/products/team-a/policies/policy", 404, {"error": {"code": "NotFound", "message": "x"}})
        s.add("PUT", "/products/team-a/policies/policy", 200, {})
        fo.apim.set_token_limit("gw", LlmTokenLimit(tokens_per_minute=100), "product:team-a")
        put = s.calls[-1]
        self.assertEqual(put["json"]["properties"]["format"], "rawxml")
        self.assertEqual(put["headers"]["If-Match"], "*")


class PricingAndTracingTests(unittest.TestCase):
    def test_estimate(self):
        sheet = PriceSheet.from_mapping({"gpt-5.4-mini": {"input": 0.25, "output": 2.0, "cached_input": 0.025}})
        self.assertAlmostEqual(sheet.estimate("gpt-5.4-mini-2026", 1_000_000, 500_000, 200_000), 0.2 + 0.005 + 1.0)
        self.assertIsNone(sheet.estimate("other", 1, 1))

    def test_extract_usage_shapes(self):
        chat = {"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                        "prompt_tokens_details": {"cached_tokens": 4}}}
        resp = {"model": "m", "usage": {"input_tokens": 7, "output_tokens": 3,
                                        "output_tokens_details": {"reasoning_tokens": 2}}}
        self.assertEqual(extract_usage(chat), ("m", 10, 5, 4, 0))
        self.assertEqual(extract_usage(resp), ("m", 7, 3, 0, 2))

    def test_tracker_context(self):
        tr = UsageTracker(PriceSheet.from_mapping({"m": {"input": 1, "output": 1}}))
        with finops_context(team="risk"), finops_context(cost_center="cc1"):
            rec = tr.record({"model": "m", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}, agent="a")
        self.assertEqual(rec.attributes, {"team": "risk", "cost_center": "cc1", "agent": "a"})
        self.assertEqual(rec.cost, 1.0)


class IdTests(unittest.TestCase):
    def test_parse(self):
        rid = ResourceId.parse(f"{ACCT}/projects/p1/costControls/c")
        self.assertEqual(rid.resource_group, "rg")
        self.assertEqual(rid.name, "c")
        self.assertEqual(rid.type, "Microsoft.CognitiveServices/accounts/projects/costControls")


if __name__ == "__main__":
    unittest.main()
