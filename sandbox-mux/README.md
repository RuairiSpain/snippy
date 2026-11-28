# sandbox-mux

A session manager that serves many users from a bounded pool of Azure Container Apps
Sandboxes (`Microsoft.App/sandboxGroups`). It adds the parts of Foundry hosted agent
sessions that the raw sandbox API does not provide: per-user identity and isolation,
conversation state, idle handling, routing, and telemetry.

Instead of one sandbox per user, each sandbox hosts several users. Each user gets a Unix
account with a private `$HOME`. Pool size, users per sandbox and placement strategy are in
a config file. All shared state is in Redis, so any number of replicas can serve requests.

```
client ──Entra token──▶ replica 1 ─┐
                        replica 2 ─┼─▶ Redis (placement, locks, history)
                        replica N ─┘        │
                                            ▼
                   ┌──────────── sandbox group ────────────┐
                   │ sandbox A: u1 u2 u3   sandbox B: u4 u5 │  (max_sandboxes, users_per_sandbox)
                   └─────────────────────────────────────────┘
```

## Is multiplexing users onto shared sandboxes possible?

Yes, and this project does it. Three constraints shape the design.

**1. `$HOME` separation alone is not isolation.** Users who share a Unix UID can read each
other's directories regardless of `$HOME`. `isolation.mode: unix_user` (the default)
therefore creates one Unix account per user (`useradd`, home mode `0700`) and runs every
turn as that account with `runuser` and a cleared environment (`env -i`). `home_only` mode
only sets `$HOME`; use it for tests or for users who fully trust each other.

**2. Users on one sandbox still share a kernel, network identity and egress policy.**
Unix accounts stop file and process snooping between users, but they are not a hypervisor
boundary like the one between sandboxes. A kernel exploit, a noisy neighbour exhausting CPU
or memory, or a user abusing a shared egress credential affects everyone on that sandbox.
The sandbox egress policy and any injected credentials apply to the whole sandbox, so you
cannot give two users on one sandbox different network permissions. Multiplex only users
who share a trust level. For separate trust levels, run one pool per tenant (a different
`pool.name`, optionally a different sandbox group).

**3. A user's files live on one sandbox, so routing is sticky.** A request cannot go to
"any idle sandbox", because the user's `$HOME` exists only where it was created. A user is
bound to a sandbox when they first need a slot and stays bound until they are idle longer
than `session.user_idle_ttl_seconds` (or call `DELETE /v1/me/session`). The binding is
released and the home directory deleted at that point. Load balancing therefore happens at
binding time (`pack` or `spread`), not per request. If a user's sandbox disappears, they
are rebound to another one with an empty home; conversation history survives because it is
in Redis. Making homes durable would need a shared volume or snapshots, which this
project does not implement.

## How placement works

All of it runs in Lua scripts in `store.py`, so replicas cannot disagree:

* **assign**: returns the user's existing sandbox; otherwise picks a ready sandbox with a
  free slot (`pack`: fullest first, which minimizes the number of sandboxes; `spread`:
  least loaded first); otherwise registers the user as a waiter.
* **create**: a replica is told to create a sandbox only when waiting users exceed what the
  sandboxes already being created can absorb, and the count of existing plus in-flight
  sandboxes is below `max_sandboxes`. A burst of 20 users with 5 slots per sandbox creates
  4 sandboxes, not 20.
* **full**: waiting users poll for `session.queue_timeout_seconds`, then get HTTP 429 with
  `Retry-After`.
* **per-user lock**: one turn per user at a time (HTTP 409 otherwise), held across replicas
  with a token, so a turn and an eviction cannot interleave.

The reaper runs in every replica but only the replica holding the Redis `leader` lock acts.
It evicts idle users (wiping their home), deletes sandboxes that stayed empty for
`pool.empty_ttl_seconds` while keeping `pool.min_sandboxes`, keeps the warm pool filled,
and reconciles Redis with the real sandbox list (forgets sandboxes that vanished, deletes
orphans left by a replica that crashed mid-creation).

Idle compute cost is handled by the platform: each sandbox is created with
`auto_suspend_seconds` and `auto_suspend_mode`, and `ensure_running` resumes it on demand.
A shared sandbox only suspends when none of its users is active.

## Triggers: run agents when connector events arrive

**Off by default.** Nothing is exposed until a developer sets `triggers.enabled: true` (or
`SANDBOX_MUX_TRIGGERS_ENABLED`) in the config, and running *as the user while they are away* is a second,
separate switch, `triggers.delegation.enabled`. With triggers off, none of the routes below exist.

```
connector (Outlook, Teams, ...) -> connector namespace trigger -> POST /v1/triggers/callback/<source>
   -> normalize + deduplicate -> subscriptions whose filters match -> queued runs
   -> workers run the agent in the subscription owner's own sandbox user
```

**Two roles.**
* **An admin** registers *sources* in `triggers.sources` (a connector, a connection, an operation) and creates
  the matching trigger on the connector namespace. The service never calls the namespace itself, so your pipeline
  stays in control of what is subscribed: `python -m sandbox_mux.triggers_cli arm --source mailbox-support
  --callback-base-url https://agents.example.com` prints the `triggerConfigs` body (from Microsoft's
  Azure/Connectors documentation, API `2026-05-01-preview`) and the `az rest` command. The namespace's managed
  identity also needs access to the connection, and its object ID goes in `triggers.callback_principal_ids`.
  Users cannot point a trigger at an arbitrary connection, so they cannot read other people's mailboxes.
* **Users and code** subscribe to a source with filters and an instruction:

