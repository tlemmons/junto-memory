"""State-spec pointer check — compute which live pointers moved since the write.

interface:state-pointer-check-v0. Borrowed from orbit-spec (las-sal/orbit-spec):
position is COMPUTED from the record, never stated. A state spec is a stated
position; its Next Steps cite backlog items, messages, reminders and learnings
that keep changing after the park. memory_get_spec reports the ones that did,
so `go` doesn't resume from a step that's already done or answered.

ADVISORY ONLY and owner-only (gated by the caller in tools/specs.py). Never
raises into get_spec: check_pointers returns {"error": ...} on failure.

Cost: Mongo lookups are batched per kind ($in), so a spec costs a handful of
indexed queries regardless of how many ids it cites (op_log indexes in
op_log.ensure_op_log_indexes). Chroma: one get per collection per kind.
"""

import re
from typing import Callable, Dict, List, Optional, Tuple

from shared_memory.helpers import (
    get_project_collection,
    get_shared_collection,
    parse_timestamp,
    utc_now_iso,
)

# Sections whose ids are claims about what's still open. Status / Decisions /
# history legitimately cite closed items, so they're never checked. Word
# boundary at the start so "Reopened (history)" is not a live heading.
LIVE_HEADING = re.compile(r"\b(current task|next step|blocker|open|waiting|pending)", re.IGNORECASE)
# Key Context carries "pull learning_X before Y" pointers: check learnings only.
KEY_CONTEXT_HEADING = re.compile(r"\bkey context", re.IGNORECASE)

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
# msg_sched_ must win over msg_ (same prefix), so it comes first.
_ID = re.compile(
    r"\b(msg_sched_[0-9a-f]{12}|backlog_[0-9a-f]{12}|msg_[0-9a-f]{12}|learning_[0-9a-f]{16})\b"
)
MAX_IDS = 40
CLOSED_BACKLOG = {"done", "wont_do", "duplicate"}
PROJECT_PREFIX = "proj_"


def kind_of(doc_id: str) -> str:
    if doc_id.startswith("msg_sched_"):
        return "reminder"
    if doc_id.startswith("msg_"):
        return "message"
    if doc_id.startswith("backlog_"):
        return "backlog"
    return "learning"


