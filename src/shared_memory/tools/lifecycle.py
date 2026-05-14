"""Document lifecycle tools - update work status, archive/restore documents."""

import json
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma
from shared_memory.config import DOC_STATUSES, WORK_STATUSES
from shared_memory.helpers import (
    get_project_collection,
    get_shared_collection,
    normalize_project,
    require_session,
    utc_now_iso,
)
from shared_memory.state import active_sessions, active_signals


@mcp.tool()
async def memory_update_work(
    session_id: str,
    title: str,
    status: str,
    files_touched: List[str] = None,
    notes: str = None,
    blocked_by: str = None,
    blocked_reason: str = None,
    waiting_for_signal: str = None,
    signals: List[str] = None,
    signal_details: str = None,
    ctx: Context = None
) -> str:
    """
    Update your current work status and files touched.

    Call this periodically to:
    - Let other Claudes know what you're working on
    - Enable overlap detection (warns if another Claude touches same files)
    - Track progress on your task
    - Signal dependencies (blocked_by) or completion (signals)

    Args:
        session_id: Your session ID
        title: What you're working on
        status: Current status (in_progress, blocked, completed, abandoned)
        files_touched: Files you've touched (for overlap detection)
        notes: Additional context
        blocked_by: Agent ID you're waiting for (e.g., "frames-team")
        blocked_reason: What you need from them
        waiting_for_signal: Signal name you're waiting for (e.g., "status-schema-ready")
        signals: Signals to broadcast on completion (e.g., ["status-schema-ready"])
        signal_details: Additional context for signals
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in WORK_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {WORK_STATUSES}"}, indent=2)

    files_touched = files_touched or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Update session
    active_sessions[session_id]["last_activity"] = now
    active_sessions[session_id]["task"] = title

    # Update blocker info in session
    if blocked_by:
        active_sessions[session_id]["blocked_by"] = blocked_by
        active_sessions[session_id]["blocked_reason"] = blocked_reason
        active_sessions[session_id]["waiting_for_signal"] = waiting_for_signal
    elif status != "blocked":
        # Clear blocker info if not blocked
        active_sessions[session_id]["blocked_by"] = None
        active_sessions[session_id]["blocked_reason"] = None
        active_sessions[session_id]["waiting_for_signal"] = None

    # Broadcast signals if provided (typically on completion)
    signals_broadcast = []
    if signals:
        for signal_name in signals:
            active_signals[signal_name] = {
                "from_session": session_id,
                "from_claude": session_info["claude_instance"],
                "timestamp": now,
                "details": signal_details or ""
            }
            signals_broadcast.append(signal_name)

    # Check for overlaps
    overlap_warning = ""
    if files_touched:
        from shared_memory.helpers import check_overlap, format_overlap_warning
        overlaps = await check_overlap(chroma, session_info["project"], files_touched, session_id)
        overlap_warning = format_overlap_warning(overlaps, session_info.get("claude_instance"))

    # Store/update work item
    work_collection = await get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"

    content = f"{title}\n\n{notes or ''}"

    await work_collection.upsert(
        ids=[work_id],
        documents=[content],
        metadatas=[{
            "title": title,
            "status": status,
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": session_info["project"],
            "files_touched": json.dumps(files_touched),
            "blocked_by": blocked_by or "",
            "blocked_reason": blocked_reason or "",
            "waiting_for_signal": waiting_for_signal or "",
            "created": session_info["started"],
            "updated": now
        }]
    )

    result = {"status": "updated", "work": status}
    if overlap_warning:
        result["warning"] = overlap_warning
    if signals_broadcast:
        result["signals_broadcast"] = signals_broadcast
    return json.dumps(result)


# =============================================================================
# Lifecycle Management Tools
# =============================================================================

@mcp.tool()
async def memory_change_status(
    session_id: str,
    doc_id: str,
    new_status: str,
    project: str = None,
    superseded_by: str = None,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Change a document's lifecycle status.

    Use this to:
    - Mark outdated docs as DEPRECATED (still searchable with warning)
    - Mark replaced docs as SUPERSEDED (link to replacement)
    - Archive old docs (excluded from normal search)

    Prefer this over deletion - it preserves history and context.

    Args:
        session_id: Your session ID
        doc_id: Document ID to update
        new_status: New status (active, deprecated, superseded, archived)
        project: Project (if project-specific doc)
        superseded_by: ID of replacement document (if superseding)
        reason: Reason for status change
    """
    error = require_session(session_id)
    if error:
        return error

    if new_status not in DOC_STATUSES:
        return json.dumps({"error": f"Invalid new_status. Must be one of: {DOC_STATUSES}"}, indent=2)

    chroma = await get_chroma()
    now = utc_now_iso()

    if project:
        project = normalize_project(project)

    # Find the document
    if project:
        collection = await get_project_collection(chroma, project)
    else:
        collection = None
        for shared_name in ["patterns", "context"]:
            shared = await get_shared_collection(chroma, shared_name)
            result = await shared.get(ids=[doc_id])
            if result["ids"]:
                collection = shared
                break

        if not collection:
            return json.dumps({"error": f"Document not found: {doc_id}"}, indent=2)

    result = await collection.get(ids=[doc_id], include=["documents", "metadatas"])

    if not result["ids"]:
        return json.dumps({"error": f"Document not found: {doc_id}"}, indent=2)

    # Update metadata
    meta = result["metadatas"][0]
    old_status = meta.get("status", "active")
    meta["status"] = new_status
    meta["updated"] = now
    meta["status_changed_by"] = session_id

    if superseded_by:
        meta["superseded_by"] = superseded_by
    if reason:
        meta["status_change_reason"] = reason

    await collection.update(
        ids=[doc_id],
        metadatas=[meta]
    )

    return json.dumps({
        "status": "updated",
        "doc_id": doc_id,
        "old_status": old_status,
        "new_status": new_status,
        "superseded_by": superseded_by,
        "message": f"Document marked as {new_status}. " +
                   ("It will show warnings when retrieved." if new_status != "archived"
                    else "It is now excluded from normal searches.")
    }, indent=2)


