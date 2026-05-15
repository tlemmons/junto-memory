# Junto peer deployment

A *peer* is a second junto-memory server that replicates from the *primary* via
the sync engine. Federated agents connect to a peer over LAN; the peer
reconciles with the primary over Tailscale on whatever schedule §5.2 dictates.

This walkthrough takes a stranger from "I have a Linux box on my LAN" to a
running peer with a federated agent against it. See
`design:local-first-junto-v0-mvp` for the architectural background.

> **Terminology** (locked in v0.6.0 of the design spec):
> - **primary** — the canonical EC2/Tailscale junto-memory.
> - **peer** — what you're about to deploy.
> - **remote-mode** — an agent pointing at the primary.
> - **federated** — an agent pointing at this peer.

## 0. Prerequisites

On the box that will run the peer:

- Linux x86_64. (ARM works but the embedding determinism canary is
  unverified on ARM — see §10 risk 6 in the design spec.)
- Docker + Docker Compose v2.
- Tailscale installed and joined to the same tailnet as the primary.
- ≥ 4 GB RAM, ≥ 50 GB disk free.
- Outbound TCP 443 + UDP 41641 (Tailscale).
- Inbound TCP 8080 reachable on the LAN you intend to serve federated
  agents from. (Default port; change with `MCP_PORT` in `.env`.)

You also need:

- A clone of this repo on the peer (`git clone https://github.com/tlemmons/junto-memory && cd junto-memory`).
- The primary's MCP URL on the tailnet (`https://primary.<your-tailnet>.ts.net/mcp` or similar).
- An admin-tier API key on the primary. Generate one with `memory_admin(action="create_key", name="<descriptive-name>", role="admin")` from a session that already has admin tier; the returned `api_key` value is shown once — record it.

## 1. Generate the peer's secrets

Mongo's single-node replica set requires a keyfile for internal auth. Each
peer generates its own — sharing the primary's keyfile is not required and is
not recommended (replica-set auth is local to each Mongo instance).

```bash
mkdir -p secrets
openssl rand -base64 756 > secrets/mongo-keyfile
chmod 400 secrets/mongo-keyfile
sudo chown 999:999 secrets/mongo-keyfile  # UID 999 = mongo user inside the container
```

If you skip the `chown`, Mongo refuses to start with `permissions on /etc/mongo/keyfile are too open`.

## 2. Pick this peer's `ORIGIN_SERVER_ID`

Every peer must declare a globally-unique origin string. The primary's op-log
indexes `(origin, seq)` uniquely, so a collision corrupts the cursor.

Convention: `peer-<short-location>`, e.g. `peer-spg-office`, `peer-home`, `peer-laptop`.

This MUST be different from the primary's origin (`central` by convention)
and from every other peer in the deployment.

## 3. Write `.env`

Copy `.env.example` and fill in the peer-specific values:

```ini
# === Mongo (REQUIRED, peer-specific) ===
# Generate strong, peer-local credentials. These don't need to match the primary.
MONGO_USER=mcp_orch
MONGO_PASSWORD=<random-32+chars>
MONGO_DB=mcp_orchestrator

# === Sync engine (REQUIRED) ===
# Globally unique. See §2 above.
ORIGIN_SERVER_ID=peer-spg-office

# Primary's MCP endpoint on the tailnet.
JUNTO_SYNC_PRIMARY_URL=https://primary.your-tailnet.ts.net/mcp

# Admin-tier API key on the PRIMARY (generated via memory_admin on primary).
JUNTO_SYNC_PRIMARY_KEY=<key-value-from-primary>

# Admin-tier API key on THIS PEER (you'll generate this after first boot, see §5).
# Leave blank for the first `up -d`; you'll restart the sync-engine after.
JUNTO_SYNC_LOCAL_KEY=

# === Optional overrides ===
# MCP_PORT=8080
# CHROMA_PORT=8001
# MONGO_PORT=27019
# JUNTO_SYNC_PULL_INTERVAL=10.0
# JUNTO_SYNC_PUSH_INTERVAL=5.0
```

## 4. Start chroma + mongo + mcp-server (sync engine will fail loudly until §5)

```bash
docker compose -f docker-compose.peer.yml up -d chromadb mongodb mcp-server
```

