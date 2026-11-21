# Set up the Foundry FinOps connector for Microsoft 365 Copilot (Cowork)

The plugin has two parts:

* **MCP server** (`mcp_server/`) – a small HTTPS service you host. It validates the user's Microsoft Entra token and
  calls Azure REST APIs **on behalf of that user** through the `foundry-finops` SDK.
* **Plugin package** (`plugin/appPackage/`) – manifest, two skills, and the connector's captured tool list.

```
Cowork ──(user signs in, OAuth via Teams Developer Portal)──► MCP server /mcp
                                                              │ validate JWT (aud = api://<client-id>)
                                                              │ on-behalf-of exchange
                                                              ▼
                      Azure Resource Manager · Cost Management · Azure Monitor · Log Analytics · API Management
```

Because of on-behalf-of, each user sees only the subscriptions, budgets and cost data their own Azure roles allow.

## 1. Register the Entra app (one app protects the server and performs OBO)

1. **Entra ID → App registrations → New registration**: `Foundry FinOps MCP`, single tenant.
2. **Expose an API**: set the Application ID URI to `api://<client-id>` and add the scope `access_as_user`
   (admins and users can consent).
3. **API permissions → Add (delegated)**:
   * *Azure Service Management* → `user_impersonation` (Resource Manager, Cost Management, Monitor, APIM)
   * *Log Analytics API* → `Data.Read` (only if you use `run_kql_query`)
   * Grant admin consent.
4. **Authentication → Add a platform → Web**, redirect URI
   `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`.
5. **Credentials for OBO** – prefer no secret:
   * Recommended: give the Container App a user-assigned managed identity and add a **federated credential** on the
     app registration for that identity; set `OBO_USE_MANAGED_IDENTITY=1` and `OBO_MANAGED_IDENTITY_CLIENT_ID`.
   * Otherwise create a client secret and store it in Key Vault, referenced as `MCP_CLIENT_SECRET` – never in code,
     the manifest or the repo.
6. The Teams Developer Portal OAuth registration (step 3) also needs a client secret for the sign-in code exchange;
   create a separate secret for it and enter it only in Developer Portal.

## 2. Deploy the MCP server

```bash
az containerapp up -n finops-mcp -g rg-ai --source . --ingress external --target-port 8000 \
  --env-vars AZURE_TENANT_ID=<tenant> MCP_CLIENT_ID=<client-id> MCP_PUBLIC_BASE_URL=https://<fqdn> \
             OBO_USE_MANAGED_IDENTITY=1 OBO_MANAGED_IDENTITY_CLIENT_ID=<mi-client-id> \
             FINOPS_SUBSCRIPTION_ID=<sub> FINOPS_RESOURCE_GROUP=rg-ai FINOPS_FOUNDRY_ACCOUNT=<foundry> \
             FINOPS_APIM_SERVICE=<apim> FINOPS_LOGS_TARGET=<app-insights-resource-id> FINOPS_LOCATION=eastus2
```

Optional: `FINOPS_READ_ONLY=1` hides every write tool; `MCP_ALLOWED_TENANTS` for multi-tenant use.
Check `https://<fqdn>/healthz` and `https://<fqdn>/.well-known/oauth-protected-resource`.

## 3. Create the OAuth client registration (Teams Developer Portal)

**Developer Portal → Tools → OAuth client registration → New**

| Field | Value |
|---|---|
| Base URL | exactly the MCP URL, e.g. `https://<fqdn>/mcp` |
| Restrict usage by app | Any Teams app |
| Client ID / secret | the Entra app's client ID and the sign-in secret |
| Authorization endpoint | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize` |
| Token endpoint / refresh endpoint | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` |
| Scope | `api://<client-id>/access_as_user offline_access` |
| PKCE | Enabled |

Copy the generated **OAuth client registration ID**.

## 4. Finish and package the plugin

```bash
python scripts/configure_plugin.py --mcp-url https://<fqdn>/mcp --oauth-reference-id <registration-id> \
    --out foundry-finops-plugin.zip
```

If you change tools, recapture the tool description from the running server first:

```bash
python scripts/capture_tools.py --url https://<fqdn>/mcp --token "$(az account get-access-token \
    --scope api://<client-id>/access_as_user --query accessToken -o tsv)" \
    --out plugin/appPackage/tools/connector-tools.json
```

Validate (`wiqd plugin validate` or `atk validate --package-file foundry-finops-plugin.zip`), then upload in Cowork
or share with `wiqd plugin share`. Organization-wide sharing and MCP registration need admin approval.

## 5. Give users Azure access

Users need roles on the scopes they query, for example:

| Feature | Role |
|---|---|
| Cost breakdown, budgets (read) | Cost Management Reader (subscription or resource group) |
| Token metrics, alerts (read) | Monitoring Reader |
| Cost controls (read) / KQL | any Foundry role / Log Analytics Reader |
| Create cost controls | Foundry Owner or Account Owner (resource), Foundry Project Manager (project) |
| Budgets (write) | Cost Management Contributor |
| Token limits, products | API Management Service Contributor |
| Deployment capacity, tags | Contributor / Tag Contributor |

## Local testing without Azure

```bash
PYTHONPATH=.:../foundry-finops/src MCP_AUTH=none FINOPS_DEMO=1 python -m mcp_server.app --port 8000
python scripts/capture_tools.py --url http://127.0.0.1:8000/mcp --out /tmp/tools.json
```

`MCP_AUTH=none` refuses to start on anything but a loopback address.
