"""Sync endpoints — Phase 2 replication primitive.

Per `design:local-first-junto-v0-mvp` v0.4.0 §5.1.

`memory_sync_pull` — read side. Returns op_log rows newer than the
caller's cursor, per origin. Cursor map shape: `{origin_id: last_seen_seq}`.
Origins absent from the cursor are treated as `seq=0` (full history),
which is the cold-start case in §5.3.

`memory_sync_push` — write side. Not yet implemented (separate session;
materialization machinery is the meat of Phase 2).

**Auth:** both endpoints require role `admin` or `owner` ("sync"
permission). These are server-to-server replication primitives — a
LAN-local junto-memory pulling from central — not for routine agents.

**Open questions surfaced while writing pull (filed as follow-ups before
push lands):**

1. **Embedding payload completeness.** §4.2 schema says "embeddings inline
   when applicable" but the §4.3.a canaries shipped to date do NOT capture
   the embedding vector in payload — only the source fields. A peer
   applying these rows would have to re-embed locally, contradicting §5.3.
   Either canaries need a retrofit to include vectors, or the spec needs
   to soften the "no re-embed" property to "MVP allows re-embed."
2. **Project filter + internal events.** Server-side internal events
   (lock.expired, autopilot.auto_disabled, etc.) may emit without
   `actor.project`. Today the pull tolerates missing/null project via
   `$or`; we should confirm the design wants those replicated to all
   peers vs. scoped to specific ones.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.auth import require_auth
from shared_memory.clients import get_mongo
from shared_memory.config import ORIGIN_SERVER_ID
from shared_memory.helpers import require_session
from shared_memory.op_log import OPLOG_COLLECTION
from shared_memory.state import active_sessions

log = logging.getLogger(__name__)


def _jsonable(obj):
    """Recursive JSON-default for datetime + anything Mongo-returned.

    Op_log payloads round-trip arbitrary fields including BSON dates;
    PyMongo decodes those to `datetime`. JSON has no date type — convert
    to ISO-8601 strings so peers can re-parse on the apply side.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(
        f"Cannot JSON-serialize {type(obj).__name__} in op_log payload"
    )


def _pull_op_log(
    db,
    cursors: Dict[str, int],
    limit: int,
    projects: Optional[List[str]],
) -> Dict[str, Any]:
    """Core read logic for memory_sync_pull — extracted for unit testing.

    Pure function: takes a Mongo `db` handle + cursor map + page size +
    optional project filter; returns the response dict.

    Discovers all known origins (server-side `distinct` ∪ cursor keys),
    fetches up to `limit+1` rows per origin where `seq > cursor[origin]`,
    sets `has_more[origin]` from the +1 probe, and trims to `limit`.

    Internal events (no `actor.project`) are always included when a
    project filter is set; see module docstring open-question #2.
    """
    op_log = db[OPLOG_COLLECTION]
    known_origins = set(op_log.distinct("origin")) | set(cursors.keys())

    ops: List[Dict[str, Any]] = []
    next_cursor: Dict[str, int] = {}
    has_more: Dict[str, bool] = {}

    for origin in sorted(known_origins):
        cur = int(cursors.get(origin, 0))
        query: Dict[str, Any] = {"origin": origin, "seq": {"$gt": cur}}

        if projects:
            query["$or"] = [
                {"actor.project": {"$in": projects}},
                {"actor.project": {"$exists": False}},
                {"actor.project": None},
            ]

        # Fetch limit+1 so we can flag has_more without a separate count().
        rows = list(
            op_log.find(query).sort([("seq", 1)]).limit(limit + 1)
        )
        if not rows:
            continue

        page_has_more = len(rows) > limit
        rows = rows[:limit]

        has_more[origin] = page_has_more
        next_cursor[origin] = rows[-1]["seq"]
        ops.extend(rows)

    return {
        "ops": ops,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "server_origin": ORIGIN_SERVER_ID,
    }