The first start does a few things:

1. Pulls the digest-pinned `chromadb/chroma:1.2.4@sha256:70c2…` image.
   This is the same digest as the primary — required for cross-server
   embedding determinism (design spec §4.3.a + §10 risk 5).
2. Initializes Mongo as a single-node replica set named `rs0`. The
   container entrypoint waits for `rs.initiate()`; the healthcheck won't
   pass until that happens. Give it ~20s.
3. Boots the MCP server on port `MCP_PORT` (default 8080) with
   `ORIGIN_SERVER_ID` from your `.env`.

Sanity check:

```bash
curl -fsS http://localhost:8080/health
# {"status":"healthy","chroma":"healthy"}
```

## 5. Generate this peer's admin-tier key and finish wiring the sync engine

The sync engine needs an admin-tier API key on this peer to read the local
op-log (push side) and to materialize incoming primary ops (pull side).

From a CC session with the peer as MCP target (caller must already have admin role):

```python
memory_admin(action="create_key", name="sync-engine-local", role="admin")
# {"status": "created", "api_key": "smk_...", "name": "sync-engine-local", "role": "admin", "warning": "Save this key now — it cannot be retrieved later."}
```

Or from inside the container (no MCP session needed — talks directly to the local Mongo):

```bash
docker exec -it junto-peer-mcp-server python -c \
  "from shared_memory.auth import create_api_key; \
   raw, _ = create_api_key(name='sync-engine-local', role='admin', created_by='operator'); \
   print(raw)"
```

Take the returned `smk_…` value, put it in `.env`:

```ini
JUNTO_SYNC_LOCAL_KEY=smk_<...>
```

Now bring up the sync engine:

```bash
docker compose -f docker-compose.peer.yml up -d sync-engine
```

Logs should show pull + push loops starting against both endpoints:

```bash
docker logs -f junto-peer-sync-engine
# sync_engine: local origin auto-discovered: peer-spg-office
# sync_engine: starting pull loop (primary=https://primary.../mcp, interval=10s)
# sync_engine: starting push loop (local=http://mcp-server:8080/mcp, interval=5s)
```

If you see `JUNTO_SYNC_*_KEY must be ...` errors, the env vars didn't land in
the container. Restart with `docker compose -f docker-compose.peer.yml up -d
--force-recreate sync-engine` after fixing `.env`.

## 6. Cold sync

The peer starts empty. The sync engine pulls from the primary in batches of
500 ops at ~10s cadence; full cold-sync at MVP scale (~80k ops) takes
**15–30 minutes** on a typical Tailscale 50 Mbps link (design spec §5.3).

Watch progress via the cursor file (mounted on a named volume):

```bash
docker exec junto-peer-sync-engine cat /var/lib/junto/sync-cursors.json
# {"pull": {"central": 73421}, "push": {"peer-spg-office": 0}}
```

`pull.central` ticking upward = pull loop is making progress. `push.<your-origin>`
stays at 0 until federated agents start writing through this peer.

You can also `memory_query` from a CC session targeting the peer; results
will fill in as collections populate.

## 7. Run the smoke

Before pointing real agents at this peer, run the included sync engine smoke
to verify the transport contract end-to-end:

```bash
JUNTO_SYNC_ADMIN_KEY=$JUNTO_SYNC_LOCAL_KEY \
JUNTO_SYNC_LOCAL_URL=http://localhost:8080/mcp \
.venv/bin/python contrib/test/sync_engine_smoke.py
```

Expected: 5/5 PASS. Side-effect-free by design — pushes the server's own ops
back to itself so the §7.4 self-origin gate rejects before any apply happens.

If a check fails, the smoke output embeds the literal recipe for the most
common fix path:

```
docker compose -f docker-compose.peer.yml build mcp-server
docker compose -f docker-compose.peer.yml up -d --force-recreate mcp-server
```

## 8. Point a federated agent at the peer

In the agent's MCP client config (e.g. CC `.claude/settings.json` or
launcher script), change the MCP server URL from the primary's to this peer's:

```jsonc
{
  "mcpServers": {
    "shared-memory": {
      "url": "http://<peer-lan-ip>:8080/mcp",
      "headers": { "Authorization": "Bearer <agent-api-key-on-peer>" }
    }
  }
}
```

