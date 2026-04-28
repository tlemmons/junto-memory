"""Autopilot tools — per-(project, agent) auto-relay configuration.

Phase C1: CRUD surface (set/pause/status/digest).
Phase C2: budget enforcement via memory_autopilot_check_budget. Channel
plugins call this immediately before auto-processing a message; the server
counts the auto-event and returns whether autopilot is still allowed. On
budget breach, server auto-disables autopilot for that (project, agent)
pair and sends a system message to the agent's inbox.

Configuration is a single document per (project, agent) in `agent_autopilot`:
    {
        project: str,
        agent: str,
        enabled: bool,
        depth_cap: int,        # max chain_depth this agent will auto-process
        hourly_budget: int,    # max auto-processed messages per hour
        destructive_gate: bool # when True, require_human messages go through
                               # to the human regardless of enabled
        updated: datetime,
        updated_by: str,
    }
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import require_session, utc_now
from shared_memory.state import active_sessions


def _autopilot_config(db, project: str, agent: str) -> dict:
    """Return the autopilot config doc, falling back to defaults."""
    doc = db.agent_autopilot.find_one({"project": project, "agent": agent}) or {}
    return {
        "project": project,
        "agent": agent,
        "enabled": bool(doc.get("enabled", False)),
        "depth_cap": int(doc.get("depth_cap", 1)),
        "hourly_budget": int(doc.get("hourly_budget", 10)),
        "destructive_gate": bool(doc.get("destructive_gate", True)),
        "updated": doc.get("updated"),
        "updated_by": doc.get("updated_by", ""),
        "paused_at": doc.get("paused_at"),
        "paused_reason": doc.get("paused_reason", ""),
    }


def _format_dt(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@mcp.tool()
async def memory_set_autopilot(
    session_id: str,
    project: str,
    agent: str,
    enabled: bool = None,
    depth_cap: int = None,
    hourly_budget: int = None,
    destructive_gate: bool = None,
    ctx: Context = None,
) -> str:
    """
    Configure autopilot for a (project, agent) pair.

    Autopilot lets channel plugins (e.g., ClaudeTerminal cterm-inbox) auto-process
    inbound messages without human prompting. The server enforces:
      - depth_cap: messages with chain_depth > depth_cap require human review
      - hourly_budget: rolling 1-hour count of auto-processed messages
        (enforced in Phase C2; advisory in C1)
      - destructive_gate: when True, messages flagged require_human always go
        to the human regardless of `enabled`

    Args:
        session_id: Your session ID
        project: Target project
        agent: Target agent within that project
        enabled: Turn autopilot on or off
        depth_cap: Max chain_depth to auto-process (default 1 — only direct
            human-originated messages count, replies go to human)
        hourly_budget: Max auto-processed messages per hour (default 10)
        destructive_gate: Force human review on require_human messages (default True;
            recommended to leave True)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    session_info = active_sessions[session_id]
    actor = session_info.get("claude_instance", "unknown")

    update: dict = {"updated": utc_now(), "updated_by": actor}
    if enabled is not None:
        update["enabled"] = bool(enabled)
    if depth_cap is not None:
        update["depth_cap"] = max(0, int(depth_cap))
    if hourly_budget is not None:
        update["hourly_budget"] = max(0, int(hourly_budget))
    if destructive_gate is not None:
        update["destructive_gate"] = bool(destructive_gate)

    # Re-enabling? Clear the paused_at/paused_reason fields so a future
    # auto-disable-on-budget-breach is recorded fresh.
    if update.get("enabled") is True:
        update["paused_at"] = None
        update["paused_reason"] = ""

    db.agent_autopilot.update_one(
        {"project": project, "agent": agent},
        {
            "$set": update,
            "$setOnInsert": {"project": project, "agent": agent},
        },
        upsert=True,
    )

    return json.dumps(
        {
            "status": "ok",
            **{k: _format_dt(v) for k, v in _autopilot_config(db, project, agent).items()},
        },
        indent=2,
    )


