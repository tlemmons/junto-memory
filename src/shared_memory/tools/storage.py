"""Storage tools - store documents and record learnings."""

import json
from typing import Dict, List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import MAX_CONTENT_SIZE, MEMORY_TYPES
from shared_memory.helpers import (
    calculate_expiry,
    check_duplicate,
    check_overlap,
    format_overlap_warning,
    generate_content_hash,
    generate_doc_id,
    get_project_collection,
    get_shared_collection,
    normalize_project,
    require_session,
    utc_now_iso,
)
from shared_memory.op_log import emit_op_log_from_context
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_store(
    session_id: str,
    title: str,
    content: str,
    memory_type: str,
    project: str = None,
    tags: List[str] = None,
    files_related: List[str] = None,
    interface_name: str = None,
    interface_version: str = None,
    interface_owner: str = None,
    interface_schema: Dict = None,
    expires_in_days: int = None,
    force_store: bool = False,
    ctx: Context = None
) -> str:
    """
    Store a new memory in the knowledge base.

    Use this for:
    - API specs, architecture docs (project-specific)
    - Code snippets and solutions (can be shared)
    - Task context and notes
    - Interface contracts (with schema validation)

    For quick learnings, use memory_record_learning instead.

    Args:
        session_id: Your session ID
        title: Title for this memory
        content: Content (markdown supported, max 50KB)
        memory_type: Type of memory (api_spec, architecture, learning, pattern, code_snippet, interface, etc.)
        project: Project this belongs to (omit for shared/cross-project memories)
        tags: Tags for categorization
        files_related: File paths this memory relates to
        interface_name: For interfaces - unique name (e.g., "mqtt:frame-status")
        interface_version: For interfaces - version string (e.g., "1.2")
        interface_owner: For interfaces - owning team/agent (e.g., "frames-team")
        interface_schema: For interfaces - JSON schema dict for validation
        expires_in_days: Custom expiry (default: 90 for learnings, never for architecture)
        force_store: Set True to store even if duplicate detected
    """
    error = require_session(session_id)
    if error:
        return error

    # Auth check
    try:
        from shared_memory.auth import require_auth
        auth_error = require_auth(active_sessions[session_id], "store", project)
        if auth_error:
            return json.dumps({"error": auth_error})
    except ImportError:
        pass

    if memory_type not in MEMORY_TYPES:
        return json.dumps({"error": f"Invalid memory_type. Must be one of: {MEMORY_TYPES}"}, indent=2)

    # Check content size limit
    if len(content.encode('utf-8')) > MAX_CONTENT_SIZE:
        return json.dumps({
            "error": f"Content exceeds maximum size of {MAX_CONTENT_SIZE // 1024}KB",
            "size": f"{len(content.encode('utf-8')) // 1024}KB",
            "suggestion": "Break into smaller documents or summarize"
        })

    tags = tags or []
    files_related = files_related or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    if project:
        project = normalize_project(project)

    # Determine collection
    if project:
        collection = await get_project_collection(chroma, project)
    else:
        if memory_type in ["pattern", "code_snippet", "solution", "interface"]:
            collection = await get_shared_collection(chroma, "patterns")
        else:
            collection = await get_shared_collection(chroma, "context")

    # Check for duplicates (unless force_store or interface update)
    duplicate_warning = None
    if not force_store and not (memory_type == "interface" and interface_name):
        duplicate = await check_duplicate(collection, content)
        if duplicate:
            if duplicate["type"] == "exact":
                return json.dumps({
                    "error": "Exact duplicate already exists",
                    "existing_doc_id": duplicate["doc_id"],
                    "existing_title": duplicate["title"],
                    "suggestion": "Use force_store=True to store anyway, or update the existing doc"
                })
            else:
                # Near-duplicate - warn but allow
                duplicate_warning = f"Similar doc exists: '{duplicate['title']}' ({duplicate['similarity']} similar)"

    # For interfaces with a name, use that as the doc_id for easy updates
    if memory_type == "interface" and interface_name:
        doc_id = f"interface_{interface_name.replace(':', '_').replace('/', '_')}"
    else:
        doc_id = generate_doc_id(content, memory_type)

    # Calculate expiry date
    expires_at = calculate_expiry(memory_type, expires_in_days)

    # Generate content hash for future duplicate detection
    content_hash = generate_content_hash(content)

    # Check for overlaps if files are specified
    overlap_warning = ""
    if files_related:
        overlaps = await check_overlap(chroma, project or "shared", files_related, session_id)
        overlap_warning = format_overlap_warning(overlaps, session_info.get("claude_instance"))

    # Build metadata
    metadata = {
        "title": title,
        "type": memory_type,
        "status": "active",
        "tags": json.dumps(tags),
        "files_related": json.dumps(files_related),
        "session_id": session_id,
        "claude_instance": session_info["claude_instance"],
        "project": project or "",
        "created": now,
        "updated": now,
        "content_hash": content_hash,
        "access_count": 0,
        "last_accessed": now
    }

    # Add expiry if applicable
    if expires_at:
        metadata["expires_at"] = expires_at

    # Add interface-specific fields
    if memory_type == "interface":
        if interface_name:
            metadata["interface_name"] = interface_name
        if interface_version:
            metadata["interface_version"] = interface_version
        if interface_owner:
            metadata["interface_owner"] = interface_owner
        if interface_schema:
            metadata["interface_schema"] = json.dumps(interface_schema)

    # Use upsert for interfaces (allows updates)
    if memory_type == "interface" and interface_name:
        await collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata]
        )
    else:
        await collection.add(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata]
        )

    result = {"status": "stored", "id": doc_id[:12]}
    if memory_type == "interface" and interface_name:
        result["interface_name"] = interface_name
        result["interface_version"] = interface_version
    if expires_at:
        result["expires_at"] = expires_at
    if overlap_warning:
        result["overlap_warning"] = overlap_warning
    if duplicate_warning:
        result["duplicate_warning"] = duplicate_warning
    return json.dumps(result)


