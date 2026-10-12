"""Author context-fill stamps on learnings and corrections.

Tom-approved ask (coordinator msg_5b394f48b4d2, 2026-10-12): test whether agent
mistakes cluster at high context fill. The server already receives each
agent's context size every ~60s from the box reporters (memory_report_context
-> agent_status), so the stamp needs no client change.

One row per event in `context_stamps`:
  {doc_id, kind: "learning" | "status_change", new_status?, agent, project,
   ts, context_tokens, context_window, context_reported_at, known}
known=False (tokens/window None, never 0) when the agent has no gauge row or
its last report is older than FRESH_SECONDS.
"""

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

FRESH_SECONDS = 600


def current_context(db, agent: str, now) -> Dict[str, Any]:
    """The agent's freshly reported context, or known=False."""
    from shared_memory.helpers import parse_timestamp

    unknown = {"known": False, "context_tokens": None, "context_window": None,
               "context_reported_at": None}
    if db is None or not agent:
        return unknown
    try:
        doc = db.agent_status.find_one(
            {"instance": agent},
            {"context_tokens": 1, "context_window": 1, "context_reported_at": 1})
    except Exception:
        return unknown
    tokens = (doc or {}).get("context_tokens")
    reported = parse_timestamp((doc or {}).get("context_reported_at"))
    if not isinstance(tokens, int) or tokens <= 0 or reported is None:
        return unknown
    if now - reported > timedelta(seconds=FRESH_SECONDS):
        return dict(unknown, context_reported_at=reported)
    window = doc.get("context_window")
    return {"known": True, "context_tokens": tokens,
            "context_window": window if isinstance(window, int) and window > 0 else None,
            "context_reported_at": reported}


def stamp(db, session_id: str, kind: str, doc_id: str,
          new_status: Optional[str] = None) -> None:
    """Record one stamp for the session's agent. db=None fetches the shared
    client inside the guard. Never raises."""
    try:
        from shared_memory.helpers import utc_now
        from shared_memory.state import active_sessions

        if db is None:
            from shared_memory.clients import get_mongo
            db = get_mongo()
        if db is None:
            return
        info = active_sessions.get(session_id) or {}
        agent = info.get("claude_instance")
        now = utc_now()
        row = {"doc_id": doc_id, "kind": kind, "agent": agent,
               "project": info.get("project"), "ts": now}
        if new_status is not None:
            row["new_status"] = new_status
        row.update(current_context(db, agent, now))
        db.context_stamps.insert_one(row)
    except Exception as e:  # noqa: BLE001
        log.warning("context_stamp: failed for %s (%s)", doc_id, e)