@mcp.tool()
async def memory_pause_autopilot(
    session_id: str,
    project: str,
    agent: str,
    reason: str = "",
    ctx: Context = None,
) -> str:
    """
    Pause autopilot for a (project, agent) pair without losing config.

    Equivalent to memory_set_autopilot(enabled=False) plus a paused_reason
    note that persists across re-enables. Use when you want to temporarily
    halt auto-processing for an agent (debugging, runaway loop, deploy
    window) without forgetting the depth_cap / hourly_budget tuning.

    Args:
        session_id: Your session ID
        project: Target project
        agent: Target agent
        reason: Why autopilot was paused (shown in status output)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    session_info = active_sessions[session_id]
    actor = session_info.get("claude_instance", "unknown")

    db.agent_autopilot.update_one(
        {"project": project, "agent": agent},
        {
            "$set": {
                "enabled": False,
                "paused_at": utc_now(),
                "paused_reason": reason,
                "updated": utc_now(),
                "updated_by": actor,
            },
            "$setOnInsert": {
                "project": project,
                "agent": agent,
                "depth_cap": 1,
                "hourly_budget": 10,
                "destructive_gate": True,
            },
        },
        upsert=True,
    )

    return json.dumps(
        {
            "status": "paused",
            **{k: _format_dt(v) for k, v in _autopilot_config(db, project, agent).items()},
        },
        indent=2,
    )


@mcp.tool()
async def memory_autopilot_status(
    session_id: str,
    project: str = None,
    agent: str = None,
    ctx: Context = None,
) -> str:
    """
    Inspect autopilot configuration.

    Without filters, returns every configured (project, agent) pair. With
    project only, returns all agents in that project. With both, returns
    just that one config (or defaults if no config exists).

    Args:
        session_id: Your session ID
        project: Optional project filter
        agent: Optional agent filter (requires project)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    if project and agent:
        return json.dumps(
            {k: _format_dt(v) for k, v in _autopilot_config(db, project, agent).items()},
            indent=2,
        )

    query: dict = {}
    if project:
        query["project"] = project

    configs = []
    for doc in db.agent_autopilot.find(query).sort([("project", 1), ("agent", 1)]):
        configs.append(
            {
                "project": doc.get("project"),
                "agent": doc.get("agent"),
                "enabled": bool(doc.get("enabled", False)),
                "depth_cap": int(doc.get("depth_cap", 1)),
                "hourly_budget": int(doc.get("hourly_budget", 10)),
                "destructive_gate": bool(doc.get("destructive_gate", True)),
                "updated": _format_dt(doc.get("updated")),
                "updated_by": doc.get("updated_by", ""),
                "paused_at": _format_dt(doc.get("paused_at")),
                "paused_reason": doc.get("paused_reason", ""),
            }
        )

    return json.dumps({"count": len(configs), "configs": configs}, indent=2)


