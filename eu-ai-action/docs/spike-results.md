# M1 spike results

Status: **NOT RUN.** These checks need the real dev tenant and credentials, which were not available when the
code was written. Everything marked [verify] in `IMPLEMENTATION_PLAN.md` is still an assumption.

How to run: fill `.env` (TARGET_TENANT_ID, COLLECTOR_CLIENT_ID, COLLECTOR_CLIENT_SECRET, DATAVERSE_ENVIRONMENT_URL,
and the two endpoint paths once known), then `python spikes/run_spikes.py --record`. Paste the table below.

| Check | Result | Detail |
|---|---|---|
| dataverse: list bots (app-only) | not run | |
| dataverse: greeting topic text | not run | |
| dataverse: channels and authentication mode | not run | |
| graph: agent registry | not run | |
| purview: audit and labels | not run | |
| minimum permission set and admin-consent steps | not run | |
| compliance manager: write-back API | not run | |

Decision rule from the plan: if Dataverse cannot be read with an app-only token, stop and re-plan before more work on the
collector. If the registry or Purview is unavailable, continue with Dataverse-only facts; the dependent fields stay human input.
