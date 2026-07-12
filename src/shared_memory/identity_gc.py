"""Mechanism B — pending-agent GC (design:identity-lifecycle-v0 v0.2.0).

Reaps PENDING-tier roster rows whose agent is genuinely gone, so auto-
registered one-shots (probes, temp helpers, mis-boots) stop accumulating as
addressable ghosts that silently swallow mis-routed messages.

Parameters as ratified by coordinator@nimbus (msg_7bfb4018d26e) + memory:
- PENDING TIER ONLY. named/admin/worker rows are never touched — the
  pending/named boundary is the destructive gate, which is why the fleet's
  legitimate long-livers were promoted to named before this shipped
  (2026-07-12, Tom-approved).
- NO immediate-reap on clean end_session (coordinator Flag 1, taken further:
  trigger-1 dropped entirely). ALL reaping goes through the grace scan, so a
  parked-not-dead pending agent is structurally safe. Correctness > promptness.
- GRACE: last_seen older than JUNTO_PENDING_REAP_GRACE_HOURS (default 48).
- LIVE-SESSION GUARD: never reap while any active session belongs to the
  agent (covers the long-running-but-quiet case).
- OBLIGATION GUARD (v1 = BLOCK, not orphan-resolve): never reap an agent
  with open inbound obligations — reaping would strand the waiter forever.
- SCAN, THROTTLED: runs at session start (the cleanup_stale_sessions moment),
  scoped to pending rows only, and does real work at most once per
  THROTTLE_MINUTES via a claimed last_swept marker.
- REAP = delete the registered_agents + agent_directory rows (addressability
  and roster presence). Artifacts (messages, learnings, specs, audit) are
  NEVER touched — agent ≠ output. A reaped name can freely re-register later.
- DEFAULT OFF (JUNTO_PENDING_REAP_ENABLED) — enable deliberately after the
  roster's long-livers are promoted.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)

META_COLLECTION = "maintenance_meta"
META_ID = "pending_reap"
THROTTLE_MINUTES = 15


def reap_enabled() -> bool:
    return os.environ.get("JUNTO_PENDING_REAP_ENABLED", "false").lower() in (
        "1", "true", "yes",
    )


def _grace_hours() -> float:
    try:
        return float(os.environ.get("JUNTO_PENDING_REAP_GRACE_HOURS", "48"))
    except ValueError:
        return 48.0


def _as_utc(value) -> datetime:
    """Normalize mongo datetimes (naive UTC) / ISO strings to aware UTC.
    Unparseable/missing → datetime.min (ancient ⇒ grace-eligible)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.min.replace(tzinfo=timezone.utc)


def maybe_reap_pending_agents(db, active_sessions: Dict[str, dict]) -> List[dict]:
    """Throttled scan; returns the reaped [(project, name), ...] descriptors.
    Best-effort by design — callers wrap it; any exception here must never
    block a session start."""
    if db is None or not reap_enabled():
        return []

    now = datetime.now(timezone.utc)

    # Throttle: claim the sweep FIRST (upsert last_swept), so concurrent
    # session starts don't run duplicate scans.
    meta = db[META_COLLECTION].find_one({"_id": META_ID})
    if meta and _as_utc(meta.get("last_swept")) > now - timedelta(minutes=THROTTLE_MINUTES):
        return []
    db[META_COLLECTION].update_one(
        {"_id": META_ID}, {"$set": {"last_swept": now}}, upsert=True
    )

    live = {
        (info.get("project"), info.get("claude_instance"))
        for info in active_sessions.values()
    }
    cutoff = now - timedelta(hours=_grace_hours())

    reaped: List[dict] = []
    for row in list(db.registered_agents.find({"tier": "pending"})):
        proj, name = row.get("project"), row.get("name")
        if (proj, name) in live:
            continue
        if _as_utc(row.get("last_seen")) >= cutoff:
            continue
        # Open inbound obligations: someone is still waiting on this agent.
        # Reaping now would orphan their question/task/blocker forever.
        if db.messages.count_documents(
            {"to_instance": name, "to_project": proj, "obligation": "open"}
        ) > 0:
            continue

        db.registered_agents.delete_one({"project": proj, "name": name, "tier": "pending"})
        db.agent_directory.delete_many({"project": proj, "instance": name})
        reaped.append({"project": proj, "agent": name})
        try:
            from shared_memory.audit import log_audit
            log_audit("agent.reaped", name, proj, {
                "tier": "pending",
                "last_seen": str(row.get("last_seen")),
                "grace_hours": _grace_hours(),
            })
        except Exception:
            pass

    if reaped:
        logger.info("pending-agent GC reaped %d ghost(s): %s", len(reaped), reaped)
    return reaped
