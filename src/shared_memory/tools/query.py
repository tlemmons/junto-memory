"""Query and retrieval tools - search knowledge base, get documents."""

import json
from datetime import timedelta
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma
from shared_memory.config import OVERLAP_WINDOW_HOURS, PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import (
    cleanup_stale_signals,
    format_age,
    format_staleness_warning,
    format_status_warning,
    get_project_collection,
    get_shared_collection,
    is_expired,
    normalize_project,
    parse_timestamp,
    require_session,
    update_access_stats,
    utc_now,
    utc_now_iso,
)
from shared_memory.state import active_sessions, active_signals

# Relevance threshold - results below this are excluded
# Chroma L2 distance: 0 = identical, 1 = quite different, 2+ = very different
# We convert to similarity: 1 - (dist/2) gives 0-1 range
MIN_RELEVANCE_THRESHOLD = 0.3  # 30% minimum relevance


def calculate_relevance(distance: float) -> float:
    """Convert Chroma L2 distance to 0-1 relevance score.

    L2 distances typically range 0-2 for normalized embeddings.
    We clamp and convert to similarity percentage.
    """
    # Clamp distance to reasonable range
    dist = max(0, min(distance, 2.0))
    # Convert to similarity (0-1 range)
    return 1 - (dist / 2)


