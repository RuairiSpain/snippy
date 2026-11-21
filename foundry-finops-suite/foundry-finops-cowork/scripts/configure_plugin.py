"""Point the plugin at your deployed MCP server and OAuth registration, bump the version, and zip it.

    python scripts/configure_plugin.py --mcp-url https://finops-mcp.contoso.com/mcp \
        --oauth-reference-id <Teams Developer Portal OAuth client registration ID> --out foundry-finops-plugin.zip

Then validate with Work IQ Dev Tools (``wiqd plugin validate``) or Agents Toolkit (``atk validate --package-file``)
before uploading. The three IDs are different: the app manifest ID, the Entra app (client) ID and the OAuth client
registration ID – only the last goes here.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "plugin" / "appPackage"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-url", required=True)
    ap.add_argument("--oauth-reference-id", required=True)
    ap.add_argument("--out", default="foundry-finops-plugin.zip")
    ap.add_argument("--no-bump", action="store_true")
    a = ap.parse_args()
    if not a.mcp_url.startswith("https://"):
        raise SystemExit("The MCP URL must use HTTPS")
    if re.search(r"REPLACE|PLACEHOLDER|<|>", a.oauth_reference_id, re.I) or len(a.oauth_reference_id) > 128:
        raise SystemExit("Pass the real OAuth client registration ID from Teams Developer Portal")
    manifest_path = PKG / "manifest.json"
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    remote = m["agentConnectors"][0]["toolSource"]["remoteMcpServer"]
    remote["mcpServerUrl"] = a.mcp_url
    remote["authorization"] = {"type": "OAuthPluginVault", "referenceId": a.oauth_reference_id.strip()}
    if not a.no_bump:
        major, minor, patch = (int(x) for x in m["version"].split("."))
        m["version"] = f"{major}.{minor}.{patch + 1}"
    manifest_path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(PKG.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(PKG).as_posix())
    print(f"Wrote {a.out} (version {m['version']})")


if __name__ == "__main__":
    main()
