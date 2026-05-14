"""Op-log primitives — Phase 1 foundation for local-first sync.

This module is the schema + collection + indexes + seq-counter foundation
that Phase 1 #2 (per-tool instrumentation) will build on. Today nothing
writes to op_log; this module's job is to make sure the collection and
its indexes exist on startup, define the closed op_type enumeration that
instrumentation must obey, and provide the atomic per-origin sequence
counter that op-log writers will consume from inside a Mongo transaction.

See `design:local-first-junto-v0-mvp` v0.3.0 §4.1 (taxonomy), §4.2 (schema),
§4.5 (per-server seq + origin id), and §4.6 (intent-id reconciliation).
"""

from pymongo import ASCENDING, ReturnDocument

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
