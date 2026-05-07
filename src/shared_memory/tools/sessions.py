"""Session management tools - start and end Claude sessions."""

import json
import uuid
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.helpers import (
    _match_path_patterns,
    cleanup_stale_sessions,
    generate_doc_id,
    get_blocking_others,
    get_interface_updates,
    get_pending_signals,
    get_project_collection,
    get_recent_modifications,
    get_relevant_locks_for_session,
    get_shared_collection,
    normalize_project,
    release_session_locks,
    require_session,
    utc_now,
    utc_now_iso,
)
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_start_session(
    project: str,
    claude_instance: str = "unknown",
    task_description: str = "",
    tmux_target: str = None,
    role_description: str = None,
    working_directory: str = None,
    spawned_by: str = None,
    api_key: str = None,
    ctx: Context = None
) -> str:
    """
    START HERE - Call this first before any other memory tools.

    Registers your session and returns:
    - Your session ID (required for all other calls)
    - Recent relevant learnings for your project
    - Active work by other Claudes (avoid conflicts)
    - Handoff notes from previous sessions
    - Active file locks in your working area
    - Signals from other agents (completion notifications)
    - Who you might be blocking
    - Interface updates since your last session

    You MUST call this at the start of your work.

    Args:
        project: Project you're working on (e.g., 'emailtriage', 'nimbus')
        claude_instance: Identifier for this agent (e.g., 'main', 'agent-1')
        task_description: Brief description of what you're about to work on
        tmux_target: Optional delivery target routing string (opaque, used by
            external dispatcher; leave blank unless your setup uses one)
        role_description: What this agent does (e.g., 'Core triage/classification engine').
            Set once - persists across sessions. Other agents can discover you via memory_list_agents.
        working_directory: Your working directory path. Used to auto-identify your agent name
            if path patterns are registered for this project.
        spawned_by: Parent agent that spawned this worker (for worker tier agents).
        api_key: API key for authentication (required when MCP_AUTH_ENABLED=true).
    """
    # Cleanup stale sessions on each new session start
    cleanup_stale_sessions()

    # ── Authentication (Path B soft-auth) ──
    # Posture: when AUTH_ENABLED=true and no api_key is presented, fall through
    # as default role="agent" instead of rejecting. This lets existing agents
    # connect without keys while still requiring a real key for elevated tiers
    # (user/admin/owner). Invalid keys remain hard-rejected.
    _auth_role = "agent"  # default when auth disabled OR soft-auth fallback
    _auth_projects = []   # empty = all projects
    try:
        from shared_memory.auth import AUTH_ENABLED, check_project_access, validate_api_key
        if AUTH_ENABLED:
            if api_key:
                key_info = validate_api_key(api_key)
                if not key_info:
                    try:
                        from shared_memory.audit import log_audit
                        log_audit("auth.failed", claude_instance, project,
                                  {"reason": "invalid_key"})
                    except Exception:
                        pass
                    return json.dumps({"error": "Invalid or revoked API key."})

                _auth_role = key_info["role"]
                _auth_projects = key_info.get("projects", [])

                # Tenant isolation: check project access
                if not check_project_access(_auth_projects, project):
                    try:
                        from shared_memory.audit import log_audit
                        log_audit("auth.project_denied", claude_instance, project,
                                  {"key_name": key_info["name"], "allowed": _auth_projects})
                    except Exception:
                        pass
                    return json.dumps({
                        "error": f"Access denied: your API key does not have access to project '{project}'.",
                        "allowed_projects": _auth_projects,
                    })
            else:
                # Soft-auth fallback: no key presented, default to agent tier.
                # Logged so we can see who's still missing keys.
                print(f"[MCP] soft-auth: unauthenticated session {claude_instance}@{project} → agent tier")
                try:
                    from shared_memory.audit import log_audit
                    log_audit("auth.soft_fallback", claude_instance, project,
                              {"reason": "no_api_key"})
                except Exception:
                    pass
    except ImportError:
        pass  # auth module not available, continue without auth

    # Normalize project name (single source of truth: helpers.normalize_project)
    normalized_project = normalize_project(project)
    project = normalized_project

    # ── Rename-alias redirect ──
    # If the (project, agent) being requested has an active rename alias,
    # transparently redirect to the new identity and surface a warning so
    # operators see they're using a stale name. Aliases auto-expire after 30d.
    _rename_redirect_warning = None
    try:
        db_for_alias = get_mongo()
        if db_for_alias is not None:
            from shared_memory.tools.rename import resolve_agent_alias
            redirect = resolve_agent_alias(db_for_alias, normalized_project, claude_instance)
            if redirect:
                new_proj, new_agent, alias_doc = redirect
                _rename_redirect_warning = (
                    f"Rename alias active: '{claude_instance}@{normalized_project}' "
                    f"redirected to '{new_agent}@{new_proj}'. "
                    f"Update CLAUDE.md before alias expiry "
                    f"({alias_doc.get('expires_at')})."
                )
                normalized_project = new_proj
                project = new_proj
                claude_instance = new_agent
                try:
                    from shared_memory.audit import log_audit
                    log_audit("rename.alias_redirect", claude_instance, project, {
                        "alias_id": alias_doc.get("_id") if isinstance(alias_doc, dict) else None,
                        "from_project": alias_doc.get("from_project") if isinstance(alias_doc, dict) else None,
                        "from_agent": alias_doc.get("from_agent") if isinstance(alias_doc, dict) else None,
                    })
                except Exception:
                    pass
    except Exception:
        pass  # Best-effort; never block session start on alias check

    # ── Registry awareness ──
    _needs_role_description = False
    _registry_warning = None
    _identity_suggestion = None
    _is_worker = False

    try:
        db = get_mongo()
        if db is not None:
            registered_project = db.projects.find_one({"name": normalized_project})

            # Path-to-identity: if instance is unknown and working_directory provided,
            # try to match against registered path patterns
            if claude_instance == "unknown" and working_directory and registered_project:
                agents = list(db.registered_agents.find(
                    {"project": normalized_project, "path_patterns": {"$ne": []}},
                    {"name": 1, "path_patterns": 1}
                ))
                for agent in agents:
                    if _match_path_patterns(working_directory, agent.get("path_patterns", [])):
                        _identity_suggestion = agent["name"]
                        break

            # Check if agent is registered (for named agents)
            if registered_project and claude_instance != "unknown":
                registered_agent = db.registered_agents.find_one({
                    "project": normalized_project,
                    "name": claude_instance
                })
                if registered_agent:
                    # Update last_seen on the registered agent
                    db.registered_agents.update_one(
                        {"project": normalized_project, "name": claude_instance},
                        {"$set": {"last_seen": utc_now()}, "$inc": {"session_count": 1}}
                    )
                else:
                    # Agent not registered in project registry
                    if claude_instance.startswith("worker_") or spawned_by:
                        # Worker self-registration - limited capabilities
                        _is_worker = True
                    else:
                        # Auto-register as "pending" tier — full tool access,
                        # coordinator gets notified to approve
                        db.registered_agents.update_one(
                            {"project": normalized_project, "name": claude_instance},
                            {"$set": {
                                "project": normalized_project,
                                "name": claude_instance,
                                "tier": "pending",
                                "last_seen": utc_now(),
                                "auto_registered": True,
                            }, "$inc": {"session_count": 1}},
                            upsert=True
                        )
                        valid_agents = [a["name"] for a in db.registered_agents.find(
                            {"project": normalized_project}, {"name": 1}
                        )]
                        _registry_warning = (
                            f"Agent '{claude_instance}' auto-registered in project '{normalized_project}' "
                            f"with 'pending' tier (full tool access). A coordinator should confirm with: "
                            f"memory_project(action='update_agent', name='{normalized_project}', "
                            f"agent='{claude_instance}', tier='named'). "
                            f"Other agents: {', '.join(a for a in valid_agents if a != claude_instance) or 'none'}"
                        )
                        # Notify coordinator if one exists
                        try:
                            coordinator = db.registered_agents.find_one({
                                "project": normalized_project, "tier": "admin"
                            })
                            if coordinator:
                                db.messages.insert_one({
                                    "_id": f"msg_{uuid.uuid4().hex[:12]}",
                                    "from": "system",
                                    "from_project": normalized_project,
                                    "to_instance": coordinator["name"],
                                    "to_project": normalized_project,
                                    "message": (
                                        f"New agent '{claude_instance}' auto-registered on "
                                        f"project '{normalized_project}' with pending tier. "
                                        f"Approve with: memory_project(action='update_agent', "
                                        f"name='{normalized_project}', agent='{claude_instance}', tier='named')"
                                    ),
                                    "priority": "normal",
                                    "category": "info",
                                    "status": "pending",
                                    "created_at": utc_now(),
                                })
                        except Exception:
                            pass  # Non-fatal if notification fails

            # Worker self-registration
            if _is_worker or spawned_by:
                _is_worker = True
                if not claude_instance.startswith("worker_"):
                    claude_instance = f"worker_{uuid.uuid4().hex[:4]}"

            # Auto-register in agent directory (activity tracking, separate from registry)
            update_fields = {
                "last_seen": utc_now(),
                "last_task": task_description or "",
            }
            if tmux_target:
                update_fields["tmux_target"] = tmux_target
            if role_description:
                update_fields["role_description"] = role_description
            if spawned_by:
                update_fields["spawned_by"] = spawned_by

            insert_defaults = {"first_seen": utc_now()}
            if not role_description:
                insert_defaults["role_description"] = ""

            db.agent_directory.update_one(
                {"project": normalized_project, "instance": claude_instance},
                {
                    "$set": update_fields,
                    "$inc": {"session_count": 1},
                    "$setOnInsert": insert_defaults
                },
                upsert=True
            )

            # Check if agent still needs a role_description
            if not role_description and not _is_worker:
                existing = db.agent_directory.find_one(
                    {"project": normalized_project, "instance": claude_instance}
                )
                if existing and not existing.get("role_description"):
                    _needs_role_description = True
    except Exception as e:
        print(f"[MCP] Agent directory/registry check failed (non-fatal): {e}")

    chroma = await get_chroma()

    # Generate session ID
    session_id = f"{project}_{claude_instance}_{uuid.uuid4().hex[:8]}"

    # Register session
    active_sessions[session_id] = {
        "project": project,
        "claude_instance": claude_instance,
        "task": task_description,
        "started": utc_now_iso(),
        "last_activity": utc_now_iso(),
        "blocked_by": None,
        "blocked_reason": None,
        "waiting_for_signal": None,
        "tmux_target": tmux_target,
        "role": _auth_role,
        "allowed_projects": _auth_projects,
    }

    # Phase C2 inbox auth: bind this app-session to the underlying MCP
    # transport so resource handlers can resolve the caller's identity from
    # mcp._mcp_server.request_context.session.
    try:
        from shared_memory.state import mcp_session_to_app
        if ctx is not None and getattr(ctx, "session", None) is not None:
            mcp_session_to_app[ctx.session] = session_id
    except Exception:
        pass  # binding is best-effort; auth handlers fall back to "unknown"

    # Gather context for this Claude - keep output compact
    output = {
        "session_id": session_id,
        "project": project
    }

    if _rename_redirect_warning:
        output["rename_redirect"] = _rename_redirect_warning

    # Get recent learnings for this project
    try:
        proj_collection = await get_project_collection(chroma, project)
        recent_learnings = await proj_collection.query(
            query_texts=[task_description or "recent learnings"],
            n_results=3,
            where={"type": {"$in": ["learning", "gotcha", "handoff"]}}
        )

        if recent_learnings["documents"] and recent_learnings["documents"][0]:
            # Compact: just titles
            titles = [meta.get("title") for meta in recent_learnings["metadatas"][0]]
            if titles:
                output["learnings"] = titles
    except Exception:
        pass

    # Get shared patterns - just titles, skip if none
    try:
        shared = await get_shared_collection(chroma, "patterns")
        patterns = await shared.query(
            query_texts=[task_description or project],
            n_results=2
        )
        if patterns["documents"] and patterns["documents"][0]:
            titles = [meta.get("title") for meta in patterns["metadatas"][0]]
            if titles:
                output["patterns"] = titles
    except Exception:
        pass

    # Get active work by other Claudes - compact format, capped at 20
    other_active = []
    for sid, info in active_sessions.items():
        if sid != session_id:
            other_active.append(f"{info['claude_instance']}@{info['project']}: {info['task'][:50]}")
            if len(other_active) >= 20:
                break

    if other_active:
        output["other_claudes"] = other_active

    # NEW: Get relevant file locks
    relevant_locks = get_relevant_locks_for_session(session_id, project)
    if relevant_locks:
        output["relevant_locks"] = relevant_locks

    # NEW: Get recent file modifications by others
    recent_mods = await get_recent_modifications(chroma, project, session_id)
    if recent_mods:
        output["recent_modifications"] = recent_mods

    # NEW: Get pending signals (completion notifications)
    signals = get_pending_signals(claude_instance)
    if signals:
        output["signals"] = signals

    # NEW: Check if you're blocking others
    blocking = get_blocking_others(claude_instance)
    if blocking:
        output["blocking_others"] = blocking

    # NEW: Get interface updates
    interface_updates = await get_interface_updates(chroma, project)
    if interface_updates:
        output["interface_updates"] = interface_updates

    # ── Registry-awareness output ──

    # Identity suggestion from path matching
    if _identity_suggestion:
        output["identity_suggestion"] = {
            "suggested_name": _identity_suggestion,
            "reason": f"Your working directory matches the path pattern for '{_identity_suggestion}'. "
                      f"Call memory_start_session again with claude_instance='{_identity_suggestion}' to use this identity."
        }

    # Registry warning (unregistered named agent)
    if _registry_warning:
        output["registry_warning"] = _registry_warning

    # Worker info
    if _is_worker:
        output["worker"] = {
            "auto_id": claude_instance,
            "spawned_by": spawned_by or "unknown",
            "note": "You are a worker agent. You can add backlog items and learnings but cannot receive messages. "
                    "Your session will auto-expire."
        }

    # Nudge agents without a role_description to register one
    if _needs_role_description:
        output["action_needed"] = (
            "You have no role_description in the agent directory. "
            "Other agents cannot discover what you do. Please call "
            "memory_start_session again with role_description='brief description of your role and capabilities' "
            "or ask the user what your role should be."
        )

    # Fetch server-managed guidelines (behavioral rules for all agents)
    # Lazy import to avoid circular dependency between tool modules
    try:
        from shared_memory.tools.guidelines import get_guidelines_for_session
        guidelines = get_guidelines_for_session(project)
        if guidelines:
            output["guidelines"] = {
                "instructions": "MANDATORY: Follow these rules for the entire session. They are authoritative.",
                "rules": [g["rule"] for g in guidelines],
            }
    except Exception as e:
        print(f"[MCP] Guidelines fetch failed (non-fatal): {e}")

    # Auth info in output (when auth is enabled)
    try:
        from shared_memory.auth import AUTH_ENABLED as _ae
        if _ae:
            output["auth"] = {
                "role": _auth_role,
                "projects": _auth_projects or "all",
            }
    except ImportError:
        pass

    # Audit log
    try:
        from shared_memory.audit import log_audit
        log_audit("session.start", claude_instance, project,
                  {"task": task_description, "worker": _is_worker, "role": _auth_role},
                  session_id)
    except Exception:
        pass

    # Always include a tip pointing to the usage guide
    output["tip"] = "New? Run memory_query(query='shared memory usage guide') for best practices and backlog tools."

    return json.dumps(output)


