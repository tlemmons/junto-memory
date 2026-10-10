"""Idle→park nudge (design:idle-park-nudge-v0; coordinator msg_177ffff64ec1, Tom 2026-10-10).

Why: Claude Code's prompt cache expires ~60 min after an agent's last turn. A
big-context agent that is typed into after that pays a full-price cache
rebuild. CC 2.1.296 tried to solve this with idle compaction; the fleet turned
that off (learning_c92c3e1080238427). Instead, the server nudges an agent that
has gone quiet for `idle_park_minutes` (default 50, i.e. still cache-warm) to
park, so the park turn runs cheap and the operator returns to a parked agent
(/clear + go) rather than a stale 800K context.

Mechanics:
- ONE timer per (project, agent name) — not per session; agents often run
  2-3 sessions. In-memory only: a server restart clears every timer, and the
  agent's next real call re-arms it (a restart is not a reason to nudge).
- Armed/reset by every REAL tool call, observed at the CallToolRequest
  chokepoint (app.py). Background traffic does NOT count: the junto-inbox
  plugin's heartbeat / health / emission-stats / start_session (reconnects)
  and the per-box context reporter (memory_report_context, every 60s). A call
  with no resolvable session is ignored.
- Fires ONCE per idle period. The agent's own reply turn to the nudge makes
  junto calls; calls within REARM_GRACE_SECONDS of the nudge do not re-arm, so
  an agent that reads the nudge and decides not to park is not re-nudged every
  hour while it stays idle. Real activity after the grace re-arms.
- memory_end_session disarms the timer (parked agents are never nudged).
- Opt-out: push_control config `idle_park_minutes=0` (per project or server
  default) disables; `idle_park_optout` is a comma-separated agent list for
  always-on agents.

Delivery: an is_system_notice message (kind idle_park_nudge) → INJECT push via
the normal inbox path, so the full body reaches the agent's channel.
"""

import asyncio
import logging
import time
import uuid
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Tools the junto-inbox plugin / context reporter call on their own schedule.
# These must never reset the idle timer, or it would never fire.
BACKGROUND_TOOLS = frozenset({
    "memory_heartbeat",
    "memory_health",
    "memory_start_session",
    "memory_report_context",
    "memory_get_emission_stats",
})

SWEEP_INTERVAL_SECONDS = 60
REARM_GRACE_SECONDS = 600

NUDGE_SUBJECT = "Idle — park now if at a clean stop (cache expires soon)"
NUDGE_BODY = (
    "Idle {minutes} min: park now (memory_get_skill('parking')) if you're at a "
    "clean stopping point — your prompt cache expires at ~60 min idle, and "
    "parking while it's warm is cheap. If you're mid-task or the user is "
    "actively working with you, ignore this. (One nudge per idle period; "
    "opt out via push_control config idle_park_optout.)"
)

# (project, agent) -> {"last_real": monotonic, "nudged_at": monotonic|None}
_timers: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}

_sweep_task: Optional[asyncio.Task] = None


def _identity_for_session(session_id) -> Optional[Tuple[str, str]]:
    from shared_memory.state import active_sessions

    if not isinstance(session_id, str):
        return None
    info = active_sessions.get(session_id)
    if not info:
        return None
    project = info.get("project")
    agent = info.get("claude_instance")
    if not project or not agent:
        return None
    return project, agent


def note_tool_call(tool_name: str, session_id, now: Optional[float] = None) -> None:
    """Record a tool call against its agent's idle timer (CallToolRequest hook)."""
    if tool_name in BACKGROUND_TOOLS:
        return
    ident = _identity_for_session(session_id)
    if ident is None:
        return
    now = time.monotonic() if now is None else now
    if tool_name == "memory_end_session":
        _timers.pop(ident, None)
        return
    timer = _timers.get(ident)
    if timer is None:
        _timers[ident] = {"last_real": now, "nudged_at": None}
        return
    nudged_at = timer.get("nudged_at")
    if nudged_at is not None and now - nudged_at < REARM_GRACE_SECONDS:
        # The agent's response turn to the nudge — don't re-arm off it.
        return
    timer["last_real"] = now
    timer["nudged_at"] = None