The agent is now **federated** in v0.6.0 terminology. Pinned-to-peer per
§7.5: if the peer drops, the agent fails loudly. No silent fallback to the
primary.

Generate the agent's API key on the peer (NOT the primary). Per §7.3,
autopilot config is primary-write only, but everything else routes through
the peer.

## 9. Verify round-trip

From the federated agent's session, write something:

```python
memory_record_learning(title="Peer round-trip test", details="...", project="junto")
```

Within ~5 seconds (push interval + jitter), the sync engine pushes that op
to the primary. Confirm from a remote-mode session against the primary:

```python
memory_query(query="Peer round-trip test", project="junto")
```

The learning should appear, with `origin=peer-spg-office` in the op-log row
(check via `memory_admin(action="op_log_tail", limit=5)` on the primary).

## 10. What to monitor in steady state

- **Sync engine logs.** Quiet is good. Any `ERROR` or repeated `WARNING`
  about transport timeouts means the Tailscale link is degraded or the
  primary is unreachable.
- **Cursor lag.** `pull.central` should track within ~50 ops of the primary's
  current seq. Sustained gap > 1000 ops = sync engine is falling behind.
- **Local disk on the peer.** Mongo + Chroma grow ~5 GB/month at MVP scale.
- **`memory_health` from a federated agent.** Returns the peer's status, not
  the primary's. To check primary reachability from the peer, run
  `memory_health` from a remote-mode session.

## 11. Known caveats (Phase 2 today)

- **HTTPMCPClient does NOT auto-reconnect.** The sync engine's primary
  client uses the same MCP session across the entire process lifetime.
  A persistent half-open Tailscale stream (e.g., NAT timeout, primary
  restart) will keep failing with exponential backoff (5s → 300s) but
  never recover until the sync engine restarts. `backlog_4dd929b6c622`
  is the fix; until it lands, `docker compose restart sync-engine` is
  the operator action.
- **No cursor file locking.** Running two sync engines against the same
  `sync-cursors.json` (e.g., dev + prod on one box pointing at the same
  cursor path) corrupts the cursor. `backlog_817f469c656e` tracks the
  flock fix. Don't do this.
- **MQTT eager-trigger is Phase 2.5.** v1 cadence is always 10s pull / 5s
  push, even when the primary is up. New messages from remote-mode agents
  arrive at federated agents with up to ~13s latency. Acceptable for MVP.
- **No reconciliation manual trigger.** §4.7 reconciliation runs on
  primary startup but isn't yet exposed via `memory_admin`. Phase 2.5.
- **Cold sync uses the regular pull endpoint.** No optimized snapshot
  transfer. 15–30 minutes at MVP scale is honest; bigger deployments
  will need OQ#3 (vector-in-payload optimization for cold start).

## 12. Tearing down

If you need to destroy and recreate:

```bash
docker compose -f docker-compose.peer.yml down -v
# -v removes the named volumes (chroma-data, mongo-data, sync-cursors).
# Without -v they survive and the peer resumes from the same cursor on
# next `up -d`. -v means full cold sync next time.
```

The primary is unaffected — peer volumes are local to the peer.

## 13. Smoke + acceptance test cheat sheet

| Test | Command | Pass criterion |
|------|---------|----------------|
| Health | `curl http://localhost:8080/health` | `{"status":"healthy",...}` |
| Sync smoke | `JUNTO_SYNC_ADMIN_KEY=$JUNTO_SYNC_LOCAL_KEY contrib/test/sync_engine_smoke.py` | 5/5 PASS |
| Round-trip | write on peer → query on primary | learning visible within ~5s |
| Tailscale-drop (§13 design spec) | `sudo tailscale down` for 1h during agent activity; `sudo tailscale up` | All writes present on primary; sync recovery < 5 min |

The Tailscale-drop acceptance test is the §13 "Phase 2 done" gate. Once it
passes against a deployed peer with active federated agents, Phase 2 is
complete and the deployment is production-ready for the originating LAN.

---

Questions? File against `junto-memory` on GitHub or `memory_send_message
to_instance="memory" to_project="junto"`.
