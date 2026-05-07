"""Admin tools - API key management and audit log access."""

import json
from datetime import datetime

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.auth import (
    AUTH_ENABLED,
    ROLES,
    create_api_key,
    list_api_keys,
    require_auth,
    revoke_api_key,
)
from shared_memory.helpers import require_session
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_admin(
    session_id: str,
    action: str,
    name: str = None,
    role: str = "agent",
    projects: list = None,
    limit: int = 50,
    event_type: str = None,
    from_project: str = None,
    from_agent: str = None,
    to_project: str = None,
    to_agent: str = None,
    dry_run: bool = True,
    alias_type: str = None,
    ctx: Context = None,
) -> str:
    """
    Admin operations: manage API keys, view audit logs, rename agents/projects.

    Requires 'owner' role when auth is enabled.

    Actions:
        create_key   - Create a new API key (requires name, optional role + projects)
        revoke_key   - Revoke an API key by name
        list_keys    - List all active API keys
        audit_log    - View recent audit log entries (optional event_type filter)
        auth_status  - Check if auth is enabled and current session's role
        rename_agent - Rename an agent across all stores. Args: from_project,
                       from_agent, to_project, to_agent, dry_run (default True).
                       Default dry_run reports impact counts without writing.
        rename_project - Rename a whole project. Args: from_project, to_project,
                       dry_run (default True).
        list_aliases - List active rename aliases. Args: alias_type=agent|project|None.

    Args:
        session_id: Your session ID
        action: see Actions above
        name: Key name (for create_key/revoke_key)
        role: Role for new key (owner, admin, agent, readonly). Default: agent
        projects: List of project names the key can access (empty = all projects)
        limit: Max entries for audit_log (default 50)
        event_type: Filter audit_log by event type (e.g., "auth.login", "spec.created")
        from_project / from_agent: source identity for rename_agent / rename_project
        to_project / to_agent: target identity
        dry_run: rename actions default to dry_run=True; pass dry_run=False to commit
        alias_type: filter for list_aliases ("agent" or "project")
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]

    # Auth check (if enabled, only owner can use admin tools)
    auth_error = require_auth(session_info, "admin")
    if auth_error:
        return json.dumps({"error": auth_error})

    if action == "auth_status":
        return json.dumps({
            "auth_enabled": AUTH_ENABLED,
            "session_role": session_info.get("role", "agent" if not AUTH_ENABLED else "unknown"),
            "allowed_projects": session_info.get("allowed_projects", []),
            "available_roles": ROLES,
        }, indent=2)

    elif action == "create_key":
        if not name:
            return json.dumps({"error": "'name' is required for create_key"})
        if role not in ROLES:
            return json.dumps({"error": f"Invalid role '{role}'. Must be one of: {ROLES}"})

        try:
            raw_key, record = create_api_key(
                name=name,
                role=role,
                projects=projects,
                created_by=session_info.get("claude_instance", "unknown"),
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

        # Audit
        try:
            from shared_memory.audit import log_audit
            log_audit("admin.key_created", session_info.get("claude_instance", "unknown"),
                      "", {"key_name": name, "role": role, "projects": projects or []},
                      session_id)
        except Exception:
            pass

        return json.dumps({
            "status": "created",
            "api_key": raw_key,
            "name": record["name"],
            "role": record["role"],
            "projects": record["projects"],
            "warning": "Save this key now — it cannot be retrieved later.",
        }, indent=2)

    elif action == "revoke_key":
        if not name:
            return json.dumps({"error": "'name' is required for revoke_key"})

        success = revoke_api_key(name)

        if success:
            try:
                from shared_memory.audit import log_audit
                log_audit("admin.key_revoked", session_info.get("claude_instance", "unknown"),
                          "", {"key_name": name}, session_id)
            except Exception:
                pass

        return json.dumps({
            "status": "revoked" if success else "not_found",
            "name": name,
        })

    elif action == "list_keys":
        keys = list_api_keys()
        # Serialize datetime objects
        for k in keys:
            for field in ("created", "last_used"):
                if isinstance(k.get(field), datetime):
                    k[field] = k[field].isoformat()
                elif k.get(field) is None:
                    k[field] = ""
        return json.dumps({"keys": keys, "count": len(keys)}, indent=2)

    elif action == "audit_log":
        from shared_memory.clients import get_mongo

        db = get_mongo()
        if db is None:
            return json.dumps({"error": "MongoDB not available"})

        query = {}
        if event_type:
            query["event_type"] = event_type

        entries = list(
            db.audit_log.find(query, {"_id": 0})
            .sort("timestamp", -1)
            .limit(min(limit, 200))
        )

        for e in entries:
            if isinstance(e.get("timestamp"), datetime):
                e["timestamp"] = e["timestamp"].isoformat()

        return json.dumps({"entries": entries, "count": len(entries)}, indent=2)

    elif action in ("rename_agent", "rename_project", "list_aliases"):
        from shared_memory.clients import get_chroma, get_mongo
        from shared_memory.tools.rename import (
            list_aliases,
            perform_rename_agent,
            perform_rename_project,
        )

        db = get_mongo()
        if db is None:
            return json.dumps({"error": "MongoDB not available"})

        actor = session_info.get("claude_instance", "unknown")

        if action == "list_aliases":
            return json.dumps({
                "aliases": list_aliases(db, alias_type=alias_type),
            }, indent=2, default=str)

        chroma = await get_chroma()

        if action == "rename_agent":
            result = await perform_rename_agent(
                db, chroma,
                from_project=from_project, from_agent=from_agent,
                to_project=to_project, to_agent=to_agent,
                dry_run=dry_run, actor=actor, session_id=session_id,
            )
            return json.dumps(result, indent=2, default=str)

        # rename_project
        result = await perform_rename_project(
            db, chroma,
            from_project=from_project, to_project=to_project,
            dry_run=dry_run, actor=actor, session_id=session_id,
        )
        return json.dumps(result, indent=2, default=str)

    else:
        return json.dumps({
            "error": f"Unknown action '{action}'. Use: create_key, revoke_key, list_keys, audit_log, auth_status, rename_agent, rename_project, list_aliases"
        })