def live_pointers(content: str) -> List[Tuple[str, str]]:
    """(id, section heading) for each distinct id in a live section, first wins.

    A section runs until the next heading of the same or higher level, like
    state_guard.standing_fragments. `#` lines inside code fences are not
    headings (a `# restart` comment in a bash block must not end Next Steps).
    Under Key Context only learning ids count.
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    level = None
    section = ""
    learnings_only = False
    in_fence = False
    for raw in content.splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(raw)
        if m:
            depth, title = len(m.group(1)), m.group(2).strip()
            if level is not None and depth <= level:
                level = None
            if level is None:
                if LIVE_HEADING.search(title):
                    level, section, learnings_only = depth, title, False
                elif KEY_CONTEXT_HEADING.search(title):
                    level, section, learnings_only = depth, title, True
            continue
        if level is None:
            continue
        for doc_id in _ID.findall(raw):
            if doc_id in seen or (learnings_only and not doc_id.startswith("learning_")):
                continue
            seen.add(doc_id)
            out.append((doc_id, section))
    return out


async def _chroma_metas(chroma, collection: str, ids: List[str]) -> Dict[str, Dict]:
    """{id: metadata} for the ids present in one collection (proj_<p> or shared kind)."""
    if not ids:
        return {}
    if collection.startswith(PROJECT_PREFIX):
        coll = await get_project_collection(chroma, collection[len(PROJECT_PREFIX):])
    else:
        coll = await get_shared_collection(chroma, collection)
    res = await coll.get(ids=list(ids), include=["metadatas"])
    return {i: (m or {}) for i, m in zip(res["ids"], res["metadatas"])}


async def _resolve_chroma(db, chroma, ids: List[str], local: List[str], op_type: str,
                          allowed: Callable[[str], bool]) -> Dict[str, Dict]:
    """Find ids in the local collections, then in each one's op_log home collection.

    The home lookup covers pointers into another project ("waiting on nimbus
    backlog_X"). It is skipped for collections the caller's key can't see.
    """
    found: Dict[str, Dict] = {}
    for coll in local:
        found.update(await _chroma_metas(chroma, coll, [i for i in ids if i not in found]))
    missing = [i for i in ids if i not in found]
    if missing:
        homes: Dict[str, List[str]] = {}
        for row in db.op_log.find({"op_type": op_type, "ref.doc_id": {"$in": missing}}, {"ref": 1}):
            ref = row.get("ref") or {}
            home = ref.get("collection", "")
            if home and home not in local and allowed(home):
                homes.setdefault(home, []).append(ref.get("doc_id"))
        for home, home_ids in homes.items():
            found.update(await _chroma_metas(chroma, home, home_ids))
    return found


def _participant(doc: Dict, owner: str) -> bool:
    return owner in (doc.get("from_instance"), doc.get("to_instance")) or doc.get("to_instance") == "*"


def _check_messages(db, ids: List[str], since, since_iso: str, owner: str) -> Dict[str, str]:
    """{id: what} for message pointers. Replies are counted only on messages the
    owner sent or received (reply counts on others' threads are not theirs)."""
    out: Dict[str, str] = {}
    # Messages age out of the live collection (differential TTL); op_log
    # payloads carry the full message, but not every send path op-logs
    # (fleet replay 11-14), so the record is the union of both stores.
    docs: Dict[str, Dict] = {d["_id"]: d for d in db.messages.find(
        {"_id": {"$in": ids}}, {"from_instance": 1, "to_instance": 1})}
    for row in db.op_log.find({"op_type": "message.sent", "ref.doc_id": {"$in": ids}},
                              {"ref.doc_id": 1, "payload.from_instance": 1, "payload.to_instance": 1}):
        docs.setdefault(row["ref"]["doc_id"], row.get("payload") or {})
    mine = [i for i in ids if i in docs and _participant(docs[i], owner)]
    for i in ids:
        if i not in docs:
            out[i] = "no record (bad id, or aged out before history was kept)"
    if not mine or since is None:
        return out
    replies: Dict[str, set] = {}
    for row in db.op_log.find(
        {"op_type": "message.sent", "payload.in_response_to": {"$in": mine}, "ts": {"$gt": since_iso}},
        {"ref.doc_id": 1, "payload.in_response_to": 1},
    ):
        replies.setdefault(row["payload"]["in_response_to"], set()).add(row["ref"]["doc_id"])
    for d in db.messages.find(
        {"in_response_to": {"$in": mine}, "created_at": {"$gt": since.replace(tzinfo=None)}},
        {"in_response_to": 1},
    ):
        replies.setdefault(d["in_response_to"], set()).add(d["_id"])
    for i, r in replies.items():
        out[i] = f"{len(r)} repl{'y' if len(r) == 1 else 'ies'} since spec written"
    return out


async def check_pointers(db, chroma, project: str, content: str, spec_updated: str,
                         owner: str = "", allowed: Optional[Callable[[str], bool]] = None) -> Optional[Dict]:
    """The pointer_check field for an agent_state spec, or None (no live ids).

    `allowed(collection_name)` gates cross-project lookups (default: deny).
    With no parseable spec_updated, the time-based checks (backlog touched,
    replies) are skipped rather than flagging everything.
    """
    try:
        pointers = live_pointers(content)
        if not pointers:
            return None
        truncated = len(pointers) > MAX_IDS
        pointers = pointers[:MAX_IDS]
        allowed = allowed or (lambda _c: False)
        since = parse_timestamp(spec_updated)
        by_kind: Dict[str, List[str]] = {}
        for doc_id, _ in pointers:
            by_kind.setdefault(kind_of(doc_id), []).append(doc_id)
        local_proj = [PROJECT_PREFIX + project] if project else []
        what: Dict[str, str] = {}

        ids = by_kind.get("reminder", [])
        if ids:
            status = {d["_id"]: d.get("status") for d in db.scheduled_messages.find(
                {"_id": {"$in": ids}}, {"status": 1})}
            for i in ids:
                if i not in status:
                    what[i] = "not found"
                elif status[i] != "pending":
                    what[i] = status[i]

        ids = by_kind.get("message", [])
        if ids:
            what.update(_check_messages(db, ids, since, spec_updated, owner))

        ids = by_kind.get("backlog", [])
        if ids:
            metas = await _resolve_chroma(db, chroma, ids, local_proj + ["work"], "backlog.added", allowed)
            for i in ids:
                meta = metas.get(i)
                if meta is None:
                    what[i] = "not found"
                    continue
                st = meta.get("backlog_status", "")
                # Parse both: legacy rows carry naive ISO strings, so a string
                # compare with an aware "+00:00" one can misorder.
                updated = parse_timestamp(meta.get("updated"))
                if st in CLOSED_BACKLOG:
                    what[i] = f"status {st}"
                elif since and updated and updated > since:
                    what[i] = f"updated since spec written (status {st})"

        ids = by_kind.get("learning", [])
        if ids:
            metas = await _resolve_chroma(db, chroma, ids, local_proj + ["patterns"], "learning.recorded", allowed)
            for i in ids:
                meta = metas.get(i)
                st = (meta or {}).get("status") or "active"
                if meta is None:
                    what[i] = "not found"
                elif st != "active":
                    what[i] = f"status {st}"

        changed = [{"id": i, "kind": kind_of(i), "section": s, "what": what[i]}
                   for i, s in pointers if i in what]
        return {
            "as_of": utc_now_iso(),
            "spec_updated": spec_updated,
            "checked": len(pointers),
            "changed": changed,
            "truncated": truncated,
        }
    except Exception as e:  # advisory: never fail get_spec
        return {"error": f"{type(e).__name__}: {e}"}