def due_agents(minutes_for, now: Optional[float] = None):
    """Return (project, agent, idle_minutes) for timers due to fire.

    `minutes_for(project, agent)` returns the threshold in minutes, or 0/None
    when the agent is opted out.
    """
    now = time.monotonic() if now is None else now
    due = []
    for (project, agent), timer in list(_timers.items()):
        if timer.get("nudged_at") is not None:
            continue
        minutes = minutes_for(project, agent)
        if not minutes:
            continue
        if now - timer["last_real"] >= minutes * 60:
            due.append((project, agent, minutes))
    return due


def mark_nudged(project: str, agent: str, now: Optional[float] = None) -> None:
    timer = _timers.get((project, agent))
    if timer is not None:
        timer["nudged_at"] = time.monotonic() if now is None else now


def _threshold_from_config(db, project: str, agent: str) -> int:
    from shared_memory.push_control import get_effective_config

    cfg = get_effective_config(db, project)
    minutes = cfg.get("idle_park_minutes") or 0
    optout = cfg.get("idle_park_optout") or ""
    if agent in {a.strip() for a in str(optout).split(",") if a.strip()}:
        return 0
    return int(minutes)


async def _send_nudge(db, project: str, agent: str, minutes: int) -> Optional[str]:
    from shared_memory.helpers import utc_now
    from shared_memory.tools.messaging import _build_announce_packet, _notify_inbox_for_send

    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    doc = {
        "_id": message_id,
        "to_instance": agent,
        "to_project": project,
        "from_instance": "system",
        "from_project": project,
        "from_session": None,
        "subject": NUDGE_SUBJECT,
        "message": NUDGE_BODY.format(minutes=minutes),
        "priority": "normal",
        "category": "info",
        "reply_to": None,
        "in_response_to": None,
        "chain_depth": 0,
        "require_human": False,
        "sent_by_human": False,
        "human_interacted": False,
        "user_originated": False,
        "status": "pending",
        "push_suppressed": False,
        "push_suppress_reason": None,
        "emission_count": 0,
        "recency_bypass": False,
        "is_system_notice": True,
        "system_notice_kind": "idle_park_nudge",
        "created_at": utc_now(),
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }
    db.messages.insert_one(doc)
    try:
        await _notify_inbox_for_send(project, agent, _build_announce_packet(doc))
    except Exception as e:
        # Message is persisted; pull-side delivery still works.
        log.warning("idle-park: push failed for %s/%s: %s", agent, project, e)
    return message_id


async def sweep_once(db) -> int:
    """Nudge every due agent once. Returns the number of nudges sent."""
    if db is None:
        return 0
    sent = 0
    for project, agent, minutes in due_agents(lambda p, a: _threshold_from_config(db, p, a)):
        # Mark first: a failed insert must not turn into a nudge every minute.
        mark_nudged(project, agent)
        try:
            msg_id = await _send_nudge(db, project, agent, minutes)
            sent += 1
            log.info("idle-park: nudged %s/%s after %s min idle (%s)", agent, project, minutes, msg_id)
        except Exception as e:
            log.error("idle-park: nudge failed for %s/%s: %s", agent, project, e)
    return sent


async def _sweep_loop() -> None:
    from shared_memory.clients import get_mongo

    log.info("idle-park: started (sweep=%ss, rearm_grace=%ss)", SWEEP_INTERVAL_SECONDS, REARM_GRACE_SECONDS)
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await sweep_once(get_mongo())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("idle-park: loop error: %s", e)


def start_idle_park() -> None:
    """Start the sweep task. Idempotent if already running."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="idle_park_sweep")
    log.info("idle-park: background task created")


def build_call_tool_handler_with_idle_park(orig_handler):
    """Wrap the CallToolRequest handler to feed note_tool_call."""

    async def wrapped(req):
        try:
            args = getattr(req.params, "arguments", None)
            session_id = args.get("session_id") if isinstance(args, dict) else None
            note_tool_call(getattr(req.params, "name", ""), session_id)
        except Exception as e:  # bookkeeping must never break a tool call
            log.debug("idle-park: note_tool_call failed: %s", e)
        return await orig_handler(req)

    return wrapped