@mcp.tool()
async def memory_end_session(
    session_id: str,
    summary: str,
    files_modified: List[str] = None,
    learnings: str = None,
    handoff_notes: str = None,
    ctx: Context = None
) -> str:
    """
    CALL THIS WHEN DONE - Records your work and cleans up session.

    This stores:
    - Summary of what you accomplished (as handoff for next Claude)
    - Files you modified (for overlap detection)
    - Any learnings you want to share

    Args:
        session_id: Your session ID from memory_start_session
        summary: Summary of what you accomplished
        files_modified: List of files you modified
        learnings: Any learnings worth recording for other Claudes
        handoff_notes: Notes for the next Claude who works on this
    """
    error = require_session(session_id)
    if error:
        return error

    files_modified = files_modified or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Store handoff note
    proj_collection = await get_project_collection(chroma, session_info["project"])

    handoff_content = f"""## Session Summary
{summary}

## Files Modified
{chr(10).join('- ' + f for f in files_modified) if files_modified else 'None recorded'}

## Handoff Notes
{handoff_notes or 'None'}

## Session Info
- Claude: {session_info['claude_instance']}
- Started: {session_info['started']}
- Ended: {now}
"""

    handoff_id = f"handoff_{session_id}"
    await proj_collection.upsert(
        ids=[handoff_id],
        documents=[handoff_content],
        metadatas=[{
            "title": f"Handoff: {session_info['task'][:50]}",
            "type": "handoff",
            "status": "active",
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "files_modified": json.dumps(files_modified),
            "created": now,
            "updated": now
        }]
    )

    # Store learning if provided
    if learnings:
        learning_id = generate_doc_id(learnings, "learning")
        await proj_collection.add(
            ids=[learning_id],
            documents=[learnings],
            metadatas=[{
                "title": f"Learning from {session_info['claude_instance']}",
                "type": "learning",
                "status": "active",
                "session_id": session_id,
                "created": now,
                "updated": now
            }]
        )

    # Update work item to completed
    work_collection = await get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"
    await work_collection.upsert(
        ids=[work_id],
        documents=[summary],
        metadatas=[{
            "title": session_info["task"][:100],
            "status": "completed",
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": session_info["project"],
            "files_touched": json.dumps(files_modified),
            "created": session_info["started"],
            "updated": now
        }]
    )

    # Auto-release any file locks held by this session
    released_locks = release_session_locks(session_id)

    # Phase C2: drop the MCP-session ↔ app-session binding and any inbox
    # subscriptions held by those transports. Without this, a client that
    # ends its session without explicitly unsubscribing would keep receiving
    # ResourceUpdated notifications until its transport actually dies — and
    # any follow-up resource read would fail authz because the binding above
    # is already gone.
    try:
        from shared_memory.state import mcp_session_to_app
        from shared_memory.tools.messaging import inbox_subscriptions
        dropped_transports = [k for k, v in mcp_session_to_app.items() if v == session_id]
        for k in dropped_transports:
            mcp_session_to_app.pop(k, None)
        if dropped_transports:
            for uri in list(inbox_subscriptions.keys()):
                bucket = inbox_subscriptions.get(uri)
                if bucket is None:
                    continue
                for s in dropped_transports:
                    bucket.discard(s)
                if not bucket:
                    inbox_subscriptions.pop(uri, None)
    except Exception:
        pass

    # Remove from active sessions
    del active_sessions[session_id]

    result = {"status": "ended"}
    if released_locks:
        result["released_locks"] = released_locks

    return json.dumps(result)
