"""Regression tests for the issues found in the code review."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import requests

from foundry_finops import (
    AuthenticationError,
    CostControl,
    CostQuery,
    FinOpsClient,
    PriceSheet,
    RetryPolicy,
    StaticTokenCredential,
)
from foundry_finops._transport import Response, Transport, _parse_retry_after
from foundry_finops.models import Budget, BudgetNotification, TimeGrain
from foundry_finops.operations.apim import (
    LlmEmitTokenMetric,
    find_token_limits,
    upsert_policy_fragment,
)
from foundry_finops.service import DemoService, Settings, _with_status, window_start

from .fakes import FakeSession, make_client


class TransportTests(unittest.TestCase):
    def test_retry_after_http_date_and_cost_management_headers(self):
        self.assertEqual(_parse_retry_after("7"), 7.0)
        self.assertIsNone(_parse_retry_after("garbage"))
        policy = RetryPolicy(backoff_factor=0)
        self.assertEqual(policy.sleep_for(0, {"x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "12"}), 12.0)
        self.assertEqual(policy.sleep_for(0, {"Retry-After-Ms": "1500"}), 1.5)

    def test_expired_token_refreshes_once_on_401(self):
        s = FakeSession()
        tokens = iter(["t1", "t2"])

        class Cred:
            def get_token(self, *scopes, **kw):
                return type("T", (), {"token": next(tokens), "expires_on": 9_999_999_999})()

        client = FinOpsClient("sub-1", Cred(), resource_group="rg", session=s)
        s.add("GET", "/accounts", 200, {"value": []})  # later routes win in the fake, so add the fallback first
        s.add("GET", "/accounts", 401, {"error": {"code": "ExpiredAuthenticationToken", "message": "expired"}}, times=1)
        self.assertEqual(list(client.foundry.list_accounts()), [])
        self.assertEqual([c["headers"]["Authorization"] for c in s.calls], ["Bearer t1", "Bearer t2"])

    def test_credential_failure_becomes_authentication_error(self):
        class Broken:
            def get_token(self, *scopes, **kw):
                raise RuntimeError("no login")

        t = Transport(Broken(), session=FakeSession())
        with self.assertRaises(AuthenticationError):
            t.request("GET", "https://management.azure.com/x", scope="s")

    def test_location_poll_finishes_on_204(self):
        s = FakeSession()
        client = make_client(s)
        s.add("GET", "/operationresults/1", 204)
        resp = Response(202, {"Location": "https://management.azure.com/operationresults/1"}, None)
        out = client._transport.poll(resp, scope="x")
        self.assertEqual(out.status_code, 204)

    def test_paging_loop_stops(self):
        s = FakeSession()
        client = make_client(s)
        s.add("GET", "/accounts", 200, {"value": [{"name": "a"}], "nextLink": "https://management.azure.com/accounts?p=2"})
        names = [a.name for a in client.foundry.list_accounts()]
        self.assertLessEqual(len(names), 2)  # terminated instead of looping forever

    def test_connection_errors_retry_then_raise(self):
        class Flaky(requests.Session):
            n = 0

            def request(self, *a, **k):
                Flaky.n += 1
                raise requests.ConnectionError("boom")

        client = FinOpsClient("sub-1", StaticTokenCredential("x"), session=Flaky(), retry=RetryPolicy(total=2))
        client._transport.sleep = lambda s: None
        with self.assertRaises(Exception) as cm:
            list(client.foundry.list_accounts())
        self.assertEqual(Flaky.n, 3)
        self.assertEqual(getattr(cm.exception, "status_code", None), 0)


class UrlSafetyTests(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.fo = make_client(self.s)

    def test_scope_cannot_redirect_the_bearer_token(self):
        for bad in ("https://evil.example.com", "/subscriptions/x/../../y", "relative/id", "/subscriptions/x?a=b"):
            with self.subTest(scope=bad), self.assertRaises(ValueError):
                self.fo.tags.get(bad)
        self.assertEqual(self.s.calls, [])

    def test_names_are_encoded_as_single_segments(self):
        self.s.add("GET", "/metricAlerts/", 200, {"name": "x"})
        self.fo.alerts.get_metric_alert("a/../b")
        self.assertIn("/metricAlerts/a%2F..%2Fb", self.s.calls[-1]["url"])

    def test_odata_filter_quotes_are_escaped(self):
        self.s.add("GET", "/resources", 200, {"value": []})
        list(self.fo.tags.resources_with_tag("o'brien"))
        self.assertIn("tagName eq 'o''brien'", self.s.calls[-1]["params"]["$filter"])

    def test_tag_remove_of_missing_tag_makes_no_patch(self):
        sid = "/subscriptions/sub-1/resourceGroups/rg"
        self.s.add("GET", "/tags/default", 200, {"properties": {"tags": {"a": "1"}}})
        self.assertEqual(self.fo.tags.remove(sid, "zzz"), {"a": "1"})
        self.assertFalse([c for c in self.s.calls if c["method"] == "PATCH"])


class ModelTests(unittest.TestCase):
    def test_last_days_covers_whole_days(self):
        q = CostQuery.last_days(30)
        start = datetime.fromisoformat(q.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(q.end.replace("Z", "+00:00"))
        self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))
        self.assertEqual((end.date() - start.date()).days, 29)  # 30 calendar days including today
        self.assertEqual(end.date(), datetime.now(timezone.utc).date())

    def test_price_lookup_does_not_match_a_different_model(self):
        sheet = PriceSheet.from_mapping({"gpt-4o": {"input": 2.5, "output": 10}})
        self.assertIsNotNone(sheet.lookup("gpt-4o-2024-08-06"))
        self.assertIsNone(sheet.lookup("gpt-4o-mini"))

    def test_cached_tokens_cannot_exceed_input(self):
        sheet = PriceSheet.from_mapping({"m": {"input": 1.0, "output": 0.0, "cached_input": 0.5}})
        self.assertEqual(sheet.estimate("m", 1_000_000, 0, cached_tokens=5_000_000), 0.5)

    def test_bad_price_mapping_has_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "input, output"):
            PriceSheet.from_mapping({"m": {"in": 1}})

    def test_budget_needs_a_contact_and_valid_amount(self):
        with self.assertRaisesRegex(ValueError, "email"):
            Budget(name="b", amount=10, notifications=[BudgetNotification(80)]).to_dict()
        with self.assertRaises(ValueError):
            Budget(name="b", amount=0).to_dict()

    def test_cost_control_rejects_budget_only_time_grains(self):
        with self.assertRaisesRegex(ValueError, "Daily, Weekly or Monthly"):
            CostControl(name="x", amount=5, time_grain=TimeGrain.ANNUALLY).to_dict()

    def test_unknown_service_enum_values_do_not_break_parsing(self):
        cc = CostControl.from_dict({"name": "n", "properties": {"category": "Cost", "amount": 5, "timeGrain": "Hourly",
                                                                "grouping": [{"name": "newkind"}]}})
        self.assertEqual(cc.time_grain, TimeGrain.MONTHLY)


class ApimTests(unittest.TestCase):
    def test_malformed_policy_is_a_value_error(self):
        with self.assertRaises(ValueError):
            upsert_policy_fragment("<policies><inbound>", "<x/>")
        self.assertEqual(find_token_limits("<not xml"), [])

    def test_custom_metric_dimension_needs_value(self):
        with self.assertRaises(ValueError):
            LlmEmitTokenMetric(dimensions={"Team": None}).to_xml()

    def test_subscription_scope_is_expanded_to_a_resource_id(self):
        from foundry_finops import ApimSubscription

        s = FakeSession()
        fo = make_client(s)
        s.add("PUT", "/subscriptions/team-a", 200, {"name": "team-a", "properties": {}})
        fo.apim.create_or_update_subscription("gw", ApimSubscription(name="team-a", scope="/products/p1"))
        sent = s.calls[-1]["json"]["properties"]["scope"]
        self.assertTrue(sent.endswith("/providers/Microsoft.ApiManagement/service/gw/products/p1"), sent)
        with self.assertRaises(ValueError):
            fo.apim.create_or_update_subscription("gw", ApimSubscription(name="x", scope="nonsense"))


class ServiceTests(unittest.TestCase):
    def test_absolute_thresholds_are_compared_as_percent(self):
        row = {"amount": 1000, "usage": 900, "alert": {"threshold": 800, "threshold_type": "Absolute"},
               "block": {"threshold": 1000, "threshold_type": "Absolute"}}
        self.assertEqual(_with_status(row)["state"], "Near threshold")
        row = {"amount": 1000, "usage": 1000, "block": {"threshold": 1000, "threshold_type": "Absolute"}}
        self.assertEqual(_with_status(row)["state"], "Limit reached")

    def test_unknown_group_by_is_rejected_not_relabelled(self):
        svc = DemoService()
        with self.assertRaises(ValueError):
            svc.cost_series("bogus")
        with self.assertRaises(ValueError):
            svc.token_series("bogus")

    def test_set_capacity_validates(self):
        with self.assertRaises(ValueError):
            DemoService().set_capacity(DemoService().list_deployments()[0]["name"], 0)


class CliTests(unittest.TestCase):
    def test_live_mode_without_subscription_is_an_error_not_demo(self):
        from foundry_finops.cli import main

        with self.assertRaises(SystemExit) as cm:
            main(["--format", "json", "budgets"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()


class MultiThresholdTests(unittest.TestCase):
    def test_round_trip_keeps_every_alert_and_block(self):
        from foundry_finops import Threshold

        conn = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a/connections/eg"
        cc = CostControl(name="cap", amount=1000, alert=Threshold(50, event_grid_connection_id=conn),
                         extra_alerts=[Threshold(80, event_grid_connection_id=conn)],
                         block=Threshold(100))
        body = cc.to_dict()
        self.assertEqual(len(body["properties"]["notifications"]), 2)
        back = CostControl.from_dict({"name": "cap", "properties": body["properties"]})
        self.assertEqual([t.threshold for t in back.alerts], [50, 80])
        self.assertEqual(back.to_dict()["properties"]["notifications"], body["properties"]["notifications"])

    def test_validation_covers_extra_thresholds(self):
        from foundry_finops import Threshold

        conn = "x"
        with self.assertRaisesRegex(ValueError, "at or below"):
            CostControl(name="c", amount=10, alert=Threshold(50, event_grid_connection_id=conn),
                        extra_alerts=[Threshold(95, event_grid_connection_id=conn)], block=Threshold(90)).to_dict()
        with self.assertRaisesRegex(ValueError, "unique"):
            CostControl(name="c", amount=10, alert=Threshold(50, event_grid_connection_id=conn),
                        extra_alerts=[Threshold(50, event_grid_connection_id=conn)]).to_dict()

    def test_status_uses_the_lowest_threshold(self):
        row = {"amount": 100, "usage": 55, "alerts": [{"threshold": 50}, {"threshold": 80}],
               "blocks": [{"threshold": 100}]}
        self.assertEqual(_with_status(row)["state"], "Near threshold")


class UsageEstimateTests(unittest.TestCase):
    ACCT = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct"

    def _service(self, cost_controls):
        from foundry_finops.service import LiveService

        s = FakeSession()
        s.add("GET", "/costControls", 200, {"value": cost_controls})
        s.add("GET", "/projects", 200, {"value": []})
        client = make_client(s)
        return LiveService(Settings(subscription_id="sub-1", resource_group="rg", foundry_account="acct"), client), s

    def test_window_start(self):
        now = datetime(2026, 3, 18, 15, 30, tzinfo=timezone.utc)
        cc = CostControl(name="c", amount=1, time_grain=TimeGrain.DAILY)
        self.assertEqual(window_start(cc, now), datetime(2026, 3, 18, tzinfo=timezone.utc))
        cc = CostControl(name="c", amount=1, time_grain=TimeGrain.MONTHLY)
        self.assertEqual(window_start(cc, now), datetime(2026, 3, 1, tzinfo=timezone.utc))
        cc = CostControl(name="c", amount=1, time_grain=TimeGrain.MONTHLY, start_date="2026-01-20T00:00:00Z")
        self.assertEqual(window_start(cc, now), datetime(2026, 2, 20, tzinfo=timezone.utc))
        cc = CostControl(name="c", amount=1, time_grain=TimeGrain.MONTHLY, start_date="2026-01-31T00:00:00Z")
        self.assertEqual(window_start(CostControl(name="c", amount=1, start_date=cc.start_date),
                                      datetime(2026, 2, 28, 1, tzinfo=timezone.utc)),
                         datetime(2026, 2, 28, tzinfo=timezone.utc))  # clamps to the last day of a short month
        cc = CostControl(name="c", amount=1, time_grain=TimeGrain.WEEKLY, start_date="2026-03-02T00:00:00Z")
        self.assertEqual(window_start(cc, now), datetime(2026, 3, 16, tzinfo=timezone.utc))

    def test_cost_budget_usage_comes_from_billed_cost(self):
        svc, s = self._service([{"id": self.ACCT + "/costControls/cap", "name": "cap", "properties": {
            "category": "Cost", "amount": 100, "timeGrain": "Monthly",
            "filter": {"additionalInfo": {"name": "project", "operator": "In", "values": ["risk"]}},
            "providerProperties": {"actions": {"b": {"type": "Block", "threshold": 80, "thresholdType": "Percentage"}}}}}])
        s.add("POST", "/providers/Microsoft.CostManagement/query", 200, {"properties": {
            "columns": [{"name": "Cost", "type": "Number"}, {"name": "Currency", "type": "String"}],
            "rows": [[85.5, "USD"]]}})
        [row] = svc.list_cost_controls()
        self.assertEqual(row["usage"], 85.5)
        self.assertEqual(row["state"], "Limit reached")
        query = [c for c in s.calls if c["method"] == "POST"][-1]
        self.assertTrue(query["url"].startswith("https://management.azure.com" + self.ACCT))
        self.assertEqual(query["json"]["dataset"]["filter"]["tags"], {"name": "project", "operator": "In", "values": ["risk"]})

    def test_token_budget_usage_comes_from_metrics(self):
        svc, s = self._service([{"id": self.ACCT + "/costControls/tok", "name": "tok", "properties": {
            "category": "Tokens", "amount": 1000, "timeGrain": "Daily",
            "filter": {"additionalInfo": {"name": "deployment", "operator": "In", "values": ["gpt"]}}}}])
        s.add("GET", "/providers/Microsoft.Insights/metrics", 200, {"value": [{
            "name": {"value": "TokenTransaction"}, "unit": "Count",
            "timeseries": [{"data": [{"timeStamp": "t1", "total": 300}, {"timeStamp": "t2", "total": 250}]}]}]})
        [row] = svc.list_cost_controls()
        self.assertEqual(row["usage"], 550)
        self.assertEqual(row["percent_used"], 55.0)
        metrics = next(c for c in s.calls if "/providers/Microsoft.Insights/metrics" in c["url"])
        self.assertEqual(metrics["params"]["$filter"], "ModelDeploymentName eq 'gpt'")

    def test_unreproducible_budgets_stay_unknown_with_a_reason(self):
        svc, _ = self._service([{"id": self.ACCT + "/costControls/s", "name": "s", "properties": {
            "category": "Cost", "amount": 10, "timeGrain": "Daily",
            "filter": {"additionalInfo": {"name": "sessionId", "operator": "In", "values": ["a"]}}}}])
        [row] = svc.list_cost_controls()
        self.assertIsNone(row["usage"])
        self.assertIn("session", row["usage_note"].lower())
        self.assertEqual(row["state"], "Usage unknown")

    def test_a_failing_lookup_does_not_break_the_listing(self):
        svc, _ = self._service([{"id": self.ACCT + "/costControls/cap", "name": "cap", "properties": {
            "category": "Cost", "amount": 100, "timeGrain": "Monthly"}}])
        # no fake route for the Cost Management query -> 404
        [row] = svc.list_cost_controls()
        self.assertIsNone(row["usage"])
        self.assertIn("failed", row["usage_note"])

    def test_live_upsert_round_trips(self):
        svc, s = self._service([])
        s.add("PUT", "/costControls/cap", 200, {"name": "cap", "properties": {"category": "Cost", "amount": 5,
                                                                              "timeGrain": "Monthly"}})
        out = svc.upsert_cost_control({"name": "cap", "amount": 5})
        self.assertEqual(out["name"], "cap")