@mcp.tool()
async def memory_archive_by_tag(
    session_id: str,
    tag: str,
    reason: str = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Bulk archive all documents with a specific tag.

    Perfect for:
    - End of version cleanup (tag="v5" when moving to v6)
    - Feature completion (tag="oauth-feature" when shipped)
    - Sprint cleanup (tag="sprint-42")

    Archived docs are excluded from normal searches but can be restored.

    Args:
        session_id: Your session ID
        tag: Tag to match (e.g., "v5", "oauth-feature")
        reason: Why archiving (e.g., "Moving to v6", "Feature shipped")
        project: Limit to specific project (omit for all projects + shared)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    now = utc_now_iso()
    archived_count = 0
    archived_docs = []

    # Import prefix constants
    from shared_memory.config import PROJECT_PREFIX, SHARED_PREFIX

    # Get collections to search
    collections = await chroma.list_collections()
    target_collections = []

    for col in collections:
        if project:
            # Only the specified project
            if col.name == f"{PROJECT_PREFIX}{normalize_project(project)}":
                target_collections.append(col)
        else:
            # All project and shared collections
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                target_collections.append(col)

    for col in target_collections:
        try:
            # Get all docs - we'll filter by tag in Python since Chroma's JSON querying is limited
            results = await col.get(include=["metadatas"])

            for i, meta in enumerate(results["metadatas"]):
                doc_id = results["ids"][i]

                # Check if tag matches (tags stored as JSON string)
                tags = json.loads(meta.get("tags", "[]"))
                if tag not in tags:
                    continue

                # Skip already archived
                if meta.get("status") == "archived":
                    continue

                # Archive it
                meta["status"] = "archived"
                meta["archived_at"] = now
                meta["archived_by"] = session_id
                meta["archive_reason"] = reason or f"Bulk archive by tag: {tag}"
                meta["previous_status"] = meta.get("status", "active")

                await col.update(ids=[doc_id], metadatas=[meta])
                archived_count += 1
                archived_docs.append({
                    "id": doc_id,
                    "title": meta.get("title", "Untitled"),
                    "collection": col.name
                })
        except Exception:
            continue

    return json.dumps({
        "status": "completed",
        "tag": tag,
        "archived_count": archived_count,
        "archived_docs": archived_docs[:20],  # Limit output
        "note": f"Archived {archived_count} docs. Use memory_restore_by_tag to undo."
    }, indent=2)


@mcp.tool()
async def memory_restore_by_tag(
    session_id: str,
    tag: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Bulk restore archived documents with a specific tag.

    Use this to bring back previously archived version/feature docs.

    Args:
        session_id: Your session ID
        tag: Tag to match (e.g., "v5", "oauth-feature")
        project: Limit to specific project (omit for all projects + shared)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    now = utc_now_iso()
    restored_count = 0
    restored_docs = []

    # Import prefix constants
    from shared_memory.config import PROJECT_PREFIX, SHARED_PREFIX

    # Get collections to search
    collections = await chroma.list_collections()
    target_collections = []

    for col in collections:
        if project:
            if col.name == f"{PROJECT_PREFIX}{normalize_project(project)}":
                target_collections.append(col)
        else:
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                target_collections.append(col)

    for col in target_collections:
        try:
            # Get archived docs
            results = await col.get(
                where={"status": "archived"},
                include=["metadatas"]
            )

            for i, meta in enumerate(results["metadatas"]):
                doc_id = results["ids"][i]

                # Check if tag matches
                tags = json.loads(meta.get("tags", "[]"))
                if tag not in tags:
                    continue

                # Restore to previous status or active
                previous_status = meta.get("previous_status", "active")
                meta["status"] = previous_status
                meta["restored_at"] = now
                meta["restored_by"] = session_id

                await col.update(ids=[doc_id], metadatas=[meta])
                restored_count += 1
                restored_docs.append({
                    "id": doc_id,
                    "title": meta.get("title", "Untitled"),
                    "collection": col.name
                })
        except Exception:
            continue

    return json.dumps({
        "status": "completed",
        "tag": tag,
        "restored_count": restored_count,
        "restored_docs": restored_docs[:20],
        "note": f"Restored {restored_count} docs to their previous status."
    }, indent=2)