@mcp.tool()
async def memory_query(
    session_id: str,
    query: str,
    project: str = None,
    memory_types: List[str] = None,
    include_inactive: bool = False,
    include_shared: bool = True,
    limit: int = 3,
    ctx: Context = None
) -> str:
    """
    Search the knowledge base for relevant information.

    Use this BEFORE implementing something to check:
    - Has this been done before?
    - Are there known patterns or gotchas?
    - What decisions were made about this area?

    Args:
        session_id: Your session ID
        query: Natural language query
        project: Project to search (omit to search shared memories only)
        memory_types: Filter by types (api_spec, architecture, learning, pattern, etc.)
        include_inactive: Include deprecated/superseded/archived documents
        include_shared: Search shared patterns/context (default True, set False for project-only)
        limit: Maximum number of results (1-10, default 3)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    active_sessions[session_id]["last_activity"] = utc_now_iso()

    if project:
        project = normalize_project(project)

    results = []

    # Build where filter
    where_filter = {}
    if not include_inactive:
        where_filter["status"] = "active"
    if memory_types:
        where_filter["type"] = {"$in": memory_types}

    where_clause = where_filter if where_filter else None

    # Search project collection if specified
    if project:
        try:
            proj_collection = await get_project_collection(chroma, project)
            proj_results = await proj_collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_clause
            )

            if proj_results["documents"] and proj_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    proj_results["documents"][0],
                    proj_results["metadatas"][0],
                    proj_results["distances"][0]
                )):
                    # Skip expired documents
                    if is_expired(meta):
                        continue

                    # Calculate relevance and skip if below threshold
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    status = meta.get("status", "active")
                    doc_id = proj_results["ids"][0][i] if proj_results["ids"] else None

                    staleness = format_staleness_warning(meta)
                    warning = format_status_warning(status, meta.get("superseded_by"))
                    if staleness:
                        warning = (warning + " " + staleness).strip() if warning else staleness

                    results.append({
                        "source": f"project:{project}",
                        "id": doc_id or meta.get("id", "unknown"),
                        "title": meta.get("title", "Untitled"),
                        "type": meta.get("type"),
                        "status": status,
                        "relevance": f"{relevance:.0%}",
                        "created": meta.get("created", ""),
                        "updated": meta.get("updated", ""),
                        "age": format_age(meta.get("updated") or meta.get("created")),
                        "content": doc,
                        "access_count": meta.get("access_count", 0),
                        "warning": warning if warning else None
                    })

                    # Track access (fire-and-forget)
                    if doc_id:
                        await update_access_stats(proj_collection, doc_id)
        except Exception:
            pass

    # Search shared collections only if requested and with higher threshold
    if include_shared:
        # Shared results need higher relevance to be included (reduces noise)
        shared_threshold = MIN_RELEVANCE_THRESHOLD + 0.1  # 40% for shared

        for shared_name in ["patterns", "context"]:
            try:
                shared = await get_shared_collection(chroma, shared_name)
                shared_results = await shared.query(
                    query_texts=[query],
                    n_results=min(2, limit),  # Max 2 from each shared collection
                    where=where_clause
                )

                if shared_results["documents"] and shared_results["documents"][0]:
                    for i, (doc, meta, dist) in enumerate(zip(
                        shared_results["documents"][0],
                        shared_results["metadatas"][0],
                        shared_results["distances"][0]
                    )):
                        # Skip expired documents
                        if is_expired(meta):
                            continue

                        # Calculate relevance and skip if below threshold
                        relevance = calculate_relevance(dist)
                        if relevance < shared_threshold:
                            continue

                        doc_id = shared_results["ids"][0][i] if shared_results["ids"] else None

                        staleness = format_staleness_warning(meta)

                        results.append({
                            "source": f"shared:{shared_name}",
                            "id": doc_id,
                            "title": meta.get("title", "Untitled"),
                            "type": meta.get("type"),
                            "relevance": f"{relevance:.0%}",
                            "created": meta.get("created", ""),
                            "updated": meta.get("updated", ""),
                            "age": format_age(meta.get("updated") or meta.get("created")),
                            "content": doc[:500] + "..." if len(doc) > 500 else doc,
                            "access_count": meta.get("access_count", 0),
                            "warning": staleness if staleness else None
                        })

                        # Track access (fire-and-forget)
                        if doc_id:
                            await update_access_stats(shared, doc_id)
            except Exception:
                pass

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No matching memories found. This might be new territory - consider recording what you learn!"
        }, indent=2)

    # Tiered sort: group by relevance band, then sort by recency within each band.
    # This prevents old docs from outranking fresh ones on the same topic.
    def _sort_key(r):
        # Relevance band: >70% = 0 (best), 50-70% = 1, <50% = 2
        pct = int(r["relevance"].rstrip("%")) / 100
        band = 0 if pct > 0.70 else (1 if pct > 0.50 else 2)
        # Within band, sort by updated timestamp descending (newest first)
        ts = parse_timestamp(r.get("updated") or r.get("created"))
        epoch = ts.timestamp() if ts else 0
        return (band, -epoch)

    results.sort(key=_sort_key)
    results = results[:limit]

    return json.dumps({
        "query": query,
        "result_count": len(results),
        "results": results
    }, indent=2)


@mcp.tool()
async def memory_get_by_id(
    session_id: str,
    doc_id: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Retrieve a document by its exact ID.

    Use this when you have a specific document ID (from memory_store, memory_query, etc.)
    and want to retrieve the full content.

    Args:
        session_id: Your session ID
        doc_id: The document ID (e.g., "34e6c10ceecf9b59" or full ID)
        project: Project to search (omit to search all projects + shared)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()

    if project:
        project = normalize_project(project)

    # Build list of collections to search
    collections_to_search = []

    if project:
        # Search specific project + shared collections
        collections_to_search.append(await get_project_collection(chroma, project))
        collections_to_search.append(await get_shared_collection(chroma, "patterns"))
        collections_to_search.append(await get_shared_collection(chroma, "context"))
        collections_to_search.append(await get_shared_collection(chroma, "work"))
    else:
        # Search all collections
        all_collections = await chroma.list_collections()
        for col in all_collections:
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                collections_to_search.append(col)

    # Search for the document
    for col in collections_to_search:
        try:
            result = await col.get(
                ids=[doc_id],
                include=["metadatas", "documents"]
            )

            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0] if result["documents"] else ""

                # Update access tracking
                meta["access_count"] = meta.get("access_count", 0) + 1
                meta["last_accessed"] = utc_now_iso()
                await col.update(ids=[doc_id], metadatas=[meta])

                return json.dumps({
                    "found": True,
                    "id": doc_id,
                    "collection": col.name,
                    "title": meta.get("title", "Untitled"),
                    "type": meta.get("type", "unknown"),
                    "status": meta.get("status", "active"),
                    "project": meta.get("project", ""),
                    "tags": json.loads(meta.get("tags", "[]")),
                    "created": meta.get("created"),
                    "updated": meta.get("updated"),
                    "content": doc
                }, indent=2)
        except Exception:
            continue

    return json.dumps({
        "found": False,
        "id": doc_id,
        "error": f"Document not found with ID: {doc_id}",
        "hint": "Try memory_query() to search by content, or check the project parameter"
    }, indent=2)


@mcp.tool()
async def memory_get_active_work(
    session_id: str,
    project: str = None,
    instance: str = None,
    since_hours: int = None,
    limit: int = 20,
    ctx: Context = None
) -> str:
    """
    See what other Claudes are currently working on.

    Use this to:
    - Avoid working on the same files
    - Understand what's in progress
    - Coordinate with other Claude instances

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects)
        instance: Filter by specific Claude instance name
        since_hours: Only show work updated within this many hours
        limit: Maximum results to return (default 20)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()

    if project:
        project = normalize_project(project)

    # Get from active sessions (in-memory)
    active_work = []
    since_cutoff = None
    if since_hours:
        since_cutoff = (utc_now() - timedelta(hours=since_hours)).isoformat()

    for sid, info in active_sessions.items():
        if sid != session_id:
            if project and normalize_project(info["project"]) != project:
                continue
            if instance and info["claude_instance"] != instance:
                continue
            if since_cutoff and info.get("last_activity", "") < since_cutoff:
                continue
            active_work.append({
                "session_id": sid,
                "claude_instance": info["claude_instance"],
                "project": info["project"],
                "task": info["task"],
                "started": info["started"],
                "last_activity": info["last_activity"]
            })

    # Apply limit to active sessions
    active_work = active_work[:limit]

    # Also get recent work items from Chroma
    work_collection = await get_shared_collection(chroma, "work")
    cutoff = (utc_now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    where_filter = None
    if project:
        where_filter = {"project": project}

    try:
        recent = await work_collection.get(
            where=where_filter,
            include=["documents", "metadatas"]
        )

        recent_work = []
        if recent["documents"]:
            for doc, meta in zip(recent["documents"], recent["metadatas"]):
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                recent_work.append({
                    "title": meta.get("title"),
                    "status": meta.get("status"),
                    "claude": meta.get("claude_instance"),
                    "project": meta.get("project"),
                    "files": json.loads(meta.get("files_touched", "[]")),
                    "updated": meta.get("updated")
                })
    except Exception:
        recent_work = []

    # NEW: Include blocked agents info
    blocked_agents = []
    for sid, info in active_sessions.items():
        if info.get("blocked_by"):
            blocked_agents.append({
                "agent": info.get("claude_instance"),
                "waiting_for": info.get("blocked_by"),
                "signal": info.get("waiting_for_signal"),
                "reason": info.get("blocked_reason")
            })

    # NEW: Include recent signals
    cleanup_stale_signals()
    recent_signals = list(active_signals.values())[:10]

    return json.dumps({
        "currently_active": active_work[:limit],
        "blocked_agents": blocked_agents[:limit],
        "recent_signals": recent_signals[:10],
        "recent_work_items": recent_work[:limit],
        "overlap_window_hours": OVERLAP_WINDOW_HOURS
    }, indent=2)
