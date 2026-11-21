import unittest

from foundry_finops.demo import KqlError, run
from foundry_finops.service import DemoService, Settings


class KqlTests(unittest.TestCase):
    rows = [{"T": "2026-11-01T01:00:00Z", "M": "a", "X": 1}, {"T": "2026-11-01T05:00:00Z", "M": "b", "X": 2},
            {"T": "2026-11-02T01:00:00Z", "M": "a", "X": 3}]

    def q(self, text):
        return run(text, {"Tab": self.rows})

    def test_summarize_bin(self):
        r = self.q("Tab | summarize S = sum(X) by bin(T, 1d) | order by T asc")
        self.assertEqual(r.columns, ["T", "S"])
        self.assertEqual([x[1] for x in r.rows], [3, 3])

    def test_where_project_extend(self):
        r = self.q("Tab | where M == 'a' and X > 1 | extend Y = X * 10 | project M, Y")
        self.assertEqual(r.rows, [["a", 30]])

    def test_top_distinct_count_render(self):
        self.assertEqual(self.q("Tab | top 1 by X desc").rows[0][2], 3)
        self.assertEqual(len(self.q("Tab | distinct M").rows), 2)
        self.assertEqual(self.q("Tab | count").rows, [[3]])
        self.assertEqual(self.q("Tab | render piechart").render, "piechart")

    def test_errors(self):
        with self.assertRaises(KqlError):
            self.q("Nope | count")
        with self.assertRaises(KqlError):
            self.q("Tab | join (Tab) on M")


class DemoServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = DemoService()

    def test_overview(self):
        o = self.svc.overview()
        self.assertGreater(o["kpis"]["spend_mtd"], 0)
        self.assertTrue(o["top_projects"])

    def test_every_saved_query_runs(self):
        for name, q in self.svc.saved_queries().items():
            with self.subTest(name=name):
                self.assertIsNotNone(self.svc.kql(q)["columns"])

    def test_crud_cost_control(self):
        self.svc.upsert_cost_control({"name": "new-cap", "amount": 50, "time_grain": "Daily",
                                      "block": {"threshold": 100}})
        self.assertIn("new-cap", [c["name"] for c in self.svc.list_cost_controls()])
        with self.assertRaises(ValueError):
            self.svc.upsert_cost_control({"name": "new-cap", "amount": 50, "category": "Tokens"})
        self.svc.delete_cost_control("new-cap")
        self.assertNotIn("new-cap", [c["name"] for c in self.svc.list_cost_controls()])

    def test_quota_guard(self):
        with self.assertRaises(ValueError):
            self.svc.set_capacity("o4-mini", 5000)

    def test_read_only(self):
        ro = DemoService(Settings(demo=True, read_only=True))
        with self.assertRaises(PermissionError):
            ro.delete_budget("support-team")


if __name__ == "__main__":
    unittest.main()
