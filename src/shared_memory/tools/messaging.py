"""Inter-agent messaging tools - send/receive messages, agent status, discovery."""

import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Set

from mcp.server.fastmcp import Context
from pydantic import AnyUrl

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.config import MESSAGE_CATEGORIES, MESSAGE_PRIORITIES, MESSAGE_STATUSES
from shared_memory.helpers import normalize_project, parse_timestamp, require_session, utc_now
from shared_memory.state import active_sessions, mcp_session_to_app
from shared_memory.tools.projects import _fuzzy_match_agent, _is_project_admin

log = logging.getLogger(__name__)

# ── Phase C2: inbox resource subscriptions ──
# Maps inbox://<project>/<agent> URI → set of ServerSession objects that have
# subscribed via SubscribeRequest. Populated by the subscribe_resource handler
# below; drained on unsubscribe or on send-failure (dead session). Lives in
# process memory because subscriptions are tied to the live HTTP session — a
# disconnect drops the session and the subscription with it.
inbox_subscriptions: Dict[str, Set[Any]] = {}


def inbox_uri(project: str, agent: str) -> str:
    """Canonical inbox URI for a (project, agent) pair.

    Project component is normalized so all subscribers/notifiers share the
    same key in inbox_subscriptions regardless of how they spelled the
    project name on input.
    """
    return f"inbox://{normalize_project(project)}/{agent}"


def _resolve_caller_identity():
    """Look up the calling agent's app session by joining the current MCP
    transport session (from request_context) with the mcp_session_to_app map.

    Returns a dict with project / claude_instance / role / session_id, or
    None if the caller has not started a memory session yet.
    """
    try:
        server_session = mcp._mcp_server.request_context.session
    except (LookupError, AttributeError):
        return None
    app_sid = mcp_session_to_app.get(server_session)
    if app_sid is None:
        return None
    info = active_sessions.get(app_sid)
    if info is None:
        return None
    return {
        "session_id": app_sid,
        "project": normalize_project(info.get("project", "")),
        "claude_instance": info.get("claude_instance", ""),
        "role": info.get("role", "agent"),
    }


def _check_inbox_authz(uri_project: str, uri_agent: str):
    """Authorize a read/subscribe on inbox://<uri_project>/<uri_agent>.

    Returns (ok: bool, reason: str). Allowed if the caller is the named
    agent itself, an admin in the project, or has the cross-project
    'admin' or 'user' role from auth.py. The '*' agent (broadcast inbox)
    is admin-only.
    """
    caller = _resolve_caller_identity()
    if caller is None:
        return False, "no active memory session for this MCP connection"

    if caller["role"] in ("admin", "user"):
        return True, "role"

    db = get_mongo()
    if db is not None and _is_project_admin(db, uri_project, caller["claude_instance"]):
        return True, "project_admin"

    if uri_agent == "*":
        return False, "broadcast inbox URI is admin-only"

    if caller["project"] == uri_project and caller["claude_instance"] == uri_agent:
        return True, "self"

    return False, (
        f"caller {caller['claude_instance']}@{caller['project']} cannot access "
        f"inbox://{uri_project}/{uri_agent}"
    )


def get_tmux_target_for_instance(instance_name: str) -> str:
    """Look up a delivery target for an agent from active sessions.

    The field name is kept for backward compatibility with the external
    dispatcher process, but the value is just an opaque routing string.
    """
    for session_id, info in active_sessions.items():
        if info["claude_instance"] == instance_name and info.get("tmux_target"):
            return info["tmux_target"]
    return None