```bash
curl -X POST $HOST/v1/triggers -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d '{
  "name": "partner invoices", "source": "mailbox-support",
  "filters": {"from": ["@partner.com"], "subject_contains": ["invoice", "overdue"], "importance": ["high"]},
  "instruction": "Summarise the email and list the amounts due.",
  "delegated_resources": []}'
# also: GET /v1/approvals, POST /v1/approvals/{id}/approve|reject, GET /v1/triggers/sources, GET /v1/triggers, GET /v1/triggers/{id}, POST /v1/triggers/{id}/enable|disable,
#       DELETE /v1/triggers/{id}, GET /v1/triggers/{id}/runs
```

Filters: every key must match, and any one value of a key is enough. `from` matches the sender's real address
(`bob@partner.com`) or a domain written with the at-sign (`@partner.com`, not a bare domain; subdomains do not match).
The display name is ignored, because the sender chooses it. A `from` filter is still only as trustworthy as the
mail system's sender check (a forged From header passes unless SPF, DKIM and DMARC reject it), so it is a relevance
filter, not a security boundary; for Teams, `from` is the sender's id, not their name; `<field>_contains` is
a case-insensitive substring; anything else is an exact match. There are no regular expressions, so a user cannot
write a pattern that stalls the callback. The owner's own messages never trigger their subscriptions unless
`include_own_messages` is set, which stops reply loops.

**What the callback does.** It accepts only a token from the namespace's managed identity: the right tenant, an
object ID in `callback_principal_ids`, an application token (no `scp`). A user token is refused there, and
the namespace's token is refused as a user. It also needs the source's own secret (next paragraph). It caps the
body size, drops events it has already seen, caps how many runs one event can start, acknowledges quickly, and leaves
the work to the queue. A run left by a crashed worker is retried once and then failed, and a busy user or pool is
retried a few times. Per user there is an hourly run limit and a cap on subscriptions, and a subscription that fails
`max_consecutive_failures` times in a row is switched off.

**A secret for each source.** The namespace's identity is one identity for every trigger on it, so on its own it
would let anyone who can create a trigger there call any source. Each source therefore also has a secret in its
callback URL (`?k=...`), derived with HMAC from one key in `TRIGGER_CALLBACK_KEY` (nothing is stored; the key can list
several, comma-separated, to rotate: the first signs, all verify). A secret for one source does not work for another,
and a wrong or unknown source gets the same 403, so callers learn nothing about which sources exist. `triggers_cli arm`
puts the right secret in the URL it prints; treat that output as a credential. The URL is readable by whoever can read
the trigger on the namespace, so this narrows who can inject events to those people, it does not make the namespace's
readers untrusted. The service will not start without the key unless `require_source_secret: false`.

**A fair queue.** Waiting runs are kept per user, and workers take them in rotation, so a user whose source is
flooded gets one run per turn like everyone else instead of everyone waiting behind them. At most one run per user is
in flight. Caps bound what a flood can queue: `max_queued_per_user` (20), `max_queue_depth` (1000) and, optionally,
`max_concurrent_runs` across all replicas. A dropped run is recorded as skipped with the reason. In-flight work has a
lease, so a worker that dies cannot hold a slot. `GET /v1/pool/status` shows the waiting and running counts.

**Unattended runs are read-only; a person approves anything that sends or changes something.**
* `triggers.delegated_aliases` is the only list of `obo.resources` aliases an unattended run may use as the user, and
  it is required when delegation is on. Put read-only scopes there (for example Mail.Read, not Mail.Send). A run that
  cannot obtain a write scope cannot send or change anything, whatever an email tells it. Consent, subscriptions and
  token fetches are all refused for any other alias.
* When the agent wants to act it returns `proposed_actions` instead (`propose()` in `runner/run_turn.py`): a short
  `kind`, a readable `summary` and a `payload`. The service cleans them (hidden characters removed, sizes and counts
  capped) and stores them for the subscription's owner: `GET /v1/approvals`, `GET /v1/approvals/{id}`.
* `POST /v1/approvals/{id}/approve` with `{"obo_resources": ["graph-write"]}` runs the action **now, in the owner's
  interactive session, with their live token**, so the person is present and no stored credential is used.
  The executing agent gets it as `event["approved_action"]`, marked as coming from an automated run that read untrusted
  content. `POST /v1/approvals/{id}/reject` runs nothing. An approval runs at most once, only its owner can decide,
  it expires after `ttl_days`, and a failed action is recorded and not retried. Only triggered runs create proposals.

A reviewer is still deciding on text an attacker may have shaped through the agent. Make summaries show the
concrete effect (who gets what), and keep the payload small enough to read.

**The event is untrusted text, and an agent that reads someone else's email is a prompt-injection target.**
The event reaches the agent in its own `event` field, never inside the instruction, with a note saying it is
untrusted (`event(request)` in `runner/run_turn.py`). Text is stripped of control and invisible formatting
characters (zero-width and bidirectional marks are used to hide instructions) and length-capped. Structure helps
but does not make a model immune. For unattended runs, give the agent as little as it needs: no write-capable
tools, no delegated resources unless required, and a human approval step before anything is sent or changed.

**A separate pool for triggered runs.** Triggered work runs in its own pool by default, because a run started
by text from outside must not share a sandbox with interactive sessions:

```yaml
triggers:
  pool:
    name: triggers              # its own sandboxes, label and Redis namespace
    settings:
      max_sandboxes: 3
      egress: {mode: allowlist, allow_hosts: ["workiq.svc.cloud.microsoft"]}
    limits: {mode: cgroup, cpu: 0.5, memory: 1Gi, pids: 128}      # optional
```

* **Different machines, so different homes.** A user's triggered run and their interactive session never share a
  sandbox, a Unix account or a home directory. An injected run cannot read the user's interactive files, plant
  instructions the interactive agent will later trust, or reach another interactive user's processes, localhost
  services or tokens: those are on other sandboxes, behind the hypervisor boundary.
