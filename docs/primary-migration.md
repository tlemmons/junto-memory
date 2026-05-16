# Migrating an existing primary to local-first-ready state

This document walks an operator through converting an existing
junto-memory deployment ("primary") into a configuration that peers
can replicate from. The bootstrap scripts under `contrib/test/`
assume a clean install; this guide covers the **populated existing
primary** path.

**Audience:** operators who already have a junto-memory `mcp-server`
running with real data and want to add peers (`docker-compose.peer.yml`)
that replicate from it.

**Time:** 15–30 minutes for the in-place migration, plus a brief
container-restart window.

**Risk profile:** the rs0 replica-set conversion is the only step
that touches the existing data path. Mongo preserves the data
directory across this conversion — your data does not move — but
plan a short maintenance window because the mongo container restarts.

---

## 0. Pre-flight

Before starting, confirm:

- [ ] You have shell access to the primary host as a user who can run `docker`.
- [ ] Primary is reachable from the host(s) that will become peers
      — typically over Tailscale or a similar overlay. Note the
      hostname/IP peers will use; you'll embed it in their `.env`.
- [ ] Primary's current `mcp-server` is healthy:
      `curl -sf http://localhost:8080/health` returns `{"status":"healthy",…}`.
- [ ] You have a recent backup of the mongo data volume. The migration
      itself does not delete data, but having a rollback option is cheap
      insurance.
- [ ] Primary's `mcp-server` image is current enough to have Phase 1
      op-log emission. Roughly: any build from late 2026-04 onward has
      enough canaries instrumented for Phase 2 sync to be useful.
      Verify by checking the image tag matches a commit at or after
      `86f6da0` (op_log Phase 1 #2 canary 13/13). If the deployed
      image is older, **upgrade first** — peers will sync empty op-logs
      from an unmodified pre-Phase-1 primary.

---

## 1. Determine primary's current mongo mode

Two possibilities:

### Case A — primary already runs mongo as a single-node `rs0` replica set

```bash
docker exec <mongo-container> mongosh --quiet \
  -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
  --eval 'try { print(rs.status().set); } catch(e) { print("standalone"); }'
```

If output is `rs0` (or any replica-set name), **skip to §3**. Your
primary is already replica-set-mode and supports op-log transactions
out of the box.

### Case B — primary runs mongo as a plain standalone

If the output is `standalone` or you see
`NotYetInitialized / no replset config has been received`, you need
to convert. Continue to §2.

---

## 2. Convert standalone mongo → single-node `rs0` (Case B only)

This is the only step with risk-bearing changes. Mongo preserves the
data directory; the conversion just enables replica-set features
(notably: multi-document transactions, which Phase 1 op-log emission
needs). After conversion, the single existing node becomes the
PRIMARY of a one-member replica set.

### 2.1 Generate a keyfile (replica sets require internal auth)

```bash
# On the primary host, in the junto-memory checkout:
mkdir -p secrets
openssl rand -base64 756 | sudo tee secrets/mongo-keyfile >/dev/null
sudo chmod 400 secrets/mongo-keyfile
sudo chown 999:999 secrets/mongo-keyfile   # mongo's uid:gid inside the image
```

### 2.2 Edit `docker-compose.yml` for `mcp-mongodb` (or whatever your
mongo service is named)

Add the `--replSet rs0` arg, the keyfile volume mount, and the
`--keyFile` arg:

```yaml
services:
  mongodb:
    image: mongo:7.0
    container_name: <existing-name>
    command:
      - --replSet
      - rs0
      - --keyFile
      - /etc/mongo/keyfile
      - --bind_ip_all
    volumes:
      - mongo-data:/data/db
      - ./secrets/mongo-keyfile:/etc/mongo/keyfile:ro
    # ... your existing environment, healthcheck, ports ...
```

If you already had `command:` entries, append the replSet+keyFile
args; don't remove existing ones unless they conflict.

### 2.3 Recreate the container

```bash
docker compose up -d mongodb
sleep 10
docker logs --tail 20 <mongo-container>
```

Expect to see the node come up with `NotYetInitialized` warnings —
that's expected; it's running in replica-set mode but hasn't been
initiated.

### 2.4 Initiate the replica set

```bash
docker exec <mongo-container> mongosh --quiet --norc \
  "mongodb://${MONGO_USER}:${MONGO_PASSWORD}@localhost:27017/?directConnection=true&authSource=admin" \
  --eval '
    rs.initiate({_id:"rs0", members:[{_id:0, host:"mongodb:27017"}]});
    for (let i=0; i<30; i++) {
      try { if (db.hello().isWritablePrimary) { print("primary at " + i + "s"); quit(0); } } catch(e){}
      sleep(1000);
    }
    print("never elected"); quit(1);
  '
```

Expect `{ok: 1}` from `rs.initiate` and `primary at <N>s` (typically 1–3 seconds).

### 2.5 Restart mcp-server

The mcp-server holds open mongo connections under the old mode; bounce
it to pick up the replica-set-aware client:

```bash
docker compose restart mcp-server
sleep 5
curl -sf http://localhost:8080/health
```

### 2.6 Verify Phase 1 op-log emission still works

Trigger a write and confirm an op_log row appears:

```bash
# Use any mutation tool you have admin-key access for:
curl -sX POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"memory_record_learning","arguments":{"session_id":"migration-test","title":"migration test","details":"verifying op_log post-rs0"}}}'

# Inspect:
docker exec <mongo-container> mongosh --quiet \
  -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
  mcp_orchestrator --eval 'db.op_log.find().sort({seq:-1}).limit(1).toArray()'
```