@mcp.tool()
async def memory_autopilot_digest(
    session_id: str,
    project: str,
    agent: str,
    hours: int = 24,
    ctx: Context = None,
) -> str:
    """
    Summarize autopilot activity for a (project, agent) over a recent window.

    Counts messages received and broken down by chain_depth, require_human,
    and user_originated. Useful for tuning depth_cap and hourly_budget.

    Args:
        session_id: Your session ID
        project: Target project
        agent: Target agent
        hours: Lookback window in hours (default 24)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))

    base_query = {
        "to_project": project,
        "to_instance": agent,
        "created_at": {"$gte": cutoff},
    }

    total = db.messages.count_documents(base_query)
    require_human = db.messages.count_documents({**base_query, "require_human": True})
    user_originated = db.messages.count_documents({**base_query, "user_originated": True})
    sent_by_human = db.messages.count_documents({**base_query, "sent_by_human": True})

    by_depth = {}
    for doc in db.messages.find(base_query, {"chain_depth": 1}):
        depth = doc.get("chain_depth", 0) or 0
        by_depth[str(depth)] = by_depth.get(str(depth), 0) + 1

    config = _autopilot_config(db, project, agent)

    return json.dumps(
        {
            "project": project,
            "agent": agent,
            "window_hours": hours,
            "since": cutoff.isoformat(),
            "totals": {
                "all_messages": total,
                "require_human": require_human,
                "user_originated": user_originated,
                "sent_by_human": sent_by_human,
            },
            "by_chain_depth": by_depth,
            "current_config": {k: _format_dt(v) for k, v in config.items()},
        },
        indent=2,
    )


@mcp.tool()
async def memory_autopilot_check_budget(
    session_id: str,
    project: str,
    agent: str,
    message_id: str = None,
    chain_depth: int = 0,
    require_human: bool = False,
    ctx: Context = None,
) -> str:
    """
    Check budget + record an auto-processing event in one atomic call.

    Channel plugins (e.g., ClaudeTerminal cterm-inbox) call this IMMEDIATELY
    before auto-processing a message. Returns:
        {allowed: bool, reason: str, current_count: int, hourly_budget: int,
         depth_cap: int, depth_breach: bool, destructive_block: bool}

    The server counts every call (whether allowed or not) so observability
    of attempted breaches is preserved. Auto-disable happens when count
    EXCEEDS hourly_budget — server flips enabled=False, writes paused_at,
    and sends a system blocker message to the agent's inbox.

    Decision matrix:
      - autopilot disabled       → allowed=False, no count increment
      - destructive_gate=True    → allowed=False if require_human (no increment)
      - chain_depth > depth_cap  → allowed=False (counted, breach noted)
      - count >= hourly_budget   → allowed=False, AUTO-DISABLE side-effect
      - otherwise                → allowed=True, count incremented

    Args:
        session_id: Your session ID
        project: Target (project, agent) pair
        agent: Target (project, agent) pair
        message_id: The message about to be auto-processed (logged for audit)
        chain_depth: chain_depth of the message (compared against depth_cap)
        require_human: require_human flag of the message (gated by destructive_gate)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    config = _autopilot_config(db, project, agent)
    now = utc_now()

    # Gate 1: autopilot must be enabled
    if not config["enabled"]:
        return json.dumps(
            {
                "allowed": False,
                "reason": "autopilot disabled",
                "current_count": 0,
                "hourly_budget": config["hourly_budget"],
                "depth_cap": config["depth_cap"],
                "depth_breach": False,
                "destructive_block": False,
            }
        )

    # Gate 2: destructive content always blocks if destructive_gate is on
    if config["destructive_gate"] and bool(require_human):
        # Don't count: this is a "this needs a human" decision, not a budget event
        return json.dumps(
            {
                "allowed": False,
                "reason": "destructive_gate blocked require_human message",
                "current_count": 0,
                "hourly_budget": config["hourly_budget"],
                "depth_cap": config["depth_cap"],
                "depth_breach": False,
                "destructive_block": True,
            }
        )

    # Gate 2.5: sent-by-human bypass (design:human-sender-rule-v0.1).
    # Look up the message; if it was sent by a user-tier session, bypass
    # depth + budget gates entirely. The destructive gate above already had
    # its turn — destructive content from a human still requires explicit
    # human review on the receiver side. Server-enforced (forgery-resistant)
    # rather than client-trusted: a rogue/buggy channel plugin cannot claim
    # sent_by_human to skip gating.
    if message_id:
        msg_doc = db.messages.find_one(
            {"_id": message_id}, {"sent_by_human": 1}
        )
        if msg_doc and msg_doc.get("sent_by_human"):
            return json.dumps(
                {
                    "allowed": True,
                    "reason": "sent_by_human bypass",
                    "current_count": 0,
                    "hourly_budget": config["hourly_budget"],
                    "depth_cap": config["depth_cap"],
                    "depth_breach": False,
                    "destructive_block": False,
                    "sent_by_human_bypass": True,
                }
            )

    # Gate 3: depth cap (counted as an attempted auto-process)
    depth_breach = int(chain_depth or 0) > config["depth_cap"]

    # Count first so attempted breaches are visible in metrics
    cutoff = now - timedelta(hours=1)
    db.autopilot_events.insert_one(
        {
            "_id": f"ape_{uuid.uuid4().hex[:12]}",
            "project": project,
            "agent": agent,
            "message_id": message_id,
            "chain_depth": int(chain_depth or 0),
            "require_human": bool(require_human),
            "depth_breach": depth_breach,
            "logged_at": now,
        }
    )
    current_count = db.autopilot_events.count_documents(
        {"project": project, "agent": agent, "logged_at": {"$gte": cutoff}}
    )

    if depth_breach:
        return json.dumps(
            {
                "allowed": False,
                "reason": f"chain_depth {chain_depth} exceeds depth_cap {config['depth_cap']}",
                "current_count": current_count,
                "hourly_budget": config["hourly_budget"],
                "depth_cap": config["depth_cap"],
                "depth_breach": True,
                "destructive_block": False,
            }
        )

    # Gate 4: budget. Auto-disable on breach.
    budget_breached = current_count > config["hourly_budget"]
    if budget_breached:
        # Atomic flip — multiple concurrent breaches just race to set the same fields
        db.agent_autopilot.update_one(
            {"project": project, "agent": agent},
            {
                "$set": {
                    "enabled": False,
                    "paused_at": now,
                    "paused_reason": (
                        f"hourly budget breached: {current_count} auto-processes "
                        f"in last hour > budget {config['hourly_budget']}"
                    ),
                    "updated": now,
                    "updated_by": "system:autopilot-budget",
                }
            },
        )
        # System notification to the agent — uses send_message-style insert directly
        # to avoid recursive memory_send_message overhead and dedup guards.
        try:
            db.messages.insert_one(
                {
                    "_id": f"msg_{uuid.uuid4().hex[:12]}",
                    "from_instance": "system",
                    "from_project": project,
                    "to_instance": agent,
                    "to_project": project,
                    "message": (
                        f"AUTOPILOT AUTO-DISABLED: hourly budget breached "
                        f"({current_count} auto-processes in last hour > "
                        f"budget {config['hourly_budget']}). "
                        f"Use memory_set_autopilot(enabled=True) to resume "
                        f"after reviewing recent activity with "
                        f"memory_autopilot_digest."
                    ),
                    "priority": "urgent",
                    "category": "blocker",
                    "status": "pending",
                    "chain_depth": 0,
                    "require_human": True,
                    "user_originated": False,
                    "created_at": now,
                }
            )
        except Exception:
            pass  # alert is best-effort

        return json.dumps(
            {
                "allowed": False,
                "reason": "hourly budget breached — autopilot auto-disabled",
                "current_count": current_count,
                "hourly_budget": config["hourly_budget"],
                "depth_cap": config["depth_cap"],
                "depth_breach": False,
                "destructive_block": False,
                "auto_disabled": True,
            }
        )

    # Allowed
    return json.dumps(
        {
            "allowed": True,
            "reason": "ok",
            "current_count": current_count,
            "hourly_budget": config["hourly_budget"],
            "depth_cap": config["depth_cap"],
            "depth_breach": False,
            "destructive_block": False,
        }
    )
