"""Backlog ticket comments (interface:backlog-ideas-comments-v0 §2).

Mongo `backlog_comments`, one row per comment:
    {_id: "bcmt_<12hex>", item_id, project, author, author_kind, body, created}

Written by memory_update_backlog_item(comment=...) and by the /backlog page;
read by memory_get_by_id (backlog_* ids) and memory_list_backlog (counts).
Bodies are redacted by the caller before they get here. Not replicated through
the op-log in v0: comments stay on the primary.
"""

import hashlib
import logging
from typing import Dict, List, Optional

from shared_memory.helpers import utc_now_iso

log = logging.getLogger(__name__)

AUTHOR_KINDS = ("human", "agent")
MAX_BODY = 20000
READ_CAP = 100

_indexed = False


def _col(db):
    global _indexed
    col = db.backlog_comments
    if not _indexed:
        try:
            col.create_index([("item_id", 1), ("created", 1)])
            _indexed = True
        except Exception as e:  # noqa: BLE001
            log.warning("backlog_comments: index create failed: %s", e)
    return col


def add_comment(db, item_id: str, project: Optional[str], author: str,
                author_kind: str, body: str) -> Dict:
    """Insert one comment and return the stored row. Raises ValueError on bad input."""
    body = (body or "").strip()
    if not body:
        raise ValueError("comment body is empty")
    if len(body) > MAX_BODY:
        raise ValueError(f"comment body over {MAX_BODY} chars")
    if author_kind not in AUTHOR_KINDS:
        raise ValueError(f"author_kind must be one of {AUTHOR_KINDS}")
    now = utc_now_iso()
    cid = "bcmt_" + hashlib.sha256(f"{item_id}:{author}:{now}:{body[:64]}".encode()).hexdigest()[:12]
    row = {"_id": cid, "item_id": item_id, "project": project or "", "author": author or "unknown",
           "author_kind": author_kind, "body": body, "created": now}
    _col(db).insert_one(row)
    return public(row)


def list_comments(db, item_id: str, limit: int = READ_CAP) -> List[Dict]:
    """Oldest first, at most `limit`."""
    return [public(r) for r in _col(db).find({"item_id": item_id}).sort("created", 1).limit(limit)]


def counts(db, item_ids: List[str]) -> Dict[str, int]:
    """{item_id: n} for the ids that have any comments."""
    if not item_ids:
        return {}
    pipe = [{"$match": {"item_id": {"$in": list(item_ids)}}},
            {"$group": {"_id": "$item_id", "n": {"$sum": 1}}}]
    return {r["_id"]: r["n"] for r in _col(db).aggregate(pipe)}


def public(row: Dict) -> Dict:
    out = {k: v for k, v in row.items() if k != "_id"}
    out["id"] = row["_id"]
    return out
