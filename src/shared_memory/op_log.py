"""Op-log primitives — Phase 1 foundation for local-first sync.

Schema + collection + indexes + seq counter + write helpers. Phase 1 #1
shipped the foundation; Phase 1 #2 instruments mutation tools to emit
op-log rows via the helpers below.

Two emission patterns per `design:local-first-junto-v0-mvp` v0.4.0 §4.3:

- §4.3.a `emit_op_log()` — best-effort sequential append for Chroma-backed
  mutations. Logs failures, returns None on error, never raises. The
  source-collection write has already landed by the time this is called;
  any op-log gap is closed by the §4.7 reconciliation pass.
- §4.3.b transactional emission via `with_op_log()` — Mongo-only, wraps
  source write + next_seq + op_log_append in a single rs0 transaction.
  (Implemented when the first Mongo-backed canary lands.)

See spec §4.1 (taxonomy), §4.2 (schema), §4.5 (per-server seq + origin id),
§4.6 (intent-id reconciliation), §4.7 (reconciliation pass).
"""

import logging
import uuid

from pymongo import ASCENDING, ReturnDocument

from shared_memory.config import ORIGIN_SERVER_ID
from shared_memory.helpers import utc_now_iso
from shared_memory.intent import get_current_intent_id

logger = logging.getLogger(__name__)

OPLOG_COLLECTION = "op_log"
OPLOG_META_COLLECTION = "op_log_meta"

OPLOG_SCHEMA_VERSION = 1

OP_TYPES = (
    "session.started",
    "session.ended",
    "message.sent",
    "message.status_changed",
    "learning.recorded",
    "learning.superseded",
    "function.registered",
    "function.enriched",
    "spec.defined",
    "spec.updated",
    "store.created",
    "store.tagged",
    "backlog.added",
    "backlog.updated",
    "lock.acquired",
    "lock.released",
    "lock.expired",
    "autopilot.config_changed",
    "autopilot.event_recorded",
    "autopilot.auto_disabled",
    "agent.registered",
    "agent.heartbeat",
    "agent.work_updated",
    "signal.emitted",
    "audit.event",
    "rename.applied",
)

_OP_TYPE_SET = frozenset(OP_TYPES)


class OpLogError(Exception):
    """Raised when an op-log invariant is violated."""


def is_valid_op_type(op_type: str) -> bool:
    """Return True iff op_type is in the closed v1 catalog (§4.1)."""
    return op_type in _OP_TYPE_SET


def ensure_op_log_indexes(db) -> None:
    """Create the op_log + op_log_meta collections (if absent) and their
    indexes (idempotent — safe to call on every startup).

    Indexes (per design §4.2):
    - `seq` — monotonic per-origin cursor reads.
    - `(origin, seq)` unique — primary per-server cursor for sync_pull
      and the natural dedupe key alongside `op_id`.
    - `ts` — time-range queries.
    - `op_type` — selective replay / per-type counts.
    - `intent_id` sparse — only populated when caller threaded `__intent_id`;
      used for journal-replay dedupe per §4.6.

    The meta collection holds one document per origin with the current
    monotonic seq counter; `_id` is the origin string.
    """
    op_log = db[OPLOG_COLLECTION]
    op_log.create_index([("seq", ASCENDING)])
    op_log.create_index(
        [("origin", ASCENDING), ("seq", ASCENDING)],
        unique=True,
        name="origin_seq_unique",
    )
    op_log.create_index([("ts", ASCENDING)])
    op_log.create_index([("op_type", ASCENDING)])
    op_log.create_index(
        [("intent_id", ASCENDING)],
        sparse=True,
        name="intent_id_sparse",
    )

    # The meta collection is keyed by origin (_id is the origin string),
    # so the default unique _id index is the only index needed.
    db[OPLOG_META_COLLECTION]


def next_seq(db, origin: str, session=None) -> int:
    """Atomically increment and return the next seq for `origin`.

    Phase 1 instrumentation will call this from inside the same Mongo
    transaction that performs the source-collection write and the op-log
    append, passing the active `session`. That way an aborted transaction
    rolls back the seq increment alongside everything else; no orphan
    seq numbers leak into the live counter.

    Standalone use (no transaction) is also supported and is what tests
    exercise today.
    """
    if not origin:
        raise OpLogError("origin is required for next_seq")
    result = db[OPLOG_META_COLLECTION].find_one_and_update(
        {"_id": origin},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    return int(result["seq"])


def emit_op_log(
    db,
    op_type: str,
    actor: dict,
    ref: dict,
    payload: dict,
    intent_id: str | None = None,
) -> dict | None:
    """Best-effort op-log append (§4.3.a, Chroma-backed mutations).

    Call AFTER the source-collection (Chroma) write has succeeded. The
    contract is "never raise" — the source write already landed, and any
    failure here creates an op-log gap that the §4.7 reconciliation pass
    will backfill on next startup. Errors are logged loudly.

    Args:
        db: Mongo database handle (from `get_mongo()`). May be None if
            Mongo is unavailable; we log + return None in that case.
        op_type: Must be in the §4.1 catalog (`is_valid_op_type` check).
        actor: `{"agent": str, "project": str, "session_id": str}`.
        ref: `{"collection": str, "doc_id": str}` — points back at the
            source row that this op materializes.
        payload: Data needed to materialize this op on a peer (full row
            content for append-only, the delta for mutable).
        intent_id: From `get_current_intent_id()`. Pass None to omit.

    Returns:
        The inserted op-log document on success; None if validation failed,
        Mongo was unavailable, or insert raised.
    """
    if db is None:
        logger.error(
            "op_log.emit skipped: mongo db handle is None",
            extra={"op_type": op_type, "ref": ref},
        )
        return None

    if not is_valid_op_type(op_type):
        logger.error(
            "op_log.emit rejected invalid op_type %s",
            op_type,
            extra={"op_type": op_type, "ref": ref},
        )
        return None

    try:
        seq = next_seq(db, ORIGIN_SERVER_ID)
        entry = {
            "_id": f"op_{uuid.uuid4().hex}",
            "seq": seq,
            "ts": utc_now_iso(),
            "origin": ORIGIN_SERVER_ID,
            "intent_id": intent_id,
            "actor": actor,
            "op_type": op_type,
            "ref": ref,
            "payload": payload,
            "schema_version": OPLOG_SCHEMA_VERSION,
        }
        db[OPLOG_COLLECTION].insert_one(entry)
        return entry
    except Exception as exc:
        # The source write already landed. Log loudly; reconciliation
        # (§4.7) will backfill this row on next startup scan.
        logger.error(
            "op_log.emit failed for %s ref=%s: %s",
            op_type,
            ref,
            exc,
            exc_info=True,
        )
        return None


def emit_op_log_from_context(
    db,
    op_type: str,
    actor: dict,
    ref: dict,
    payload: dict,
) -> dict | None:
    """`emit_op_log` with `intent_id` auto-pulled from the request contextvar.

    Convenience wrapper for the common case: a tool handler running inside
    the MCP request scope where `__intent_id` has been stripped + stashed
    on the contextvar by `intent.build_call_tool_handler_with_intent`.
    """
    return emit_op_log(
        db=db,
        op_type=op_type,
        actor=actor,
        ref=ref,
        payload=payload,
        intent_id=get_current_intent_id(),
    )
