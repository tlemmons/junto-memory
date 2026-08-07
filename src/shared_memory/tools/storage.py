"""Storage tools - store documents and record learnings."""

import json
import logging
from typing import Dict, List

from mcp.server.fastmcp import Context

from shared_memory import claim_gate
from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import MAX_CONTENT_SIZE, MEMORY_TYPES
from shared_memory.helpers import (
    calculate_expiry,
    calculate_relevance,
    check_duplicate,
    check_overlap,
    format_overlap_warning,
    generate_content_hash,
    generate_doc_id,
    get_project_collection,
    get_shared_collection,
    is_expired,
    normalize_project,
    require_session,
    utc_now_iso,
)
from shared_memory.intent import get_current_context_tokens
from shared_memory.op_log import emit_op_log_from_context, fetch_embedding_for_op_log
from shared_memory.state import active_sessions

logger = logging.getLogger(__name__)


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

    # Determine collection. chroma_collection_name mirrors what
    # get_*_collection produces (PROJECT_PREFIX + project / SHARED_PREFIX +
    # shared bucket) so the op-log `ref.collection` carries the same
    # identifier a §5.1 sync replay or §4.7 reconciliation pass will use.
    if project:
        collection = await get_project_collection(chroma, project)
        chroma_collection_name = f"proj_{project}"
    else:
        if memory_type in ["pattern", "code_snippet", "solution", "interface"]:
            collection = await get_shared_collection(chroma, "patterns")
            chroma_collection_name = "shared_patterns"
        else:
            collection = await get_shared_collection(chroma, "context")
            chroma_collection_name = "shared_context"

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

    # Phase 1 #2 canary 2/13: emit op-log entry per §4.3.a (best-effort).
    # Chroma write already landed; emit_op_log_from_context logs + swallows
    # any Mongo-side failure so the tool response is unaffected. Both the
    # add path and the interface-upsert path emit the same op_type — replay
    # re-derives the right Chroma path from payload.memory_type +
    # payload.interface_name. Reconciliation (§4.7) backfills gaps.
    #
    # Phase 2 A-path: capture the Chroma-computed embedding into payload so
    # peers pin the same vector on replay — avoids cross-server vector-skew
    # (backlog_f0cb1ba24496). Helper is best-effort: None on fetch failure.
    embedding = await fetch_embedding_for_op_log(collection, doc_id)
    emit_op_log_from_context(
        db=get_mongo(),
        op_type="store.created",
        actor={
            "agent": session_info["claude_instance"],
            "project": project,
            "session_id": session_id,
        },
        ref={"collection": chroma_collection_name, "doc_id": doc_id},
        payload={
            "title": title,
            "content": content,
            "memory_type": memory_type,
            "tags": tags,
            "files_related": files_related,
            "interface_name": interface_name,
            "interface_version": interface_version,
            "interface_owner": interface_owner,
            "interface_schema": interface_schema,
            "expires_at": expires_at,
            "content_hash": content_hash,
            "created": now,
            "embedding": embedding,
        },
    )

    result = {"status": "stored", "id": doc_id}
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


# Write-time contradiction gate (threshold stage). When a new learning is
# recorded, surface near-prior learnings so the author can catch a duplicate or
# a contradiction of an existing note before it silently lands (root case:
# 2026-07 mesh-offline — a wrong learning recorded over its contradicting prior,
# no query). THRESHOLD-ONLY v0: embedding similarity only. It surfaces near
# priors of ALL kinds — a true contradiction and a consistent restatement look
# alike to a cosine floor (they can be lexically near-identical; only semantics
# separate them). Telling the two apart needs a claim-extraction + classifier
# layer on top (backlog_6471d8348393); this stage just says "these are close,
# check them." Advisory only — it NEVER blocks the write.
# TODO: promote SIMILAR_LEARNING_THRESHOLD to the three-layer query_config knob
# once calibrated against coordinator@nimbus's expanded dataset.
SIMILAR_LEARNING_THRESHOLD = 0.6  # normalized [0,1] similarity (helpers.calculate_relevance)


