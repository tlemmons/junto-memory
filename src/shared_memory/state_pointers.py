"""State-spec pointer check — compute which live pointers moved since the write.

interface:state-pointer-check-v0. Borrowed from orbit-spec (las-sal/orbit-spec):
position is COMPUTED from the record, never stated. A state spec is a stated
position; its Next Steps cite backlog items, messages, reminders and learnings
that keep changing after the park. memory_get_spec reports the ones that did,
so `go` doesn't resume from a step that's already done or answered.

ADVISORY ONLY and owner-only (gated by the caller in tools/specs.py). Never
raises into get_spec: check_pointers returns {"error": ...} on failure.
"""

import re
from typing import Dict, List, Optional, Tuple

from shared_memory.helpers import (
    get_project_collection,
    get_shared_collection,
    parse_timestamp,
    utc_now_iso,
)

# Sections whose ids are claims about what's still open. Status / Decisions /
# history legitimately cite closed items, so they're never checked.
LIVE_HEADING = re.compile(r"current task|next step|blocker|open|waiting|pending", re.IGNORECASE)
# Key Context carries "pull learning_X before Y" pointers: check learnings only.
KEY_CONTEXT_HEADING = re.compile(r"key context", re.IGNORECASE)

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
# msg_sched_ must win over msg_ (same prefix), so it comes first.
_ID = re.compile(
    r"\b(msg_sched_[0-9a-f]{12}|backlog_[0-9a-f]{12}|msg_[0-9a-f]{12}|learning_[0-9a-f]{16})\b"
)
MAX_IDS = 40
CLOSED_BACKLOG = {"done", "wont_do", "duplicate"}


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
    state_guard.standing_fragments. Under Key Context only learning ids count.
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    level = None
    section = ""
    learnings_only = False
    for raw in content.splitlines():
        m = _HEADING.match(raw)
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


async def _chroma_meta(chroma, collections: List[str], project: str, doc_id: str) -> Optional[Dict]:
    """Metadata of doc_id in the first collection that has it, else None."""
    for name in collections:
        coll = (await get_project_collection(chroma, project)) if name == "project" \
            else (await get_shared_collection(chroma, name))
        res = await coll.get(ids=[doc_id], include=["metadatas"])
        if res["ids"]:
            return res["metadatas"][0] or {}
    return None


async def _check_one(db, chroma, project: str, doc_id: str, spec_updated: str) -> Optional[str]:
    """What changed for one pointer, or None when it's unchanged."""
    kind = kind_of(doc_id)
    if kind == "reminder":
        doc = db.scheduled_messages.find_one({"_id": doc_id}, {"status": 1})
        if not doc:
            return "not found"
        status = doc.get("status")
        return None if status == "pending" else status
    if kind == "message":
        # Messages age out of the live collection (differential TTL), so the
        # durable op_log is the main record. Not every send path op-logs
        # (fleet replay 11-14: a pending live message had no op_log row), so
        # existence and replies are the union of both stores.
        live = db.messages.find_one({"_id": doc_id}, {"_id": 1})
        if not live and not db.op_log.find_one({"op_type": "message.sent", "ref.doc_id": doc_id}, {"_id": 1}):
            return "not found"
        reply_ids = {
            d["ref"]["doc_id"] for d in db.op_log.find(
                {"op_type": "message.sent", "payload.in_response_to": doc_id, "ts": {"$gt": spec_updated}},
                {"ref.doc_id": 1},
            )
        }
        since = parse_timestamp(spec_updated)
        if since is not None:
            reply_ids |= {
                d["_id"] for d in db.messages.find(
                    {"in_response_to": doc_id, "created_at": {"$gt": since.replace(tzinfo=None)}}, {"_id": 1}
                )
            }
        n = len(reply_ids)
        return f"{n} repl{'y' if n == 1 else 'ies'} since spec written" if n else None
    if kind == "backlog":
        meta = await _chroma_meta(chroma, ["project", "work"], project, doc_id)
        if meta is None:
            return "not found"
        status = meta.get("backlog_status", "")
        if status in CLOSED_BACKLOG:
            return f"status {status}"
        if (meta.get("updated") or "") > spec_updated:
            return f"updated since spec written (status {status})"
        return None
    meta = await _chroma_meta(chroma, ["project", "patterns"], project, doc_id)
    if meta is None:
        # A pointer to another project's learning: op_log names its collection.
        row = db.op_log.find_one({"op_type": "learning.recorded", "ref.doc_id": doc_id}, {"ref": 1})
        home = ((row or {}).get("ref") or {}).get("collection", "")
        if home.startswith("proj_"):
            meta = await _chroma_meta(chroma, ["project"], home[len("proj_"):], doc_id)
    if meta is None:
        return "not found"
    status = meta.get("status") or "active"
    return None if status == "active" else f"status {status}"


async def check_pointers(db, chroma, project: str, content: str, spec_updated: str) -> Optional[Dict]:
    """The pointer_check field for an agent_state spec, or None (no live ids)."""
    try:
        pointers = live_pointers(content)
        if not pointers:
            return None
        truncated = len(pointers) > MAX_IDS
        pointers = pointers[:MAX_IDS]
        changed = []
        for doc_id, section in pointers:
            what = await _check_one(db, chroma, project, doc_id, spec_updated or "")
            if what:
                changed.append({"id": doc_id, "kind": kind_of(doc_id), "section": section, "what": what})
        return {
            "as_of": utc_now_iso(),
            "spec_updated": spec_updated,
            "checked": len(pointers),
            "changed": changed,
            "truncated": truncated,
        }
    except Exception as e:  # advisory: never fail get_spec
        return {"error": f"{type(e).__name__}: {e}"}

