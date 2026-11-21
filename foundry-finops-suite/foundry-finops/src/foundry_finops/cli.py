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

from .errors import FinOpsError
from .models import Column, Table, dumps
from .service import GROUPINGS


def _emit(data: Any, fmt: str) -> None:
    if isinstance(data, dict) and "columns" in data and "rows" in data:
        cols = [c["name"] for c in data["columns"]]
        data = Table.from_records([dict(zip(cols, r, strict=False)) for r in data["rows"]])
        if not data.columns:  # an empty result still has a header row
            data = Table([Column(c) for c in cols], [])
    elif isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        data = Table.from_records(data)
    if isinstance(data, Table):
        if fmt == "csv":
            sys.stdout.write(data.to_csv())
        elif fmt == "json":
            print(json.dumps(data.to_records(), indent=2, default=str))
        else:
            _print_table(data.column_names, data.rows)
        return
    print(dumps(data))


def _print_table(cols: list[str], rows: list[list[Any]]) -> None:
    cells = [[("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))) for v in r] for r in rows]
    widths = [min(max([len(c)] + [len(r[i]) for r in cells]), 48) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v[:w].ljust(w) for v, w in zip(r, widths, strict=False)))


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="foundry-finops", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subscription", default=os.environ.get("FINOPS_SUBSCRIPTION_ID"))
    ap.add_argument("-g", "--resource-group", default=os.environ.get("FINOPS_RESOURCE_GROUP"))
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table")
    ap.add_argument("--demo", action="store_true", help="use illustrative demo data (no Azure calls)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    account_default = os.environ.get("FINOPS_FOUNDRY_ACCOUNT")
    p = sub.add_parser("cost-controls", help="list Foundry cost controls")
    p.add_argument("account", nargs="?", default=account_default, help="Foundry resource name (or FINOPS_FOUNDRY_ACCOUNT)")
    p = sub.add_parser("cost", help="billed cost grouped by project/agent/deployment/model/team")
    p.add_argument("--by", default="project", choices=GROUPINGS)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--account", default=account_default, help="Foundry resource name (project/agent/deployment tags)")
    p = sub.add_parser("tokens", help="token metrics per deployment")
    p.add_argument("account", nargs="?", default=account_default, help="Foundry resource name (or FINOPS_FOUNDRY_ACCOUNT)")
    p.add_argument("--hours", type=int, default=24)
    sub.add_parser("budgets", help="Cost Management budgets")
    p = sub.add_parser("quota", help="regional model quota")
    p.add_argument("--location", default=os.environ.get("FINOPS_LOCATION", "eastus2"))
    p = sub.add_parser("kql", help="run a KQL query")
    p.add_argument("target", help="workspace ID or App Insights resource ID (ignored in --demo)")
    p.add_argument("query")
    p.add_argument("--timespan", default="P1D")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    for name in ("days", "hours"):
        if getattr(args, name, 1) < 1:
            ap.error(f"--{name} must be at least 1")

    if args.demo:
        from .service import DemoService

        svc: Any = DemoService()
    else:
        if not args.subscription:
            # Never fall back to demo data silently: invented numbers in a spend report are worse than an error.
            ap.error("--subscription (or FINOPS_SUBSCRIPTION_ID) is required; use --demo for illustrative data")
        if args.cmd in ("cost-controls", "tokens") and not args.account:
            ap.error(f"{args.cmd}: pass the Foundry resource name (or set FINOPS_FOUNDRY_ACCOUNT)")
        from .service import LiveService, Settings

        svc = LiveService(Settings(
            subscription_id=args.subscription, resource_group=args.resource_group or "",
            foundry_account=getattr(args, "account", None) or "",
            location=getattr(args, "location", None) or "eastus2",
            logs_target=getattr(args, "target", None) or "",
            team_tag=os.environ.get("FINOPS_TEAM_TAG", "team"),
        ))

    try:
        result: Any
        if args.cmd == "cost-controls":
            result = svc.list_cost_controls()
        elif args.cmd == "cost":
            result = svc.cost_series(args.by, args.days)
        elif args.cmd == "tokens":
            result = svc.token_series("deployment", args.hours)
        elif args.cmd == "budgets":
            result = svc.list_budgets()
        elif args.cmd == "quota":
            result = svc.list_quota()
        else:
            result = svc.kql(args.query, args.timespan)
        _emit(result, args.format)
    except FinOpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, KeyError) as exc:
        print(f"error: {str(exc).strip(chr(39))}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # `foundry-finops ... | head`
        return 0
    finally:
        fo = getattr(svc, "fo", None)
        if fo is not None:
            fo.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
