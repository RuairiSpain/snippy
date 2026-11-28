"""Print what an admin must create on the connector namespace for a trigger source.

    python -m sandbox_mux.triggers_cli arm --source mailbox-support \\
        --callback-base-url https://agents.contoso.com --config config.yaml

The service never calls the namespace itself, so a pipeline or an administrator keeps control
of what the namespace subscribes to. The body follows Microsoft's Azure/Connectors
documentation (API 2026-05-01-preview) and has not been run against a namespace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import Config
from .triggers import arm_trigger_body


def main(argv: list[str] | None = None, out=None) -> int:
    p = argparse.ArgumentParser(prog="sandbox_mux.triggers_cli")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("arm", help="print the namespace trigger for a source")
    a.add_argument("--source", required=True)
    a.add_argument("--callback-base-url", required=True, help="public https URL of this service")
    args = p.parse_args(argv)
    out = out or sys.stdout
    path = args.config or os.environ.get("SANDBOX_MUX_CONFIG", "config.yaml")
    cfg = Config.load(path)
    src = cfg.triggers.sources.get(args.source)
    if not src:
        print(f"error: no such source '{args.source}' in triggers.sources", file=sys.stderr)
        return 2
    if not args.callback_base_url.startswith("https://"):
        print("error: the callback URL must be https", file=sys.stderr)
        return 2
    audience = cfg.triggers.callback_audience or cfg.auth.audience
    print(json.dumps(arm_trigger_body(args.source, src, args.callback_base_url, audience), indent=2), file=out)
    print("\n# Create it (also needs an access policy letting the namespace's managed identity use\n"
          f"# connection '{src.connection}', and its object ID in triggers.callback_principal_ids):\n"
          "# az rest --method PUT --url \"https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/"
          f"providers/Microsoft.Web/connectorGateways/<namespace>/triggerConfigs/{args.source}"
          "?api-version=2026-05-01-preview\" --body @trigger.json", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