* **Safe by default.** The triggers pool inherits sizes, the image, timeouts and warm-up from `pool`, but **not** the egress
  policy (it starts as deny-all) and **not** the connectors (none). You add only what triggered runs need. Per-user limits
  are inherited unless you set `triggers.pool.limits`. With delegation on, deny-all is refused at startup, because a
  delegated run must be able to reach the API: allow that one host.
* **Separate capacity.** A flood of events cannot take interactive slots, and a full interactive pool does not stop
  triggered runs. Each pool has its own per-user lock, so a running interactive turn does not block a triggered one.
  The queue is fair (below).
* **One deployment.** Both pools run in the same service with their own maintenance loop, idle eviction, egress and
  connector reconciliation, warm pool and retirement. `DELETE /v1/me/session` removes the user from both pools.
  `GET /v1/pool/status` (admins) shows both. `triggers.pool.separate: false` shares the interactive pool instead.
* **What is still shared:** the sandbox group, its managed identity and its connector attachments (so keep the identity's
  roles minimal; the connector set is chosen per pool), the quota (`maxSandboxCount` on the group must cover both pools'
  `max_sandboxes`), Redis, the file store and the delegation vault.

Choosing the egress profile for the triggers pool. All three keep the agent off the open internet:
connector tools only (allow the connector gateway host); plus Work IQ as the user (add `workiq.svc.cloud.microsoft`);
plus another Azure service (add that one host, and keep the identity's roles minimal). Whether traffic to the connector
gateway needs an allow rule is not documented: with a deny policy, call the connector's runtime URL from inside a
triggers-pool sandbox and check its egress decisions, then allow only what the audit shows. Raw TCP and UDP may not be
covered by a deny policy unless you set `egress.block_non_http`; test that before treating the pool as sealed.

**Running as the user while they are away (option C).** A trigger fires with no signed-in user, so there is no token
to exchange. With `triggers.delegation.enabled`, a user consents once and the service keeps an encrypted refresh token:

1. `POST /v1/triggers/consent/start {"resource": "graph-read"}` returns a Microsoft sign-in URL for one resource alias
   from `triggers.delegated_aliases`. After the user signs in and consents, Microsoft redirects the browser to
   `/v1/triggers/consent/callback`; the one-time `state` ties that to the user who started it, and the account that
   signs in must be that same user or nothing is stored.
2. A subscription with `"delegated_resources": ["graph"]` is accepted only for resources the user consented to.
3. At run time the service gets an access token silently from the stored refresh token, checks it is that user's
   delegated token (same check as OBO), hands it to the agent through the usual root-owned file, and deletes the file
   after the run. Rotated refresh tokens are saved back.
4. `GET /v1/triggers/consent` shows what is on file; `DELETE /v1/triggers/consent` deletes it and switches off every
   delegated subscription. Consent expires after `max_lifetime_days` (default 30), and so do the subscriptions that use
   it. If Microsoft stops honouring the token (revoked, password change, Conditional Access), the run fails, the
   subscription is switched off with a reason, and the user consents again.

This is the most sensitive thing in the project, because it keeps a long-lived credential for each user who opts in.
What limits it: it is off unless a developer enables it; it needs the user's explicit consent per resource; the token
cache is encrypted with a Fernet key from the environment (`TRIGGER_TOKEN_KEY`, comma-separated for rotation; generate
one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep it in
Key Vault, not in Redis or the config file); nothing is logged or returned; and it expires. The service will not start
with delegation enabled and no key. Work IQ does not allow application-only access, so this is the only way for an
unattended run to use it. Register the redirect URI on the app registration (web platform), with the delegated
permissions for each resource and `offline_access`.

**Not verified. Please test before relying on any of these:**
* **Callback payloads.** The platform documentation does not describe what each connector sends to the callback. The
  default field mappings for `email` (`from`, `subject`, `bodyPreview`, ...) and `teams_message` are guesses based on the
  Office 365 and Teams connector shapes. Capture a real delivery and set `fields`, `unwrap` or `items_path` per source
  (`generic` has no defaults).
* **Namespace trigger creation, the access policy, and the managed-identity callback.** The body printed by `triggers_cli`
  follows the documented shape but has not been run against a namespace. Polling triggers poll about every three minutes.
* **The consent flow and silent token refresh against real Microsoft Entra.** Tested with a fake MSAL client and a real
  token cache, not against a tenant. Whether Work IQ accepts tokens obtained from a refresh token is not documented.
* **Connections for each user.** Sources are shared: an admin registers a mailbox or channel, and filters decide who is
  interested. Per-user connections (a user consenting to their own mailbox) would add a connection-consent flow that I have
  not verified, so they are not built.
* **Results go nowhere but the run record.** `GET /v1/triggers/{id}/runs` shows the status, reply and any output files.
  Sending a reply back to Teams or email would need a write-capable connector, which is deliberately not wired in.

## Images, the warm pool, and cold start

**Is this Docker?** Partly. A sandbox boots from a *disk image*, which is its root filesystem, and
there are three ways to get one (from the platform docs):

| Source | How | Use it for |
|---|---|---|
| Public image | `pool.disk: python-3.12` (also `ubuntu`, `node-24`, `copilot`, `claude`, ...) | a stock runtime, maintained by Microsoft |
| **Custom image from a container image** | build from any OCI image in Docker Hub or a registry (ACR), then `pool.disk_id` | **your agent runtime and dependencies, baked in** |
| Committed image | `commit` a running sandbox you set up by hand | setup that is easier to do interactively |

So you write a normal Dockerfile, push it to a registry, and the platform converts it to a disk image
(private registries with a managed identity or a username and token). What runs is a microVM that
boots that filesystem, not a Docker or Container Apps container. I have not verified how Foundry hosted
agents build theirs, so I won't claim the two are the same.

**Baking in the runtime (recommended).**

```bash
docker build -t myregistry.azurecr.io/agent:1.4 -f runner/Dockerfile runner && docker push myregistry.azurecr.io/agent:1.4
python -m sandbox_mux.image build --image myregistry.azurecr.io/agent:1.4 --name agent-1-4 --managed-identity system
# -> {"id": "<disk image id>", "state": "Ready", ...}
SANDBOX_MUX_DISK_ID=<disk image id>        # set it on the service and roll it
```

`runner/Dockerfile` is a starting point (Python, your `requirements.txt`, bytecode compiled once as
root, `/opt/agent` not writable by users). Run the build from CI on each release: it takes minutes
(default wait 15 minutes). Other commands: `commit --sandbox-id ...`, `list`, `public`. Registry passwords
come from an environment variable (`--registry-token-env`), never the command line. Changing
`pool.disk_id` affects only new sandboxes; existing ones keep the old image until they are deleted
(they are replaced as they go idle; there is no forced rollout for images, unlike connectors).

**What the warm pool does.** Starting a sandbox costs a boot plus the setup script. The warm pool does
that ahead of time so a user rarely waits:

* `pool.min_sandboxes` keeps that many sandboxes created and set up even when nobody is using them.
* `pool.min_free_slots` keeps that many *free user slots* across ready sandboxes, so a burst of new
  users lands on an already-booted sandbox. Empty sandboxes are not deleted while they are needed for
  this floor. It never exceeds `max_sandboxes`.
* Idle warm sandboxes suspend after `auto_suspend_seconds` and resume on use (with
  `auto_suspend_mode: Memory`, memory is preserved), so keeping them costs little. Resuming is
  documented as sub-second, which I have not measured.
* `pool.warmup_command` runs once as root in each new sandbox, after setup and before it takes users
  (argv, not a shell string). Use it to import heavy modules, compile bytecode, or read model files:
  the file cache and bytecode are shared by every user on the sandbox. It does not share a running
  process between users, so it speeds cold starts, not per-user memory. A failing warm-up deletes the
  sandbox and it is never registered.
* Creation failures (a bad image id, a quota) stop the warm-up for that sweep and are retried at the
  next one, instead of looping.

**Not snapshots.** Creating from a snapshot cannot set labels, egress policy, connectors or ports, and
this service needs those, so a disk image is the template to use.

Not verified: that a sandbox built from your OCI image boots, keeps running, and lets the service run
commands as root with `useradd`, `runuser` and cgroups available; the first sandbox will tell you
(the setup script fails fast and says what is missing). Build time and image size limits, and
registry-pull time, are not documented in what I found.

## Connectors and MCP servers

Container Apps Sandboxes connectors give a sandbox access to services such as SharePoint, Office
365, Teams, GitHub or Jira, or to an MCP server, without OAuth flows or secrets inside the sandbox.
You choose which ones every sandbox in the pool starts with:

```yaml
connectors:
  available:                       # optional short names
    sharepoint: /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Web/connectorGateways/<ns>/mcpserverconfigs/office365Mcp
  attach: [sharepoint]             # names or full resource IDs, up to 10
```
```bash
SANDBOX_MUX_CONNECTORS=sharepoint,github        # names from `available`, or full IDs, comma separated
```

How the platform works (from its docs): a connector is first **attached to the sandbox group**
(`aca sandboxgroup connector add --connection-id <id> --authorization system`, and the group needs an
identity). Each sandbox then **opts in when it is created**, with up to 10 resource IDs that must
already be on the group, and **the list cannot be changed afterwards**. This service passes
`connections=[...]` on every sandbox it creates; it does not attach connectors to the group, which you
do once with the CLI or the portal. MCP connector IDs end in `/mcpserverconfigs/<name>` and API
connector IDs in `/connections/<name>`; IDs are validated at startup. If an ID is not on the group the
service rejects sandbox creation, which shows in the logs and as HTTP 429 to users.

**Because the list is fixed at creation, changing it replaces sandboxes.** Each sandbox is recorded
with the list it was created under. When the configured list changes, the reaper leader retires the
others: no new users are placed on them, users already there keep working, and a retired sandbox is
deleted as soon as it is empty. After `retire_grace_seconds` (default 1 hour) it is deleted even if
users are on it; they are placed on a new sandbox at their next request with an empty home (history
is kept). A turn running at that moment fails and is retried once on the new sandbox, so the agent
can run twice. The warm pool is rebuilt with the new list. This also means **removing a connector
from the config removes access**, within the grace period, not only for new sandboxes.

**Read this before attaching anything.** With `--authorization system`, a connector acts as the
sandbox group's identity, using the account that granted consent, **not as the calling user**. Every
user on every sandbox in the pool gets the same access, and sandboxes in a pool cannot have different
connectors per user. Attach only what the whole pool is allowed to use, or run one pool (one service
instance, one `pool.name`) per set of users. For access as the signed-in user, use On-Behalf-Of
instead; the two combine (connectors for shared services, OBO for user-specific data).

Making the tools visible to your agent: for Copilot disks the platform writes an MCP client config
into the sandbox (documented as `/root/.copilot/mcp-config.json`). That file is under root, so the
user's process cannot read it. If you set `connectors.config_file` to its path, root copies it into
the user's root-owned request directory on each turn (read-only for the user) and the agent finds it at
`$MCP_CONFIG_FILE` (`mcp_config()` in `runner/run_turn.py`). Requires `unix_user`.

Not verified, because none of it can be tested without a real group and connector:
* How non-root users inside a sandbox reach the connector endpoints (the docs describe access through
  the group's managed identity but not through which mechanism), and whether the egress policy
  (`pool.egress`) must allow them. With `egress.mode: disabled` they may be blocked.
* Whether non-Copilot disks write an MCP config file at all.
* Which tools a connector exposes and whether you can limit them per user. The service has a
  per-connection policy-rules endpoint, which this project does not use.
* The retirement logic is tested with the in-memory backend, not against the service.

## Files: upload inputs, download outputs

Sandboxes are temporary (they are deleted when empty, drained, or lost), so files are kept in
durable storage that the service owns, and a sandbox only holds a copy for the length of one
turn. Enable with `files.enabled: true` (requires `isolation.mode: unix_user`).

```bash
# upload (raw body; the name is a query parameter). 413 if over the limit, 409 over quota.
curl -X POST "$HOST/v1/files?name=data.csv" -H "Authorization: Bearer $T" \
     -H "Content-Type: text/csv" --data-binary @data.csv          # -> {"file_id": "...", ...}

# use it in a turn; results come back in the response
curl -X POST $HOST/v1/agent/invoke -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
     -d '{"prompt": "summarise it", "inputs": ["<file_id>"]}'
# -> {"reply": "...", "outputs": [{"file_id": "...", "name": "summary.md", "size": 912, "sha256": "..."}],
#     "skipped_outputs": []}

curl -O -J "$HOST/v1/files/<file_id>" -H "Authorization: Bearer $T"      # download
# also: GET /v1/files, GET /v1/files/{id}/metadata, DELETE /v1/files/{id}
```

**Inside the sandbox**, the agent finds attached files in `$INPUT_DIR` (also listed as
`input_files` in the request JSON) and writes anything it wants to return into `$OUTPUT_DIR`
(`$HOME/outputs`). Inputs are read-only and removed after the turn. After a successful turn the
service collects the regular files in `$OUTPUT_DIR`, stores them, and empties the directory, so
each turn returns only its own files.

**Size limits (all in YAML under `files:`, or env):**

| Setting | Env | Default |
|---|---|---|
| `max_file_size` per file, uploaded or produced | `SANDBOX_MUX_MAX_FILE_SIZE` | `100MB` |
| `max_user_total_size` per user | `SANDBOX_MUX_MAX_USER_STORAGE` | `2GB` |
| `retention_days` | `SANDBOX_MUX_FILE_RETENTION_DAYS` | `7` |
| `max_files_per_user`, `max_input_files_per_turn`, `max_output_files_per_turn` | generic form | 500, 10, 20 |
| `max_output_size_per_turn` (archive size) | generic form | `200MB` |

`100MB` is 100,000,000 bytes; `100MiB` is 104,857,600. Any setting can also use
`SANDBOX_MUX__FILES__<KEY>`. The limit is enforced while the body streams, so an oversized upload
is refused (413) without being stored, and a quota is reserved before the upload starts, so
concurrent uploads cannot overshoot it. Whole files are held in memory while moving to or from a
sandbox (`max_concurrent_transfers` bounds how many at once), so raise `max_file_size` together
with the service's memory. I found no documented size limit for the sandbox file API: test
your largest file size before relying on it.

**Why files never live in the sandbox.** Besides durability, there is a security reason. The
data-plane file calls and the setup scripts run as root, and a user can replace anything under
their own home with a symlink. So root never reads, writes or changes ownership of a path the
user controls:

* Requests, delegated tokens and input files are placed in `/var/lib/smux/{req,in}/<user>/`,
  owned by root with read access for the user's private group only. The user's process can read
  them but cannot modify, replace or delete them.
* Outputs are archived by a process running **as the user**, so the kernel applies that user's
  permissions to whatever a link points at. Only regular files are listed (`find -type f` skips
  symlinks), and the archive is written into a root-owned directory by the manager's shell.
* The archive is untrusted: it is read in memory and never extracted to disk. Only plain files
  with safe relative paths are accepted; links, absolute paths and `..` are skipped and reported
  in `skipped_outputs`. Output names are flattened to one component and made unique.
* Downloads are always `attachment` with `nosniff` and `Content-Security-Policy: sandbox`, so agent
  output is never rendered in the service's origin. Other users' files return 404.

**Retention and cleanup.** The reaper deletes files after `retention_days` and returns the
quota of uploads that never finished. `infra/main.bicep` (`enableFiles`) also creates the
storage account (no public access, no shared keys), the container, a lifecycle rule as a
backstop, and the *Storage Blob Data Contributor* role for the service identity.

**Not in this version:**
* **Direct-to-storage upload links.** Files go through the service so the size limit and quota
  can be enforced exactly (a storage upload link cannot cap the size). Large files cost service
  bandwidth and memory. Direct links with a post-upload size check are a possible follow-up.
* **Resuming a user's `$HOME` on another sandbox** after eviction (archive and restore).
* **Malware scanning.** Enable Defender for Storage on the account if you need it.
* If collecting outputs fails after the agent ran, the turn still succeeds and the failure is
  listed in `skipped_outputs`; the agent is never re-run to retry collection.
* The Azure Blob store and the Bicep have not been run against a real account (the Azure store is
  written against the installed SDK; tests use a local store). The security scripts are tested
  against a temporary directory with planted symlinks, but not as real root with real accounts.

## Per-user CPU, memory and process limits

Users in one sandbox share its CPU and memory, so without limits one user can starve the
others. `limits` caps each user's whole process tree:

```yaml
pool: {cpu: "4", memory: 8Gi, users_per_sandbox: 4}
limits: {mode: cgroup, cpu: 1, memory: 2Gi, pids: 256}
```

* **`cgroup` (recommended).** One Linux cgroup v2 group per user: `cpu.max` (CPU is
  throttled, not killed), `memory.max` with swap disabled (the kernel OOM-kills only that
  user's processes), and `pids.max` (fork bombs stop at the limit). The turn script puts its
  shell into the user's cgroup before `exec runuser`, so the agent and everything it spawns
  inherit it.
* **`ulimit` (fallback).** Process count per user, and an address-space cap that applies per
  process, not in total. No CPU limit. Many runtimes (Python, Node, JVM) reserve large address
  ranges, so `memory` can break them; prefer `cgroup`.
* Limits are ceilings, not reservations. If `users_per_sandbox` times a limit exceeds the
  sandbox size (for example 4 users at 4Gi in an 8Gi sandbox), the service logs a warning at
  startup, because a full sandbox can still run out of memory. A limit larger than the whole
  sandbox is rejected.
* **Fails closed.** The cgroup setup runs at the start of every turn, because cgroups live in
  memory and a disk-mode suspend and resume loses them. If it fails, the turn fails; it never
  runs unlimited. When a sandbox is created the same requirements are checked (cgroup v2 mounted,
  the needed controllers, a writable hierarchy), and a sandbox that cannot enforce the limits is
  deleted and never registered. Then use `ulimit` or `none`.
* Environment variables: `SANDBOX_MUX_LIMITS_MODE`, `SANDBOX_MUX_USER_CPU`,
  `SANDBOX_MUX_USER_MEMORY`, `SANDBOX_MUX_USER_PIDS`.

Not verified: whether the sandbox's kernel exposes a writable cgroup v2 hierarchy to the exec
user. The generated scripts are tested against a fake cgroup directory and syntax-checked, not
run in a real sandbox. The creation-time check exists so this fails loudly on the first sandbox.
Not done: per-user disk quotas and I/O limits, per-user network limits, reporting each user's
peak usage as metrics.

## Network isolation and egress

Outbound access is set per pool with `pool.egress` and applied to each sandbox when it is
created (`egress_policy` on sandbox creation):

| `egress.mode` | Outbound HTTP/HTTPS |
|---|---|
| `disabled` (default) | Denied. The agent has no internet |
| `allowlist` | Denied except hosts in `allow_hosts` (`example.com`, `*.example.com`) |
| `enabled` | Allowed |
| `platform_default` | No policy sent: you get whatever the service defaults to |

I use explicit modes because the sources disagree on the service default (Microsoft's overview
says unrestricted without a policy, one news article says deny). Do not rely on it.
`SANDBOX_MUX_EGRESS_MODE` and `SANDBOX_MUX_EGRESS_ALLOW_HOSTS` set these from the environment.
`audit_only` logs what a policy would block without blocking, which helps when building an
allowlist.

**Changing the setting takes effect on running sandboxes.** Each sandbox is registered with the
fingerprint of the policy it was created under. The reaper leader applies the current policy
to any sandbox whose fingerprint differs (`set_egress_policy`) and retries on failure. So
switching `enabled` to `disabled` closes existing sandboxes within one sweep (30 seconds by
default), not only new ones. `platform_default` cannot undo an earlier policy.

If `obo.enabled`, egress `disabled` is rejected at startup, because the agent could not reach
the downstream APIs. Use `allowlist` with their hosts (for example `graph.microsoft.com`).

What this does not give you, and what I could not verify:

* **Egress policy is per sandbox, not per user.** All users on a sandbox share it. Different
  network permissions per user need different pools, which means separate service instances
  with different `pool.name` and egress settings.
* **Sandbox-to-sandbox traffic.** I found nothing in the public docs saying whether two
  sandboxes in one group can reach each other over the network. Test it: from one sandbox,
  try to connect to another's address. Denying egress is the control this project uses, but I
  have not confirmed that it covers east-west traffic. For hard separation use a separate
  sandbox group (and VNet, one subnet per group) for each trust boundary.
* **No inbound by design.** The manager never adds sandbox ports; all access is exec and file
  calls through the data plane.
* **Raw TCP/UDP.** `block_non_http` sets the transport default to deny. It is off by default
  because I could not confirm that HTTPS through the egress proxy still works with it on. The
  SDK's transport rules need a single destination port per rule, so "block this CIDR on all
  ports" cannot be expressed; private ranges are not blocked individually.
* **Not run against the service.** The policy objects are built with the real SDK models and
  pass its validator, and the wiring is tested with stubs. No sandbox has been created with
  these policies.

## On-Behalf-Of: acting as the signed-in user

With `obo.enabled`, a request can ask for delegated tokens, and the agent process (which
already runs as that user's Unix account) can call downstream APIs as that Entra user:

```http
POST /v1/agent/invoke
Authorization: Bearer <user token, audience = this service's app registration>

{"prompt": "...", "obo_resources": ["graph"]}
```

1. The user's token is validated as usual, then exchanged with Entra's On-Behalf-Of flow
   (MSAL `acquire_token_on_behalf_of`) for each requested alias in `obo.resources`.
   Callers name aliases, never raw scopes, and at most `max_resources_per_turn` per request.
2. The tokens are written to `$HOME/.agent/obo.json` with mode `0600`, owned by the user,
   and the path is passed as `OBO_TOKENS_FILE`. They are not put in the command line or
   environment (other users could see those), not stored in Redis, and not logged.
3. The file is deleted right after the turn, including when the agent fails. If the delete
   fails the error is logged, and the file remains readable by that user only until the
   token expires or the session is wiped.
4. If the exchange needs consent or step-up authentication, the API returns 403 with the
   `X-Claims-Challenge` header and does not take a sandbox slot. Entra's error text is not
   echoed to the caller.

Which header? I could not confirm a Foundry-specific OBO header in the public docs. Sources
disagree on whether Foundry's proxy forwards the user's token to the container at all, and
`x-ms-user-isolation-key` only scopes per-user resources: it is not a delegated token. So
the header is configurable (`obo.assertion_header`, default `Authorization`). If your front
end forwards the user token under another name, set that name. A token in a different
header from the one authenticated must belong to the same `oid` and tenant, or the request
is rejected.

Limits to be aware of:

* The agent process, and anything it runs, can use and exfiltrate the token while the turn
  runs. OBO narrows what it can do to what that user may do and what `obo.resources` allows;
  it does not stop a misbehaving agent from misusing that access.
* OBO requires `isolation.mode: unix_user` and `auth.mode: entra`; the config refuses to
  start otherwise. With `home_only`, users share one UID and could read each other's tokens.
* Egress policy is still per sandbox, so network access is shared even though identity is not.
* The exchange needs a confidential client: a secret (`OBO_CLIENT_SECRET`), a certificate,
  or a managed identity as a federated credential (`credential: managed_identity`, no secret
  to rotate). The app registration needs the downstream delegated permissions and consent.
* `obo_token(alias)` in `runner/run_turn.py` shows how an agent reads a token.

## Work IQ, as the signed-in user

**Use On-Behalf-Of, not a connector.** Work IQ's MCP endpoint is `https://workiq.svc.cloud.microsoft/mcp`.
Microsoft's Foundry documentation says it needs *delegated* Entra authentication with the scope
`api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask`, runs in the context of the signed-in user, and
**does not support application-only authentication**. The sandbox connector docs show only
`--authorization system`, which is the sandbox group's identity, shared by every user. A connector would
therefore either fail or run as the wrong identity. I found no documented user-delegated mode for
sandbox connectors, so do not rely on one. The OBO path below does this properly and is built.

```yaml
obo:
  enabled: true
  client_id: "<your app registration>"
  resources:
    workiq: ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"]
pool:
  egress:
    mode: allowlist
    allow_hosts: ["workiq.svc.cloud.microsoft"]     # the only outbound host the agent needs
```

The caller asks for it per request, `{"prompt": "...", "obo_resources": ["workiq"]}`, and the agent
process, running as that user, calls the endpoint with the user's token:

```python
from run_turn import bearer_headers          # runner/run_turn.py
headers = bearer_headers("workiq")           # {"Authorization": "Bearer <the user's delegated token>"}
# pass `headers` to your MCP client for https://workiq.svc.cloud.microsoft/mcp
```

**What makes sure it is the user's delegation**, and not something broader:

1. The only credential the agent receives is the OBO token for that user, in a file only that user's
   process can read, deleted after the turn. There is no shared connector identity and no secret in the
   image or environment.
2. After every exchange the service checks the token before handing it over (`obo.verify_delegated`,
   on by default): it must carry an `scp` claim and not be an app token, and its `oid` and tenant must
   equal the signed-in caller's. Anything else is refused with HTTP 502 before a sandbox slot is taken
   or a byte is written. A mis-set scope, an app-only token, or a mix-up between users cannot reach the agent.
3. The assertion must be the caller's own token (checked, including when sent in a different header),
   and callers choose aliases, never scopes.
4. Do **not** attach a Work IQ connector to the pool: that would add a second, shared identity.
5. With the egress allowlist the agent can reach only Work IQ, not arbitrary hosts, which limits where
   a token could be sent.

**Tenant setup (from the Foundry Work IQ documentation; I could not test any of this):**
* A Global Administrator provisions the Work IQ service principal and grants admin consent for
  `WorkIQAgent.Ask`, once.
* Your app registration (`obo.client_id`, the one behind `auth.audience`) needs the delegated permission
  `WorkIQAgent.Ask` on the Work IQ API, with consent, or the exchange returns a consent error (HTTP 403
  with `X-Claims-Challenge`; the client must send the user through consent or step-up sign-in).
* Billing: the Work IQ API is usage-based (Copilot Credits); connector-backed Microsoft 365 tools may
  need a Microsoft 365 Copilot licence per user.
* Microsoft warns that data can leave the Azure compliance boundary when you use Work IQ.

Not verified: that Work IQ returns JWT access tokens (if it returns opaque tokens the check fails
closed and tells you to set `verify_delegated: false`, which removes check 2), the exact tool names the
MCP endpoint exposes, and the admin-consent steps. The Foundry page also says the preview built-in tool
uses the A2A endpoint while the MCP path is the generally available one; this project uses MCP.

## Timeouts and environment variables

Foundry hosted sessions have a 15-minute idle timeout that the public docs describe as a
platform behaviour, not a setting. Here every timeout is configurable, in the YAML file or
from the environment (the environment wins). Values are validated at startup.

| Variable | Setting | Effect |
|---|---|---|
| `SANDBOX_MUX_IDLE_TIMEOUT_SECONDS` | `pool.auto_suspend_seconds` | Platform suspends an idle sandbox (state kept, compute billing stops). Default 900 |
| `SANDBOX_MUX_SESSION_TTL_SECONDS` | `session.user_idle_ttl_seconds` | Remove a user's session after this long idle: unbind, delete home. Default 3600 |
| `SANDBOX_MUX_EMPTY_SANDBOX_TTL_SECONDS` | `pool.empty_ttl_seconds` | Delete a sandbox with no users (above `min_sandboxes`). Default 600 |
| `SANDBOX_MUX_HISTORY_TTL_SECONDS` | `session.history_ttl_seconds` | Expire stored conversations. Default 7 days |
| `SANDBOX_MUX_TURN_TIMEOUT_SECONDS` | `session.turn_timeout_seconds` | Kill a running agent turn |
| `SANDBOX_MUX_QUEUE_TIMEOUT_SECONDS` | `session.queue_timeout_seconds` | Wait for capacity before HTTP 429 |
| `SANDBOX_MUX_MIN_SANDBOXES`, `_MAX_SANDBOXES`, `_USERS_PER_SANDBOX` | `pool.*` | Pool shape |
| `SANDBOX_MUX_CPU`, `_MEMORY`, `_DISK_SIZE` | `pool.cpu`, `pool.memory`, `pool.disk_size` | Size of each sandbox, e.g. `2` or `2000m`, `4Gi`, `20Gi`. Shared by all users on it, so size for `users_per_sandbox`. Applied at creation |
| `SANDBOX_MUX_EGRESS_MODE`, `_EGRESS_ALLOW_HOSTS` | `pool.egress.*` | Outbound network policy (see Network isolation) |
| `SANDBOX_MUX__<SECTION>__<KEY>` | any setting | e.g. `SANDBOX_MUX__POOL__STRATEGY=spread` |

These variables can also be put in a `.env` file in the working directory (or the path in
`SANDBOX_MUX_ENV_FILE`); real environment variables take precedence. Only `SANDBOX_MUX*`,
`REDIS_URL` and `OBO_CLIENT_SECRET` are read from it.

The idle timeout, CPU, memory and disk are applied when a sandbox is created, so existing
sandboxes keep their old values until they are replaced. The group's `maxSandboxCount` and
your subscription quota still cap the total.

## Layout

| Path | Purpose |
|---|---|
| `src/sandbox_mux/store.py` | Redis state and the atomic placement scripts |
| `src/sandbox_mux/pool.py` | Acquire a slot, create and bootstrap sandboxes, warm pool |
| `src/sandbox_mux/service.py` | One turn: lock, place, resume, provision user, run agent, persist history |
| `src/sandbox_mux/reaper.py` | Leader-elected eviction, draining, reconciliation |
| `src/sandbox_mux/backends/azure.py` | The only module that uses the sandbox SDK |
| `src/sandbox_mux/backends/fake.py` | In-memory backend for tests and local dev |
| `src/sandbox_mux/config.py` (`connectors`) | Which connectors and MCP servers each sandbox is created with |
| `src/sandbox_mux/files.py`, `blobs.py` | User files: quota, names, safe archive handling; Azure Blob and local stores |
| `src/sandbox_mux/triggers.py`, `delegation.py`, `triggers_cli.py` | Connector-event triggers, subscriptions and runs; consent and the encrypted token vault; the admin command |
| `src/sandbox_mux/obo.py` | Entra On-Behalf-Of exchange |
| `src/sandbox_mux/api.py` | FastAPI app: invoke, history, end session, pool status |
| `src/sandbox_mux/image.py`, `runner/Dockerfile` | Build the sandbox disk image from a container image, from CI |
| `runner/run_turn.py` | Example in-sandbox agent runner and its contract |
| `infra/main.bicep` | Sandbox group, optional VNet connection, data-plane role assignment |
| `config.example.yaml` | All settings |

## Run it

```bash
pip install -e ".[dev]" && pytest                       # no Azure or Redis needed
cp config.example.yaml config.yaml                      # fill in the azure section
export REDIS_URL=rediss://...
az login                                                # or use a managed identity
python -m sandbox_mux.main
```

Prerequisites in Azure:

1. Deploy `infra/main.bicep`, giving it the object ID of the identity the manager runs as
   (it gets *Container Apps SandboxGroup Data Owner*).
2. Build a sandbox disk image that contains the agent runner (see `runner/run_turn.py` for
   the contract) and set `pool.disk_id`. Exec must run as root inside the sandbox: the
   bootstrap check fails the sandbox otherwise.
3. Set `auth.tenant_id` and `auth.audience` to your Entra app registration. Dev mode
   (`X-Dev-User` header) trusts the caller and is for local use only.

## API

| Method and path | Meaning |
|---|---|
| `POST /v1/agent/invoke` `{conversation_id, prompt, obo_resources}` | Run a turn. 409 if the user already has one running, 429 if the pool is full, 403 if OBO needs consent |
| `GET /v1/conversations/{id}/messages` | Stored history |
| `DELETE /v1/me/session` | Release the slot and delete the user's home |
| `POST /v1/files?name=`, `GET /v1/files`, `GET /v1/files/{id}`, `GET /v1/files/{id}/metadata`, `DELETE /v1/files/{id}` | Files (see above) |
| `GET /v1/pool/status` | Loads, states, in-flight creations, waiters (admin OIDs only) |
| `GET /healthz`, `GET /readyz` | Liveness and Redis readiness |

## Known limits and things to verify

* **Preview SDK.** `azure-containerapps-sandbox` is a community preview (1.0.0b1) and the
  API version is `2026-09-01-preview`. The calls used (`SandboxGroupClient`,
  `begin_create_sandbox`, `get_sandbox_client(...).exec/write_file/ensure_running`,
  `list_sandboxes`, `delete_sandbox`) were checked against the installed package, but the
  behaviour against a live service has not been tested. All of it is confined to
  `backends/azure.py`.
* **OBO is tested with a fake MSAL app, not against Entra.** The exchange, consent and claims-challenge handling follow MSAL's documented result shape but have not been run against a tenant.
* **Not tested against real Azure.** The test suite uses the in-memory backend and
  fakeredis. The in-sandbox commands (`useradd`, `runuser`, `hidepid`) are built and
  checked for shape, not executed.
* **Egress and secrets.** Egress policy is per sandbox and shared by its users. Do not put
  model keys in the image or agent environment; use the group's egress credential injection
  or a gateway. This project sets allow/deny egress policies but does not configure
  credential injection.
* **Waiting uses polling** with jittered backoff, not Redis pub/sub.
* **Redis Cluster** is not supported: the Lua scripts touch keys that do not share a hash
  slot. Use a single primary (Azure Cache for Redis Standard/Premium, non-clustered).
* **Exec output size and runaway processes** rely on `timeout` inside the sandbox and the
  overall `turn_timeout_seconds`. Per-user CPU, memory and process limits are available
  (`limits`) but off by default. There are no per-user disk quotas.
