# Foundry FinOps – SDK, dashboard and Microsoft 365 Copilot plugin

| Project | Purpose | Start here |
|---|---|---|
| `foundry-finops/` | Publishable Python package (`pip install foundry-finops`) wrapping the REST APIs for Foundry cost controls, Cost Management, Azure Monitor, Log Analytics, quota, tags, alerts and API Management AI-gateway policies, plus OpenTelemetry usage tracking | `foundry-finops/README.md` |
| `foundry-finops/examples/dashboard/` | Example web dashboard in the style of the Azure portal and Log Analytics, with CRUD pages, charts and downloads | `python foundry-finops/examples/dashboard/server.py` |
| `foundry-finops-cowork/` | Separate project: MCP connector (Entra sign-in + on-behalf-of) and a Cowork plugin with report and governance skills | `foundry-finops-cowork/docs/SETUP.md` |

Everything runs in demo mode without Azure (illustrative data). Tests use only the standard library plus `requests`,
`PyJWT` and `cryptography`:

```bash
cd foundry-finops && PYTHONPATH=src python -m unittest discover -s tests -t .
cd ../foundry-finops-cowork && PYTHONPATH=.:../foundry-finops/src python -m unittest discover -s tests -t .
```
