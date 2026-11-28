"""Build the sandbox disk image that contains your agent runtime. Run it from CI.

    python -m sandbox_mux.image build --image myregistry.azurecr.io/agent:1.4 --name agent-1-4 \\
        --managed-identity system
    python -m sandbox_mux.image commit --sandbox-id <id> --name agent-primed
    python -m sandbox_mux.image list
    python -m sandbox_mux.image public

`build` and `commit` print the new image's id as JSON. Put it in the config as `pool.disk_id`
(or SANDBOX_MUX_DISK_ID) and roll the service; new sandboxes boot from it and the reaper
leaves existing ones alone. It reads the `azure:` section of the config file for the group.
Registry passwords are read from an environment variable, never from the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

from .config import Config


def _client(cfg: Config):
    from azure.identity import DefaultAzureCredential
    from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

    a = cfg.azure
    return SandboxGroupClient(
        a.endpoint or endpoint_for_region(a.region), DefaultAzureCredential(),
        subscription_id=a.subscription_id, resource_group=a.resource_group,
        sandbox_group=a.sandbox_group)


def _summary(img: Any) -> dict:
    return {"id": img.id, "name": getattr(img, "name", None),
            "state": img.status.state if getattr(img, "status", None) else None,
            "base": img.image.base if getattr(img, "image", None) else None,
            "size_in_mb": getattr(img, "size_in_mb", None)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandbox_mux.image", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", help="config file (default: $SANDBOX_MUX_CONFIG or config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build a disk image from an OCI container image")
    b.add_argument("--image", required=True, help="e.g. myregistry.azurecr.io/agent:1.4")
    b.add_argument("--name", required=True)
    auth = b.add_mutually_exclusive_group()
    auth.add_argument("--managed-identity", metavar="system|CLIENT_ID",
                      help="pull with the group's managed identity (no secret)")
    auth.add_argument("--registry-user", help="username; the password comes from --registry-token-env")
    b.add_argument("--registry-token-env", default="REGISTRY_TOKEN",
                   help="environment variable holding the registry token (default REGISTRY_TOKEN)")
    b.add_argument("--timeout", type=int, default=900, help="seconds to wait for Ready")

    c = sub.add_parser("commit", help="save a running sandbox's filesystem as a disk image")
    c.add_argument("--sandbox-id", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--timeout", type=int, default=1200)

    sub.add_parser("list", help="list the group's private disk images")
    sub.add_parser("public", help="list public image names usable as pool.disk")
    return p


def main(argv: list[str] | None = None, *, client_factory: Callable[[Config], Any] | None = None,
         environ: dict[str, str] | None = None, out=None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    out = out or sys.stdout
    path = args.config or env.get("SANDBOX_MUX_CONFIG", "config.yaml")
    if not os.path.exists(path):
        print(f"error: config file {path} not found (use --config)", file=sys.stderr)
        return 2
    cfg = Config.load(path)
    client = (client_factory or _client)(cfg)

    if args.command == "build":
        creds = None
        if args.registry_user:
            token = env.get(args.registry_token_env)
            if not token:
                print(f"error: ${args.registry_token_env} is not set", file=sys.stderr)
                return 2
            from azure.containerapps.sandbox import RegistryCredentials
            creds = RegistryCredentials(username=args.registry_user, token=token)
        img = client.begin_create_disk_image(
            args.image, name=args.name, registry_credentials=creds,
            managed_identity_client_id=args.managed_identity, polling_timeout=args.timeout).result()
        print(json.dumps(_summary(img)), file=out)
        return 0 if (img.status and img.status.state in ("Ready", "Succeeded")) else 1
    if args.command == "commit":
        img = client.get_sandbox_client(args.sandbox_id).begin_commit(
            name=args.name, polling_timeout=args.timeout).result()
        print(json.dumps(_summary(img)), file=out)
        return 0
    if args.command == "list":
        print(json.dumps([_summary(i) for i in client.list_disk_images()], indent=2), file=out)
        return 0
    print(json.dumps([i.name for i in client.list_public_disk_images()], indent=2), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
