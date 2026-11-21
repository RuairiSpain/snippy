"""``foundry-finops`` command line: quick reports straight from the terminal.

Examples::

    foundry-finops --subscription <id> -g rg-ai cost-controls contoso-foundry
    foundry-finops --subscription <id> -g rg-ai cost --by project --scope <foundry-resource-id> --format csv
    foundry-finops --subscription <id> -g rg-ai tokens contoso-foundry --hours 48
    foundry-finops --subscription <id> quota --location eastus2
    foundry-finops --demo cost --by team
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .models import Table, dumps


def _emit(data: Any, fmt: str) -> None:
    if isinstance(data, dict) and "columns" in data and "rows" in data:
        cols = [c["name"] for c in data["columns"]]
        data = Table.from_records([dict(zip(cols, r)) for r in data["rows"]])
    if isinstance(data, Table):
        if fmt == "csv":
            sys.stdout.write(data.to_csv())
        elif fmt == "json":
            print(json.dumps(data.to_records(), indent=2, default=str))
        else:
            _print_table(data.column_names, data.rows)
        return
    if isinstance(data, list) and data and isinstance(data[0], dict) and fmt == "table":
        cols = list(data[0].keys())
        _print_table(cols, [[r.get(c) for c in cols] for r in data])
        return
    print(dumps(data))


def _print_table(cols: list[str], rows: list[list[Any]]) -> None:
    cells = [[("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))) for v in r] for r in rows]
    widths = [min(max([len(c)] + [len(r[i]) for r in cells]), 48) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v[:w].ljust(w) for v, w in zip(r, widths)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="foundry-finops", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subscription", default=os.environ.get("FINOPS_SUBSCRIPTION_ID"))
    ap.add_argument("-g", "--resource-group", default=os.environ.get("FINOPS_RESOURCE_GROUP"))
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table")
    ap.add_argument("--demo", action="store_true", help="use illustrative demo data")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cost-controls", help="list Foundry cost controls")
    p.add_argument("account", nargs="?")
    p = sub.add_parser("cost", help="billed cost grouped by project/agent/deployment/model/team")
    p.add_argument("--by", default="project")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--scope")
    p = sub.add_parser("tokens", help="token metrics per deployment")
    p.add_argument("account", nargs="?")
    p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("budgets", help="Cost Management budgets")
    p = sub.add_parser("quota", help="regional model quota")
    p.add_argument("--location", default="eastus2")
    p = sub.add_parser("kql", help="run a KQL query")
    p.add_argument("target", help="workspace ID or App Insights resource ID (ignored in --demo)")
    p.add_argument("query")
    p.add_argument("--timespan", default="P1D")
    args = ap.parse_args(argv)

    if args.demo or not args.subscription:
        from .service import DemoService

        svc = DemoService()
        result: Any = {
            "cost-controls": lambda: svc.list_cost_controls(),
            "cost": lambda: svc.cost_series(args.by, args.days),
            "tokens": lambda: svc.token_series("deployment", args.hours),
            "budgets": lambda: svc.list_budgets(),
            "quota": lambda: svc.list_quota(),
            "kql": lambda: svc.kql(args.query),
        }[args.cmd]()
        _emit(result, args.format)
        return 0

    from .client import FinOpsClient

    with FinOpsClient(args.subscription, resource_group=args.resource_group) as fo:
        if args.cmd == "cost-controls":
            _emit([cc.to_json() for cc in fo.cost_controls.list_all(args.account)], args.format)
        elif args.cmd == "cost":
            _emit(fo.cost.cost_by(args.by, days=args.days, scope=args.scope), args.format)
        elif args.cmd == "tokens":
            _emit(fo.metrics.query_table(fo.foundry.account_id(args.account), hours=args.hours), args.format)
        elif args.cmd == "budgets":
            _emit([b.to_json() for b in fo.cost.list_budgets()], args.format)
        elif args.cmd == "quota":
            _emit([q.to_json() for q in fo.quota.usages(args.location)], args.format)
        elif args.cmd == "kql":
            _emit(fo.logs.query(args.target, args.query, timespan=args.timespan), args.format)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