@mcp.tool()
async def memory_sync_pull(
    session_id: str,
    since_cursor_by_origin: Optional[Dict[str, int]] = None,
    limit: int = 500,
    projects: Optional[List[str]] = None,
    ctx: Context = None,
) -> str:
    """Pull op_log entries newer than the caller's cursor (per origin).

    Phase 2 replication read endpoint per §5.1 of
    `design:local-first-junto-v0-mvp`. A LAN-local junto-memory calls this
    against central on a cadence (60s online, 10s when MQTT silent) and
    materializes the returned ops into its local Mongo+Chroma stores.

    Args:
        session_id: Your session ID. Caller must hold `admin` or `owner`
            tier — these are server-to-server credentials.
        since_cursor_by_origin: Map `{origin_id: last_seen_seq}`. The
            server returns ops where `seq > cursor[origin]` for each
            origin. Origins absent from the map are treated as `seq=0`
            (full history). Empty/None map = cold start.
        limit: Max ops returned per origin per call. Default 500. Server
            caps responses at this many rows even if more are available;
            caller pages forward via the returned `next_cursor`.
        projects: When set, restrict to ops whose `actor.project` is in
            this list. Op_log entries without a project (server-side
            internal events) are always included.

    Returns JSON:
        {
          "ops": [<op_log entry>, ...],   # sorted by (origin, seq) ascending
          "next_cursor": {origin: last_returned_seq, ...},
          "has_more": {origin: bool, ...},
          "server_origin": "<this server's origin_id>",
        }

    `next_cursor` only contains origins that returned at least one row
    on this call. `has_more[origin]=True` means another page exists; the
    caller should re-call with the returned cursor. Empty result =
    caller is up to date.
    """
    err = require_session(session_id)
    if err:
        return err

    session_info = active_sessions[session_id]

    auth_err = require_auth(session_info, "sync")
    if auth_err:
        return json.dumps({"error": auth_err})

    if not isinstance(limit, int) or limit < 1:
        return json.dumps({"error": "limit must be a positive integer"})

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    cursors: Dict[str, int] = dict(since_cursor_by_origin or {})
    result = _pull_op_log(db, cursors, limit, projects)
    return json.dumps(result, default=_jsonable)


@mcp.tool()
async def memory_sync_push(
    session_id: str,
    ops: List[Dict[str, Any]],
    ctx: Context = None,
) -> str:
    """Apply a batch of op_log entries received from a peer (NOT YET LIVE).

    Phase 2 replication write endpoint per §5.1. Validates per-origin seq
    monotonicity (with §4.3.a "sequence skip" tolerance), `op_type`
    matches catalog, `schema_version` compatible, `intent_id` dedupe.
    Materializes each op into the local source store (Mongo or Chroma)
    and appends to the local op_log.

    **Status:** This endpoint is shipped as a typed stub so the API
    surface is visible to callers, but the materialization machinery
    that re-derives source-store writes from op_log payloads is the
    next session's work. Calling it today returns a structured
    `not_implemented` error so a peer can detect "central is too old"
    and pause replication cleanly rather than fail silently.

    Args:
        session_id: Your session ID. Admin/owner tier required.
        ops: Op_log entries to apply, in `(origin, seq)` ascending order.

    Returns JSON:
        {"error": "not_implemented", "endpoint": "memory_sync_push",
         "reason": "materialization machinery not yet shipped — see
                    design:local-first-junto-v0-mvp v0.4.0 §5.1 and
                    state:memory next-steps"}
    """
    err = require_session(session_id)
    if err:
        return err

    session_info = active_sessions[session_id]
    auth_err = require_auth(session_info, "sync")
    if auth_err:
        return json.dumps({"error": auth_err})

    return json.dumps(
        {
            "error": "not_implemented",
            "endpoint": "memory_sync_push",
            "reason": (
                "materialization machinery not yet shipped — see "
                "design:local-first-junto-v0-mvp v0.4.0 §5.1 and "
                "state:memory next-steps"
            ),
            "ops_received": len(ops or []),
        }
    )
