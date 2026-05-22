"""Push control read-side tools — `design:push-control-v0` v1.1.0 §5, §7.

Exposes two MCP tools for the claudeControl dashboard:
  - memory_list_alerts: list recent alerts, filterable by ack state.
  - memory_get_emission_stats: per-agent current-hour emission count +
    config thresholds, for the per-agent counter display.

Write-side operations (config CRUD, alert ack, agent unsuspend) live under
`memory_admin` sub-actions in tools/admin.py — they are owner/operator-tier
and gated behind the existing admin auth surface.
"""

import json

from mcp.server.fastmcp import Context

from shared_memory import push_control
from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import require_session


@mcp.tool()
async def memory_list_alerts(
    session_id: str,
    unacknowledged_only: bool = False,
    project: str = None,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """List push-control alerts, newest first.

    Designed for claudeControl's alert dashboard. Default behavior returns
    the most recent 50 alerts regardless of ack state — claudeControl uses
    `unacknowledged_only=True` for the active-incident feed.

    Args:
        session_id: Your session ID.
        unacknowledged_only: Return only alerts with acknowledged=False.
            Use this for the polling fallback that catches missed webhooks.
        project: Filter to alerts for agents in this project.
        limit: Max alerts to return (default 50, capped at 200).

    Auth: any agent or user-tier session can call. There is no per-agent
    filtering — alerts are operator-facing data, not message-bus content.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    limit = max(1, min(int(limit), 200))
    alerts = push_control.list_alerts(
        db=db,
        unacknowledged_only=bool(unacknowledged_only),
        project=project,
        limit=limit,
    )

    # Trim sample bodies for transport (full bodies are in the alert doc,
    # accessible via direct Mongo if needed). 200 chars per sample is plenty
    # for dashboard preview.
    for a in alerts:
        for s in a.get("sample_messages", []) or []:
            body = s.get("message", "")
            if isinstance(body, str) and len(body) > 200:
                s["message"] = body[:200] + "…"
            ts = s.get("created_at")
            if hasattr(ts, "isoformat"):
                s["created_at"] = ts.isoformat()

    return json.dumps({
        "count": len(alerts),
        "alerts": alerts,
    }, default=str)


@mcp.tool()
async def memory_get_emission_stats(
    session_id: str,
    agent: str = None,
    project: str = None,
    ctx: Context = None,
) -> str:
    """Current-hour emission count + thresholds for one or all agents.

    Designed for the claudeControl per-agent counter dashboard (§5). Returns
    a list of `{instance, project, hour, count, depth_cap, push_budget,
    hard_ceiling, suspended}` entries.

    Args:
        session_id: Your session ID.
        agent: Filter to one specific agent (omit for all current-hour senders).
        project: Filter to senders in one project (omit for all).

    Auth: any agent or user-tier session can call. Counters are not
    sensitive — they describe message volume, not message content.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()

    snapshot = push_control.snapshot_emission_counters()
    out = []
    for entry in snapshot:
        if agent and entry["instance"] != agent:
            continue
        if project and entry["project"] != project:
            continue
        # Resolve the agent's effective config (depth_cap/push_budget/hard_ceiling
        # are project-scoped; suspension is per-agent). PyMongo Database does
        # not implement __bool__, so `is not None` is required.
        cfg = push_control.get_effective_config(db, entry["project"]) if db is not None else {}
        suspended = push_control.is_agent_suspended(db, entry["project"], entry["instance"])
        out.append({
            **entry,
            "depth_cap": cfg.get("depth_cap"),
            "push_budget": cfg.get("push_budget"),
            "hard_ceiling": cfg.get("hard_ceiling"),
            "suspended": suspended,
            # Quick-render fields for the dashboard
            "over_push_budget": entry["count"] > (cfg.get("push_budget") or 9_999_999),
            "over_hard_ceiling": entry["count"] >= (cfg.get("hard_ceiling") or 9_999_999),
        })

    # Stable sort: most-emitting agents first.
    out.sort(key=lambda x: -x["count"])

    return json.dumps({
        "count": len(out),
        "stats": out,
    }, default=str)