async def _find_similar_learnings(
    collection,
    query_text: str,
    exclude_id: str,
    threshold: float = SIMILAR_LEARNING_THRESHOLD,
    k: int = 5,
) -> List[Dict]:
    """Active, non-expired prior learnings whose embedding similarity to
    query_text is >= threshold, as header pointers (id/title/score/updated, no
    bodies) sorted by score desc.

    Single-key Chroma where-filter only: multi-key filters silently return
    nothing in this Chroma version (see the memory_list_specs gotcha, commit
    28570d4). So filter type in the query, status/expiry in Python.
    """
    results = await collection.query(
        query_texts=[query_text],
        n_results=k + 5,  # over-fetch; Python-side filtering drops some
        where={"type": "learning"},
    )
    id_rows = results.get("ids") or []
    if not id_rows or not id_rows[0]:
        return []
    ids = id_rows[0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]

    hits: List[Dict] = []
    for doc_id, meta, dist in zip(ids, metas, dists):
        if doc_id == exclude_id:
            continue
        meta = meta or {}
        if meta.get("status", "active") != "active":
            continue
        if is_expired(meta):
            continue
        score = calculate_relevance(dist)
        if score < threshold:
            continue
        hits.append({
            "id": doc_id,
            "title": meta.get("title", ""),
            "score": round(score, 3),
            "updated": meta.get("updated") or meta.get("created"),
        })

    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:k]


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

    When recording on a topic that already has an entry, UPDATE/SUPERSEDE the
    existing one (memory_change_status) rather than creating a near-duplicate
    — the write-time gate surfaces similar priors in this tool's response;
    disposition them, don't ignore them.
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

    # Write-lint (backlog_1115f9fe35f7): strip serialized tool-call envelope
    # if a malformed emission leaked it into the body. Recovered sibling
    # content is kept in-doc under a marked heading (record_learning has no
    # handoff field to re-route to) and flagged in the response.
    _lint_notes = []
    try:
        from shared_memory.write_lint import strip_envelope_leak
        details, _extracted, _leaked = strip_envelope_leak(details, "details")
        if _leaked:
            _lint_notes.append("envelope leak stripped from details")
            for _pname, _ptext in _extracted.items():
                details += f"\n\n## [write-lint] recovered {_pname}\n{_ptext}"
                _lint_notes.append(f"recovered '{_pname}' block kept in-doc")
    except Exception:
        pass

    if project:
        project = normalize_project(project)
        collection = await get_project_collection(chroma, project)
        chroma_collection_name = f"proj_{project}"
    else:
        collection = await get_shared_collection(chroma, "patterns")
        chroma_collection_name = "shared_patterns"

    doc_id = f"learning_{generate_doc_id(title, 'learning')}"
    document_text = f"# {title}\n\n{details}"

    # Write-time contradiction gate (threshold stage): retrieve near-prior
    # learnings BEFORE the add, so the new doc cannot match itself. Best-effort
    # by contract — an advisory-lookup failure must NEVER block recording the
    # learning (the write is the load-bearing operation; the surfacing is not).
    similar_prior: List[Dict] = []
    try:
        similar_prior = await _find_similar_learnings(
            collection, document_text, exclude_id=doc_id
        )
    except Exception as e:  # noqa: BLE001 - advisory path, degrade to no-surfacing
        logger.warning("write-time gate similar-learning lookup failed: %s", e)

    metadata = {
        "title": title,
        "type": "learning",
        "status": "active",
        "tags": json.dumps(tags),
        "session_id": session_id,
        "claude_instance": session_info["claude_instance"],
        "created": now,
        "updated": now
    }
    # Session-age axis for the correction-rate study: context depth at write
    # time, injected client-side via the __context_tokens sideband kwarg
    # (inbox plugin hook). Optional — absent for clients without the hook;
    # session_id above is the coarse fallback (joins session_starts).
    context_tokens = get_current_context_tokens()
    if context_tokens is not None:
        metadata["context_tokens"] = context_tokens

    await collection.add(
        ids=[doc_id],
        documents=[document_text],
        metadatas=[metadata]
    )

    # Phase 1 #2 canary: emit op-log entry per §4.3.a (best-effort).
    # Chroma write already landed; emit_op_log_from_context logs + swallows
    # any Mongo-side failure so the tool response is unaffected. Reconciliation
    # (§4.7) backfills gaps on next startup.
    embedding = await fetch_embedding_for_op_log(collection, doc_id)
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
            "embedding": embedding,
            **({"context_tokens": context_tokens} if context_tokens is not None else {}),
        },
    )

    result = {"status": "recorded", "id": doc_id}
    if _lint_notes:
        result["write_lint"] = _lint_notes

    # Dangling-ref advisory (backlog_d03297e01f30): existence-check ID-shaped
    # references in the body, warn in THIS response — the artifact chokepoint.
    # Best-effort; never blocks or rejects the recorded learning.
    try:
        from shared_memory.write_lint import find_unresolved_refs, advisory_payload
        _unresolved = await find_unresolved_refs(
            f"{title}\n{details}", get_mongo(), chroma, project
        )
        if _unresolved:
            result.update(advisory_payload(_unresolved))
    except Exception:
        pass

    if similar_prior:
        # Classifier stage (interface:claim-extraction-v0): annotate each
        # surfaced prior with CONTRADICTS/CONSISTENT/UNRELATED. Advisory and
        # fail-quiet by contract — classify_against_priors never raises; when
        # the gate is disabled or the endpoint is down it hands the priors
        # back untouched and we fall through to the threshold-only wording.
        contradicted: List[str] = []
        similar_prior, contradicted = await claim_gate.classify_against_priors(
            db=get_mongo(),
            collection=collection,
            new_doc_id=doc_id,
            new_title=title,
            new_details=details,
            priors=similar_prior,
        )
        result["similar_prior"] = similar_prior
        if contradicted:
            result["note"] = (
                f"CONTRADICTION: per the claim classifier, this learning "
                f"contradicts {', '.join(contradicted)} — one of them is wrong. "
                "If the prior no longer holds, supersede it: memory_change_status("
                "doc_id=..., new_status='superseded', reason=...). If the prior "
                "is still right, re-check this note before relying on it. "
                "(Advisory — both records exist until you act.)"
            )
        else:
            # Pointers, not conclusions: surface the near priors + why, let the
            # author judge. Threshold similarity alone can't tell contradiction
            # from restatement, so the wording asks the author to check, not
            # asserts a conflict.
            result["note"] = (
                f"{len(similar_prior)} existing learning(s) are similar to this one — "
                "review for duplication or contradiction before relying on both. If this "
                "supersedes one, mark it: memory_change_status(new_status='superseded')."
            )

    # Write-time facets (design:memory-facets-v0): fire-and-forget extraction
    # AFTER the write has landed — the response never waits on the model. When
    # the claim gate ran above, the facet task reuses its cached claim.
    from shared_memory import facets as facets_mod
    if facets_mod.schedule_facet_extraction(
        get_mongo(), collection, doc_id, title, details
    ):
        result["facets"] = "extraction scheduled"
    return json.dumps(result)