If a learning.recorded op_log entry appears, Phase 1 emission survived
the conversion. If not, check `docker logs <mcp-server-container>`
for transaction errors (the most common post-conversion failure mode
is the mcp-server connecting to mongo via a non-replica-set client
URI; the fix is the restart in §2.5).

---

## 3. Add `ORIGIN_SERVER_ID` to primary's `.env`

Primary's op_log entries need a stable origin string so peers can
distinguish primary's writes from their own. Pick one — typically
`central` for a canonical deployment, or something descriptive like
`work-central`:

```bash
echo "ORIGIN_SERVER_ID=central" | sudo tee -a .env  # or your env file
docker compose restart mcp-server
```

After restart, new op_log rows will have `origin=central`. Existing
op_log rows continue to lack this field — that's OK; peer sync only
cares about ops from now forward (cold sync still replays them all,
but `(origin, seq)` uniqueness is enforced going forward).

> Note: the docker-compose.yml in this repo (intended for fresh
> installs) declares `ORIGIN_SERVER_ID` with `:?` so the container
> refuses to start without it set. If you're modifying an existing
> compose file that doesn't have that guard, add the variable to the
> service's environment block manually.

## 4. Mint an admin-tier API key for each peer

Each peer needs an admin-tier API key valid on primary. The peer uses
it to authenticate `memory_sync_push` calls. Mint per-peer keys so
they can be revoked independently.

```bash
docker exec <mcp-server-container> python -c "
from shared_memory.auth import create_api_key
raw, _ = create_api_key(
    name='peer-<descriptive-name>',  # e.g. 'peer-spg-office'
    role='admin',
    created_by='primary-migration',
)
print(raw)
"
```

Output looks like `smk_abcd...`. **Save it now** — only the hash is
stored in the database; you cannot recover the raw key later.

Repeat for each peer you plan to onboard.

## 5. Confirm network reachability for peers

Peers need an HTTP-reachable URL for primary's `mcp-server`. Common
shapes:

- **Tailscale / Wireguard:** peer hits `http://primary.<tailnet>.ts.net:8080/mcp`.
- **VPN / private LAN:** peer hits primary's LAN IP.
- **Public-facing (uncommon, not recommended):** peer hits the public
  IP — requires nginx/caddy in front to add TLS.

Note the URL you'll hand to peer operators. It goes in their `.env`
as `JUNTO_SYNC_PRIMARY_URL`.

## 6. Hand peer credentials to each peer operator

For each peer, share (out-of-band, e.g. encrypted message, not in a
chat log):

| Field | Where it goes (peer `.env`) | Example |
|---|---|---|
| Primary URL | `JUNTO_SYNC_PRIMARY_URL` | `http://primary.tailnet.ts.net:8080/mcp` |
| Per-peer admin key | `JUNTO_SYNC_PRIMARY_KEY` | `smk_...` (from §4) |
| Peer's own origin | `ORIGIN_SERVER_ID` | `peer-spg-office` (peer operator picks) |

Peer operator then follows
[peer-deployment.md](./peer-deployment.md) using these credentials.

## 7. Verify cross-peer setup

After at least one peer is up and running:

```bash
# On primary, watch op_log grow as peers push:
docker exec <mongo-container> mongosh --quiet \
  -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
  mcp_orchestrator --eval '
    db.op_log.aggregate([
      { $group: { _id: "$origin", count: { $sum: 1 }, max_seq: { $max: "$seq" } } }
    ]).toArray()'
```

You should see one row per origin (primary's own, plus each active
peer). Counts grow as agents write.

---

## Rollback

If the rs0 conversion (§2) leaves mongo in a broken state:

1. Stop the container: `docker compose stop mongodb`.
2. Restore the data volume from the §0 backup.
3. Revert the `docker-compose.yml` change (remove `--replSet rs0` and
   keyfile bits).
4. Restart: `docker compose up -d mongodb mcp-server`.

If only step §3–§6 introduces problems (e.g., peers can't authenticate),
remove `ORIGIN_SERVER_ID` from primary's `.env` and revoke the minted
keys via `memory_admin`. The cluster reverts to standalone mode with
no peer replication.

---

## Known gotchas

- **Mongo keyfile permissions.** Mode must be 400; owner must match
  the uid inside the image (typically 999:999 for mongo:7.0). The
  most common error is `permissions on /etc/mongo/keyfile are too
  open`.
- **`directConnection=true` for the rs.initiate call.** Connecting
  via the standard mongo URI fails before rs.initiate because the
  client tries to discover a replica set. The `directConnection=true`
  parameter bypasses discovery for the single bootstrap call.
- **mcp-server holds stale client.** After rs0 conversion, mcp-server
  must be restarted (§2.5) to pick up a replica-set-aware connection.
  Forgetting this manifests as transaction errors in mcp-server logs
  while mongo itself looks healthy.
- **API keys can't be recovered.** Step §4 prints the raw key
  exactly once. Save it. If lost, mint a new one and update the
  affected peer's `.env`.
- **Existing op_log rows lack `origin` field.** Pre-Phase-1 data
  doesn't have op-log entries at all. Pre-§3 data has op-log rows
  without `origin`. Peers don't replicate pre-`origin` rows — only
  ops emitted after `ORIGIN_SERVER_ID` is set ship to peers. This
  is by design (no historical backfill in MVP).

---

## What this enables

After this migration, primary can act as the upstream for any number
of peers running `docker-compose.peer.yml`. The acceptance criteria
from `design:local-first-junto-v0-mvp` §13 apply: peer-side writes
replicate to primary in steady state, survive a Tailscale drop, and
re-sync without conflict on reconnect.

See [peer-deployment.md](./peer-deployment.md) for the
per-peer setup, and `architecture:shared-memory-v1` (memory spec)
for the broader sync semantics.