@mcp.tool()
async def memory_record_learning(
    session_id: str,
    title: str,
    details: str,
    project: str = None,
    tags: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Quick way to record something you learned.

    Use this when you discover:
    - A non-obvious behavior
    - A gotcha or pitfall
    - A useful technique
    - Why something was done a certain way

    These help other Claudes avoid repeating your discovery process.

    Args:
        session_id: Your session ID
        title: What did you learn? (short title)
        details: Details of the learning
        project: Project-specific or omit for cross-project learning
        tags: Tags for categorization
    """
    error = require_session(session_id)
    if error:
        return error

    # Auth check
    try:
        from shared_memory.auth import require_auth
        auth_error = require_auth(active_sessions[session_id], "store", project)
        if auth_error:
            return json.dumps({"error": auth_error})
    except ImportError:
        pass

    tags = tags or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    if project:
        project = normalize_project(project)
        collection = await get_project_collection(chroma, project)
        chroma_collection_name = f"proj_{project}"
    else:
        collection = await get_shared_collection(chroma, "patterns")
        chroma_collection_name = "shared_patterns"

    doc_id = f"learning_{generate_doc_id(title, 'learning')}"

    await collection.add(
        ids=[doc_id],
        documents=[f"# {title}\n\n{details}"],
        metadatas=[{
            "title": title,
            "type": "learning",
            "status": "active",
            "tags": json.dumps(tags),
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "created": now,
            "updated": now
        }]
    )

    # Phase 1 #2 canary: emit op-log entry per §4.3.a (best-effort).
    # Chroma write already landed; emit_op_log_from_context logs + swallows
    # any Mongo-side failure so the tool response is unaffected. Reconciliation
    # (§4.7) backfills gaps on next startup.
    emit_op_log_from_context(
        db=get_mongo(),
        op_type="learning.recorded",
        actor={
            "agent": session_info["claude_instance"],
            "project": project,
            "session_id": session_id,
        },
        ref={"collection": chroma_collection_name, "doc_id": doc_id},
        payload={
            "title": title,
            "details": details,
            "tags": tags,
            "created": now,
        },
    )

    return json.dumps({"status": "recorded", "id": doc_id[:12]})