def get_pending_messages_for_instance(instance_name: str, project: str = None) -> List[Dict]:
    """Get undelivered messages for a specific instance (MongoDB-backed)."""
    db = get_mongo()
    if db is None:
        return []

    instance_match = {
        "$or": [
            {"to_instance": instance_name},
            {"to_instance": "*"}
        ]
    }

    query = {"$and": [instance_match, {"status": "pending"}]}

    # Add project scoping if provided
    if project:
        norm_project = normalize_project(project)
        query["$and"].append({
            "$or": [
                {"to_project": norm_project},
                {"to_project": {"$exists": False}},
                {"to_project": ""},
            ]
        })

    messages = []
    for doc in db.messages.find(query):
        created_at = doc.get("created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        entry = {
            "id": doc["_id"],
            "to": doc.get("to_instance", doc.get("to", "?")),
            "to_project": doc.get("to_project", ""),
            "from_instance": doc.get("from_instance", doc.get("from", "?")),
            "from_project": doc.get("from_project", ""),
            "category": doc.get("category", "info"),
            "message": doc.get("message", ""),
            "priority": doc.get("priority", "normal"),
            "created": created_at,
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        messages.append(entry)

    return messages


# Phase C1: destructive content gate. Body containing any of these keywords
# automatically gets require_human=True so autopilot never auto-acts on it.
# Pattern is intentionally broad — false positives (require_human=True for a
# benign message that mentions "delete") are cheap, false negatives are not.
_DESTRUCTIVE_KEYWORDS = re.compile(
    r"\b(DELETE|DROP|TRUNCATE|deploy|production|prod\b)\b|git\s+push\s+(--force|-f)\b",
    re.IGNORECASE,
)

# Phase C1: hard cap on auto-relay chains. Anything arriving with chain_depth
# above this is dropped with a system-message alert to coordinator.
CHAIN_DEPTH_HARD_CAP = 5


@mcp.tool()
async def memory_send_message(
    session_id: str,
    to_instance: str,
    message: str,
    priority: str = "normal",
    category: str = "info",
    to_project: str = None,
    reply_to: str = None,
    in_response_to: str = None,
    chain_depth: int = None,
    require_human: bool = False,
    ctx: Context = None
) -> str:
    """
    Send a note to another agent in the project.

    Notes are persisted to MongoDB and appear in the recipient's inbox on their
    next session start. Supports full lifecycle tracking: pending, delivered,
    received, completed, failed.

    Args:
        session_id: Your session ID
        to_instance: Target agent name (e.g., 'frontend', 'backend', or '*' for all)
        message: The note content
        priority: Note priority (urgent, normal, low)
        category: Note category - determines how the recipient should handle it:
            contract - exact format/spec that must be followed, no deviation
            task - work assignment
            question - needs a response
            info - FYI, no action needed (default)
            review - look at this and confirm or flag issues
            blocker - STOP work until discussed with coordinator or user
        to_project: Target project (defaults to your project; use for cross-project notes)
        reply_to: Note ID this is replying to (for threading conversations)
        in_response_to: Message ID this is a programmatic auto-response to (Phase C autopilot).
            If set, server computes chain_depth = parent.chain_depth + 1.
        chain_depth: Override chain depth (Phase C autopilot). Server takes the
            max of (parent_depth+1, caller-provided). Hard-capped at 5; messages
            above the cap are dropped with an alert.
        require_human: Force human review on the recipient side. Always True for
            messages whose body matches the destructive-keyword regex (DELETE,
            DROP, TRUNCATE, deploy, production, git push --force).
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in MESSAGE_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {MESSAGE_PRIORITIES}"})

    if category not in MESSAGE_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Must be one of: {MESSAGE_CATEGORIES}"})

    session_info = active_sessions[session_id]
    from_project = normalize_project(session_info.get("project", ""))
    target_project = to_project or from_project
    now = utc_now()

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    # ── Validate target against project registry ──
    # Normalize project name (single source of truth)
    normalized_project = normalize_project(target_project)
    registered_project = db.projects.find_one({"name": normalized_project})

    if registered_project:
        # Project is registered - validate the target agent
        if to_instance != "*":
            registered_agent = db.registered_agents.find_one({
                "project": normalized_project,
                "name": to_instance
            })
            if not registered_agent:
                # Agent not registered - hard reject with suggestions
                suggestions = _fuzzy_match_agent(db, normalized_project, to_instance)
                error_msg = f"Agent '{to_instance}' is not registered in project '{normalized_project}'."
                if suggestions:
                    error_msg += f" Did you mean: {', '.join(suggestions)}?"
                else:
                    # List all valid agents
                    all_agents = [a["name"] for a in db.registered_agents.find(
                        {"project": normalized_project}, {"name": 1}
                    )]
                    if all_agents:
                        error_msg += f" Valid agents: {', '.join(all_agents)}"
                return json.dumps({"error": error_msg})
    # If project not registered, allow message through (backward compatibility)
    # This lets messaging work before projects are fully set up

    # ── Dedup check ──
    # Reject identical messages to same target within 5 minutes
    dedup_window = now - timedelta(minutes=5)
    existing_msg = db.messages.find_one({
        "to_instance": to_instance,
        "to_project": normalized_project,
        "from_instance": session_info["claude_instance"],
        "message": message,
        "created_at": {"$gte": dedup_window}
    })
    if existing_msg:
        return json.dumps({
            "error": "Duplicate message detected. An identical message was sent to this target within the last 5 minutes.",
            "existing_message_id": existing_msg["_id"]
        })

    # ── Human-sender rule (design:human-sender-rule-v0.1) ──
    # When the caller's session is user-tier (Path B soft-auth: only valid
    # api_keys produce role="user"), force chain_depth=0 regardless of any
    # in_response_to parent or caller-supplied value. A user send is by
    # definition the start of a fresh chain even when threaded for UI
    # continuity. Hard-cap check below is naturally bypassed (0 < cap).
    sent_by_human = session_info.get("role") == "user"

    if sent_by_human:
        final_depth = 0
    else:
        # ── Phase C1: chain depth math ──
        # Final depth is max(parent.chain_depth + 1, caller-provided chain_depth, 0).
        parent_depth = -1  # so parent_depth + 1 == 0 when no parent
        if in_response_to:
            parent = db.messages.find_one({"_id": in_response_to}, {"chain_depth": 1})
            if parent and isinstance(parent.get("chain_depth"), int):
                parent_depth = parent["chain_depth"]
        caller_depth = chain_depth if isinstance(chain_depth, int) else 0
        final_depth = max(parent_depth + 1, caller_depth, 0)

    # Hard cap — drop and alert. Coordinator gets a system message so the
    # runaway loop is visible without breaking the calling agent's tool flow.
    if final_depth > CHAIN_DEPTH_HARD_CAP:
        try:
            coordinator = db.registered_agents.find_one({
                "project": normalized_project,
                "tier": "admin",
            })
            if coordinator:
                db.messages.insert_one({
                    "_id": f"msg_{uuid.uuid4().hex[:12]}",
                    "from_instance": "system",
                    "from_project": normalized_project,
                    "to_instance": coordinator["name"],
                    "to_project": normalized_project,
                    "message": (
                        f"CHAIN-DEPTH ALERT: dropped a message from "
                        f"{session_info['claude_instance']}@{from_project} "
                        f"to {to_instance}@{target_project} at depth {final_depth} "
                        f"(cap is {CHAIN_DEPTH_HARD_CAP}). Likely autopilot loop. "
                        f"Investigate and consider memory_pause_autopilot."
                    ),
                    "priority": "urgent",
                    "category": "blocker",
                    "status": "pending",
                    "chain_depth": 0,
                    "require_human": True,
                    "created_at": now,
                })
        except Exception:
            pass  # alert is best-effort; do not let it block the drop response
        return json.dumps({
            "error": f"chain_depth {final_depth} exceeds hard cap {CHAIN_DEPTH_HARD_CAP}",
            "dropped": True,
            "alert_sent": True,
        })

    # ── Phase C1: destructive content gate ──
    # Always force require_human=True if the body contains destructive keywords,
    # even if the caller passed False. False positives are cheap; missing one
    # destructive auto-action is not.
    body_is_destructive = bool(_DESTRUCTIVE_KEYWORDS.search(message))
    final_require_human = bool(require_human) or body_is_destructive

    # ── Build and store message ──
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    msg_doc = {
        "_id": message_id,
        "to_instance": to_instance,
        "to_project": normalized_project,
        "from_instance": session_info["claude_instance"],
        "from_project": from_project,
        "from_session": session_id,
        "message": message,
        "priority": priority,
        "category": category,
        "reply_to": reply_to,
        "in_response_to": in_response_to,
        "chain_depth": final_depth,
        "require_human": final_require_human,
        "sent_by_human": sent_by_human,
        # Legacy field kept during transition. The instance-prefix variant is
        # forgeable; new code should read sent_by_human instead.
        "user_originated": sent_by_human or session_info.get("claude_instance", "").startswith("user-"),
        "status": "pending",
        "created_at": now,
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }

    db.messages.insert_one(msg_doc)

    # ── Phase C2: notify inbox subscribers ──
    # If anyone is subscribed to the recipient's inbox URI, push a
    # notifications/resources/updated. The subscriber re-reads the resource
    # to fetch the new message. For broadcasts (to_instance="*") we fan out
    # to every subscribed inbox in the target project so each individual
    # subscriber sees the broadcast in their own inbox view.
    await _notify_inbox_for_send(msg_doc["to_project"], to_instance)

    return json.dumps({
        "status": "queued",
        "message_id": message_id,
        "to": to_instance,
        "to_project": msg_doc["to_project"],
        "from_project": from_project,
        "priority": priority,
        "category": category,
        "reply_to": reply_to,
        "in_response_to": in_response_to,
        "chain_depth": final_depth,
        "effective_chain_depth": final_depth,
        "sent_by_human": sent_by_human,
        "require_human": final_require_human,
        "destructive_match": body_is_destructive,
        "persisted": True,
    })


@mcp.tool()
async def memory_get_messages(
    session_id: str,
    include_delivered: bool = False,
    limit: int = 20,
    message_id: str = None,
    for_instance: str = None,
    cursor: str = None,
    ctx: Context = None
) -> str:
    """
    Get pending notes for your agent.

    Returns notes addressed to you by other agents. Notes are scoped by project
    — you only see notes sent to your project.

    Args:
        session_id: Your session ID
        include_delivered: Include already delivered notes (default False)
        limit: Maximum notes to return (default 20)
        message_id: Fetch a specific note by ID (admin/coordinator only)
        for_instance: View notes for a different agent in your project (admin/coordinator only)
        cursor: Pagination cursor (created_at ISO string from a previous call's
            next_cursor). Returns the next page of OLDER messages (created_at < cursor).
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    my_instance = session_info["claude_instance"]
    my_project = normalize_project(session_info.get("project", ""))

    db = get_mongo()
    if db is None:
        return json.dumps({"count": 0, "messages": [], "error": "MongoDB unavailable"})

    # ── Direct message lookup by ID (admin/coordinator only) ──
    if message_id:
        is_admin = _is_project_admin(db, my_project, my_instance)
        doc = db.messages.find_one({"_id": message_id})
        if not doc:
            return json.dumps({"error": f"Message '{message_id}' not found"})
        # Non-admins can only see their own messages
        if not is_admin:
            msg_to = doc.get("to_instance", doc.get("to", ""))
            msg_project = doc.get("to_project", "")
            if msg_to != my_instance and msg_to != "*":
                return json.dumps({"error": "Permission denied. Only admins/coordinators can view other agents' messages."})
            if msg_project and msg_project != my_project:
                return json.dumps({"error": "Permission denied. Message belongs to a different project."})
        entry = {
            "id": doc["_id"],
            "from": doc.get("from_instance", doc.get("from", "?")),
            "from_project": doc.get("from_project", ""),
            "to": doc.get("to_instance", doc.get("to", "?")),
            "to_project": doc.get("to_project", ""),
            "category": doc.get("category", "info"),
            "message": doc["message"],
            "priority": doc.get("priority", "normal"),
            "status": doc.get("status", "?"),
            "created": doc["created_at"].isoformat() if doc.get("created_at") else (doc["created"].isoformat() if doc.get("created") else None),
            "chain_depth": doc.get("chain_depth", 0),
            "require_human": bool(doc.get("require_human", False)),
            "user_originated": bool(doc.get("user_originated", False)),
            "sent_by_human": bool(doc.get("sent_by_human", False)),
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        if doc.get("in_response_to"):
            entry["in_response_to"] = doc["in_response_to"]
        return json.dumps({"count": 1, "messages": [entry]})

    # ── Admin/coordinator querying for another agent's messages ──
    target_instance = my_instance
    if for_instance:
        is_admin = _is_project_admin(db, my_project, my_instance)
        if not is_admin:
            return json.dumps({"error": "Permission denied. Only admins/coordinators can view other agents' messages."})
        target_instance = for_instance

    # Build query - match by instance AND project
    # Broadcasts (*) only go to named/admin agents, not workers
    is_worker = target_instance.startswith("worker_")
    if is_worker:
        instance_match = {"to_instance": target_instance}  # Workers only get direct messages
    else:
        instance_match = {
            "$or": [
                {"to_instance": target_instance},
                {"to_instance": "*"}
            ]
        }
    project_match = {
        "$or": [
            {"to_project": my_project},
            {"to_project": {"$exists": False}},  # legacy messages without project
            {"to_project": ""},  # empty project = broadcast
        ]
    }
    query = {"$and": [instance_match, project_match]}

    if not include_delivered:
        query["$and"].append({"status": "pending"})

    # Cursor pagination: cursor = created_at ISO string from previous call's
    # next_cursor. Fetch the page of messages OLDER than that cursor.
    if cursor:
        cursor_dt = parse_timestamp(cursor)
        if cursor_dt is not None:
            query["$and"].append({"created_at": {"$lt": cursor_dt}})

    # Fetch limit+1 to detect "has more" without a separate count query.
    page_size = max(1, int(limit))
    db_cursor = db.messages.find(query).sort([
        ("priority", 1),
        ("created_at", -1)
    ]).limit(page_size + 1)

    raw_docs = list(db_cursor)
    has_more = len(raw_docs) > page_size
    raw_docs = raw_docs[:page_size]

    next_cursor = None
    if has_more and raw_docs:
        last_created = raw_docs[-1].get("created_at")
        if hasattr(last_created, "isoformat"):
            next_cursor = last_created.isoformat()
        elif last_created is not None:
            next_cursor = str(last_created)

    priority_sort = {"urgent": 0, "normal": 1, "low": 2}
    messages = []
    for doc in raw_docs:
        created_at = doc.get("created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        entry = {
            "id": doc["_id"],
            "from": doc.get("from_instance", doc.get("from", "?")),
            "from_project": doc.get("from_project", ""),
            "category": doc.get("category", "info"),
            "message": doc.get("message", ""),
            "priority": doc.get("priority", "normal"),
            "status": doc.get("status", "pending"),
            "created": created_at,
            "delivered": doc.get("status", "pending") != "pending",
            "chain_depth": doc.get("chain_depth", 0),
            "require_human": bool(doc.get("require_human", False)),
            "user_originated": bool(doc.get("user_originated", False)),
            "sent_by_human": bool(doc.get("sent_by_human", False)),
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        if doc.get("in_response_to"):
            entry["in_response_to"] = doc["in_response_to"]
        messages.append(entry)

    # Sort by priority then created
    messages.sort(key=lambda x: (priority_sort.get(x["priority"], 99), x["created"] or ""))

    return json.dumps({
        "count": len(messages),
        "messages": messages,
        "next_cursor": next_cursor,
        "has_more": has_more,
    })


@mcp.tool()
async def memory_update_message_status(
    session_id: str,
    message_id: str,
    status: str,
    ctx: Context = None
) -> str:
    """
    Update a message's lifecycle status.

    Call this to track message progress through the system.

    Args:
        session_id: Your session ID
        message_id: The message ID to update
        status: New status (delivered, received, completed, failed)
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in MESSAGE_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {MESSAGE_STATUSES}"})

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    now = utc_now()
    update = {"status": status}

    # Set appropriate timestamp
    if status == "delivered":
        update["delivered_at"] = now
    elif status == "received":
        update["received_at"] = now
    elif status in ["completed", "failed"]:
        update["completed_at"] = now

    result = db.messages.update_one(
        {"_id": message_id},
        {"$set": update}
    )

    if result.matched_count == 0:
        return json.dumps({"error": f"Message not found: {message_id}"})

    return json.dumps({
        "status": status,
        "message_id": message_id,
        "updated": True
    })


@mcp.tool()
async def memory_acknowledge_message(
    session_id: str,
    message_id: str,
    ctx: Context = None
) -> str:
    """
    Acknowledge receipt of a message (shortcut for status=received).

    Call this after processing a message to mark it handled.

    Args:
        session_id: Your session ID
        message_id: The message ID to acknowledge
    """
    return await memory_update_message_status(session_id, message_id, "received", ctx)


@mcp.tool()
async def memory_heartbeat(
    session_id: str,
    status: str = "idle",
    current_task: str = None,
    ctx: Context = None
) -> str:
    """
    Send a heartbeat to update your agent status.

    Call this periodically to let the system know you're alive.
    Enables load balancing, stuck detection, and routing decisions.

    Args:
        session_id: Your session ID
        status: Current status (idle, busy, error)
        current_task: Description of current task (if busy)
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in ["idle", "busy", "error"]:
        return json.dumps({"error": "Status must be: idle, busy, error"})

    session_info = active_sessions[session_id]
    instance = session_info["claude_instance"]
    now = utc_now()

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    db.agent_status.update_one(
        {"instance": instance},
        {
            "$set": {
                "instance": instance,
                "session_id": session_id,
                "status": status,
                "current_task": current_task,
                "last_heartbeat": now,
                "tmux_target": session_info.get("tmux_target")
            }
        },
        upsert=True
    )

    return json.dumps({
        "status": "ok",
        "instance": instance,
        "agent_status": status,
        "timestamp": now.isoformat()
    })


@mcp.tool()
async def memory_get_agent_status(
    session_id: str,
    instance: str = None,
    ctx: Context = None
) -> str:
    """
    Get status of Claude agents.

    Args:
        session_id: Your session ID
        instance: Specific instance to check (omit for all agents)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    query = {"instance": instance} if instance else {}
    cursor = db.agent_status.find(query)

    agents = []
    now = utc_now()
    for doc in cursor:
        last_hb = parse_timestamp(doc.get("last_heartbeat"))
        stale = False
        if last_hb:
            age_seconds = (now - last_hb).total_seconds()
            stale = age_seconds > 300  # 5 minutes = stale

        agents.append({
            "instance": doc["instance"],
            "status": doc.get("status", "unknown"),
            "current_task": doc.get("current_task"),
            "tmux_target": doc.get("tmux_target"),
            "last_heartbeat": last_hb.isoformat() if last_hb else None,
            "stale": stale
        })

    return json.dumps({
        "count": len(agents),
        "agents": agents
    })


@mcp.tool()
async def memory_list_agents(
    session_id: str,
    project: str = None,
    query: str = None,
    ctx: Context = None
) -> str:
    """
    Discover registered Claude agents across all projects.

    Use this to find out who exists, what they do, and how to reach them.
    Agents auto-register when they call memory_start_session.

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects)
        query: Search term to filter by name or role description
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    mongo_query = {}
    if project:
        mongo_query["project"] = project

    cursor = db.agent_directory.find(mongo_query).sort("last_seen", -1)

    agents = []
    now = utc_now()
    for doc in cursor:
        # If query provided, filter by instance name or role_description
        if query:
            q_lower = query.lower()
            instance_match = q_lower in doc.get("instance", "").lower()
            role_match = q_lower in doc.get("role_description", "").lower()
            project_match = q_lower in doc.get("project", "").lower()
            if not (instance_match or role_match or project_match):
                continue

        last_seen = parse_timestamp(doc.get("last_seen"))
        days_ago = None
        if last_seen:
            days_ago = round((now - last_seen).total_seconds() / 86400, 1)

        agents.append({
            "project": doc.get("project"),
            "instance": doc.get("instance"),
            "role_description": doc.get("role_description", ""),
            "last_seen": last_seen.isoformat() if last_seen else None,
            "days_ago": days_ago,
            "session_count": doc.get("session_count", 0),
            "last_task": doc.get("last_task", ""),
        })

    return json.dumps({
        "count": len(agents),
        "agents": agents
    }, indent=2)


# ──────────────────────────────────────────────────────────────────────────
# Phase C2: inbox resource + subscriptions
# ──────────────────────────────────────────────────────────────────────────

# Default page size when reading an inbox resource. Same as memory_get_messages.
INBOX_DEFAULT_LIMIT = 20


def _format_inbox_message(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one Mongo message doc into the same payload memory_get_messages emits."""
    created_at = doc.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    entry = {
        "id": doc["_id"],
        "from": doc.get("from_instance", doc.get("from", "?")),
        "from_project": doc.get("from_project", ""),
        "to": doc.get("to_instance", doc.get("to", "?")),
        "to_project": doc.get("to_project", ""),
        "category": doc.get("category", "info"),
        "message": doc.get("message", ""),
        "priority": doc.get("priority", "normal"),
        "status": doc.get("status", "pending"),
        "created": created_at,
        "delivered": doc.get("status", "pending") != "pending",
        "chain_depth": doc.get("chain_depth", 0),
        "require_human": bool(doc.get("require_human", False)),
        "user_originated": bool(doc.get("user_originated", False)),
        "sent_by_human": bool(doc.get("sent_by_human", False)),
    }
    if doc.get("reply_to"):
        entry["reply_to"] = doc["reply_to"]
    if doc.get("in_response_to"):
        entry["in_response_to"] = doc["in_response_to"]
    return entry


@mcp.resource(
    "inbox://{project}/{agent}",
    name="inbox",
    description=(
        "Pending messages for an agent in a project. Reading returns the same "
        "payload shape as memory_get_messages plus a next_cursor for pagination "
        "(pass via memory_get_messages cursor= once supported). Subscribe to this "
        "URI to receive notifications/resources/updated when new messages arrive."
    ),
    mime_type="application/json",
)
async def read_inbox(project: str, agent: str) -> str:
    """Return pending messages for inbox://<project>/<agent> as JSON."""
    ok, reason = _check_inbox_authz(project, agent)
    if not ok:
        return json.dumps({
            "uri": inbox_uri(project, agent),
            "error": "permission denied",
            "detail": reason,
        })

    db = get_mongo()
    if db is None:
        return json.dumps({
            "uri": inbox_uri(project, agent),
            "count": 0,
            "messages": [],
            "error": "MongoDB unavailable",
        })

    # Match the targeting rules used by memory_get_messages:
    #  - workers only get direct messages (no broadcasts).
    #  - everyone else gets direct + "*" broadcasts.
    #  - project filter mirrors the legacy/empty-project tolerance.
    is_worker = agent.startswith("worker_")
    if is_worker:
        instance_match = {"to_instance": agent}
    else:
        instance_match = {"$or": [{"to_instance": agent}, {"to_instance": "*"}]}
    project_match = {
        "$or": [
            {"to_project": project},
            {"to_project": {"$exists": False}},
            {"to_project": ""},
        ]
    }
    query = {"$and": [instance_match, project_match, {"status": "pending"}]}

    cursor = db.messages.find(query).sort([
        ("priority", 1),
        ("created_at", -1),
    ]).limit(INBOX_DEFAULT_LIMIT + 1)

    docs = list(cursor)
    has_more = len(docs) > INBOX_DEFAULT_LIMIT
    docs = docs[:INBOX_DEFAULT_LIMIT]

    next_cursor = None
    if has_more and docs:
        last_created = docs[-1].get("created_at")
        if hasattr(last_created, "isoformat"):
            next_cursor = last_created.isoformat()
        elif last_created is not None:
            next_cursor = str(last_created)

    priority_sort = {"urgent": 0, "normal": 1, "low": 2}
    messages = [_format_inbox_message(d) for d in docs]
    messages.sort(key=lambda x: (priority_sort.get(x["priority"], 99), x["created"] or ""))

    return json.dumps({
        "uri": inbox_uri(project, agent),
        "project": project,
        "agent": agent,
        "count": len(messages),
        "messages": messages,
        "next_cursor": next_cursor,
        "has_more": has_more,
    })


def _parse_inbox_uri(uri_str: str) -> tuple:
    """Parse inbox://<project>/<agent> → (project, agent) or (None, None).

    Project component is normalized so case/separator variants of the URI
    resolve to the same subscription bucket.
    """
    m = re.match(r"^inbox://([^/]+)/([^/?#]+)", uri_str)
    if not m:
        return (None, None)
    return (normalize_project(m.group(1)), m.group(2))


async def _notify_inbox_for_send(to_project: str, to_instance: str) -> None:
    """Dispatch ResourceUpdated notifications after memory_send_message.

    Direct messages → notify the named agent's inbox URI.
    Broadcasts (to_instance='*') → notify every subscribed inbox URI in the
    target project, since each subscriber will see the broadcast in their
    own get_messages/inbox view.
    """
    if not to_project:
        return
    if to_instance == "*":
        prefix = f"inbox://{to_project}/"
        targets = []
        for uri in list(inbox_subscriptions.keys()):
            if not uri.startswith(prefix):
                continue
            p, a = _parse_inbox_uri(uri)
            if p and a and a != "*":
                targets.append((p, a))
        for project, agent in targets:
            await _notify_inbox(project, agent)
        return
    await _notify_inbox(to_project, to_instance)


async def _notify_inbox(project: str, agent: str) -> None:
    """Fire notifications/resources/updated to every session subscribed to
    inbox://<project>/<agent>. Drops sessions whose send fails (dead transport).
    Best-effort: a failure here must not break message insert.
    """
    if not project or not agent:
        return
    uri_str = inbox_uri(project, agent)
    sessions = list(inbox_subscriptions.get(uri_str, ()))
    if not sessions:
        return

    try:
        url = AnyUrl(uri_str)
    except Exception:  # pragma: no cover — defensive
        log.warning("inbox: cannot construct AnyUrl for %s", uri_str)
        return

    dead: List[Any] = []
    for session in sessions:
        try:
            await session.send_resource_updated(url)
        except Exception as e:  # session closed / write end gone
            log.debug("inbox: drop dead subscriber for %s: %s", uri_str, e)
            dead.append(session)

    if dead:
        bucket = inbox_subscriptions.get(uri_str)
        if bucket is not None:
            for s in dead:
                bucket.discard(s)
            if not bucket:
                inbox_subscriptions.pop(uri_str, None)


# Subscribe / unsubscribe handlers — registered on the lowlevel server because
# FastMCP exposes a resource decorator but not a subscribe handler. The lowlevel
# `subscribe_resource()` overrides the request handler for SubscribeRequest.
# Inside the handler, mcp._mcp_server.request_context.session is the
# ServerSession that issued the SubscribeRequest — that's the one we need to
# notify on future updates.

@mcp._mcp_server.subscribe_resource()
async def _on_subscribe(uri: AnyUrl) -> None:
    uri_str = str(uri)
    if not uri_str.startswith("inbox://"):
        # Only inbox resources are subscribable for now. No-op for others
        # (the lowlevel server still returns success, matching MCP semantics).
        return
    project, agent = _parse_inbox_uri(uri_str)
    if not project or not agent:
        raise ValueError(f"malformed inbox URI: {uri_str}")

    ok, reason = _check_inbox_authz(project, agent)
    if not ok:
        # Surface a real error so the client doesn't think they subscribed.
        log.warning("inbox: subscribe denied for %s — %s", uri_str, reason)
        raise PermissionError(f"subscribe denied: {reason}")

    try:
        session = mcp._mcp_server.request_context.session
    except LookupError:
        log.warning("inbox: subscribe outside request context for %s", uri_str)
        return
    inbox_subscriptions.setdefault(uri_str, set()).add(session)
    log.info(
        "inbox: subscribed %s/%s (subscribers=%d)",
        project, agent, len(inbox_subscriptions[uri_str]),
    )


@mcp._mcp_server.unsubscribe_resource()
async def _on_unsubscribe(uri: AnyUrl) -> None:
    uri_str = str(uri)
    if not uri_str.startswith("inbox://"):
        return
    try:
        session = mcp._mcp_server.request_context.session
    except LookupError:
        return
    bucket = inbox_subscriptions.get(uri_str)
    if bucket is None:
        return
    bucket.discard(session)
    if not bucket:
        inbox_subscriptions.pop(uri_str, None)
    log.info("inbox: unsubscribed %s (remaining=%d)", uri_str, len(bucket) if bucket else 0)
