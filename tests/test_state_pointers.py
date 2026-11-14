"""State-spec pointer check (interface:state-pointer-check-v0).

The pointer check computes which live pointers of a state spec moved after it
was written: closed or touched backlog items, delivered reminders, replies to
cited messages, superseded learnings. Idea borrowed from orbit-spec.
"""

import asyncio
from datetime import datetime

from shared_memory import state_pointers as sp

SPEC_UPDATED = "2026-11-13T03:12:26+00:00"

CONTENT = """## Current Task
Waiting on backlog_aaaaaaaaaaaa.

## Status
- backlog_bbbbbbbbbbbb done earlier (history: never checked).

## Next Steps
1. Reply on msg_111111111111 then msg_sched_222222222222.
2. Again backlog_aaaaaaaaaaaa (dedup).
3. msg_333333333333 is gone.

## Key Context
- Pull learning_0123456789abcdef first; backlog_cccccccccccc here is ignored.
"""


class _FakeMongoCol:
    def __init__(self, docs):
        self.docs = docs

    def _match(self, d, q):
        for k, v in q.items():
            cur = d
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(v, dict) and "$gt" in v:
                if cur is None or not cur > v["$gt"]:
                    return False
            elif cur != v:
                return False
        return True

    def find_one(self, q, proj=None):
        return next((d for d in self.docs if self._match(d, q)), None)

    def count_documents(self, q):
        return sum(1 for d in self.docs if self._match(d, q))

    def find(self, q, proj=None):
        return [d for d in self.docs if self._match(d, q)]


class _FakeDB:
    def __init__(self):
        # A live message with no op_log row, plus a live-only reply to msg_111.
        self.messages = _FakeMongoCol([
            {"_id": "msg_444444444444"},
            {"_id": "msg_777777777777", "in_response_to": "msg_111111111111",
             "created_at": datetime(2026, 11, 14, 1, 0, 0)},
            # Same reply as the op_log's msg_999 row: must count once.
            {"_id": "msg_999999999999", "in_response_to": "msg_111111111111",
             "created_at": datetime(2026, 11, 14, 0, 0, 0)},
        ])
        self.scheduled_messages = _FakeMongoCol([{"_id": "msg_sched_222222222222", "status": "delivered"}])
        self.op_log = _FakeMongoCol([
            {"op_type": "message.sent", "ref": {"doc_id": "msg_111111111111"}, "payload": {}, "ts": "2026-11-12T00:00:00+00:00"},
            {"op_type": "message.sent", "ref": {"doc_id": "msg_999999999999"},
             "payload": {"in_response_to": "msg_111111111111"}, "ts": "2026-11-14T00:00:00+00:00"},
            {"op_type": "message.sent", "ref": {"doc_id": "msg_888888888888"},
             "payload": {"in_response_to": "msg_111111111111"}, "ts": "2026-11-12T00:00:00+00:00"},
        ])


class _FakeChromaCol:
    def __init__(self, metas):
        self.metas = metas

    async def get(self, ids=None, include=None):
        hit = [i for i in ids if i in self.metas]
        return {"ids": hit, "metadatas": [self.metas[i] for i in hit]}


def _patch(monkeypatch, project_metas, shared_metas):
    async def _proj(client, project):
        return _FakeChromaCol(project_metas)

    async def _shared(client, kind):
        return _FakeChromaCol(shared_metas)

    monkeypatch.setattr(sp, "get_project_collection", _proj)
    monkeypatch.setattr(sp, "get_shared_collection", _shared)


def test_live_pointers_sections_and_dedup():
    got = sp.live_pointers(CONTENT)
    assert got == [
        ("backlog_aaaaaaaaaaaa", "Current Task"),
        ("msg_111111111111", "Next Steps"),
        ("msg_sched_222222222222", "Next Steps"),
        ("msg_333333333333", "Next Steps"),
        ("learning_0123456789abcdef", "Key Context"),
    ]


def test_msg_sched_not_read_as_msg():
    assert sp.live_pointers("## Next Steps\nmsg_sched_abcdefabcdef\n") == [
        ("msg_sched_abcdefabcdef", "Next Steps")
    ]
    assert sp.kind_of("msg_sched_abcdefabcdef") == "reminder"


def test_nested_heading_ends_section():
    content = "## Next Steps\n- backlog_aaaaaaaaaaaa\n## Decisions Taken\n- backlog_bbbbbbbbbbbb\n"
    assert [i for i, _ in sp.live_pointers(content)] == ["backlog_aaaaaaaaaaaa"]


def test_check_pointers_reports_only_changes(monkeypatch):
    _patch(
        monkeypatch,
        {"backlog_aaaaaaaaaaaa": {"backlog_status": "done", "updated": "2026-11-12T00:00:00+00:00"}},
        {"learning_0123456789abcdef": {"status": "superseded"}},
    )
    out = asyncio.run(sp.check_pointers(_FakeDB(), object(), "junto", CONTENT, SPEC_UPDATED))
    assert out["checked"] == 5 and out["truncated"] is False
    by_id = {c["id"]: c["what"] for c in out["changed"]}
    assert by_id == {
        "backlog_aaaaaaaaaaaa": "status done",
        "msg_111111111111": "2 replies since spec written",  # op_log + live-only; older reply excluded
        "msg_sched_222222222222": "delivered",
        "msg_333333333333": "not found",
        "learning_0123456789abcdef": "status superseded",
    }


def test_unchanged_pointers_not_listed(monkeypatch):
    _patch(
        monkeypatch,
        {"backlog_aaaaaaaaaaaa": {"backlog_status": "open", "updated": "2026-11-01T00:00:00+00:00"}},
        {},
    )
    content = "## Next Steps\n- backlog_aaaaaaaaaaaa\n"
    out = asyncio.run(sp.check_pointers(_FakeDB(), object(), "junto", content, SPEC_UPDATED))
    assert out["checked"] == 1 and out["changed"] == []


def test_backlog_touched_after_spec(monkeypatch):
    _patch(
        monkeypatch,
        {"backlog_aaaaaaaaaaaa": {"backlog_status": "open", "updated": "2026-11-14T00:00:00+00:00"}},
        {},
    )
    out = asyncio.run(sp.check_pointers(
        _FakeDB(), object(), "junto", "## Blockers\nbacklog_aaaaaaaaaaaa\n", SPEC_UPDATED))
    assert out["changed"][0]["what"] == "updated since spec written (status open)"


def test_no_live_ids_returns_none():
    assert asyncio.run(sp.check_pointers(_FakeDB(), object(), "junto", "## Status\nbacklog_aaaaaaaaaaaa\n", "")) is None


def test_errors_never_raise(monkeypatch):
    async def _boom(client, project):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(sp, "get_project_collection", _boom)
    out = asyncio.run(sp.check_pointers(_FakeDB(), object(), "junto", "## Next Steps\nbacklog_aaaaaaaaaaaa\n", ""))
    assert out == {"error": "RuntimeError: chroma down"}


def test_live_message_without_op_log_is_found(monkeypatch):
    _patch(monkeypatch, {}, {})
    out = asyncio.run(sp.check_pointers(
        _FakeDB(), object(), "junto", "## Next Steps\nmsg_444444444444\n", SPEC_UPDATED))
    assert out["changed"] == []


def test_cross_project_learning_resolved_via_op_log(monkeypatch):
    seen = []

    async def _proj(client, project):
        seen.append(project)
        return _FakeChromaCol({"learning_0123456789abcdef": {"status": "active"}} if project == "havruta" else {})

    async def _shared(client, kind):
        return _FakeChromaCol({})

    monkeypatch.setattr(sp, "get_project_collection", _proj)
    monkeypatch.setattr(sp, "get_shared_collection", _shared)
    db = _FakeDB()
    db.op_log.docs.append({"op_type": "learning.recorded",
                           "ref": {"collection": "proj_havruta", "doc_id": "learning_0123456789abcdef"}})
    out = asyncio.run(sp.check_pointers(
        db, object(), "andbible", "## Key Context\nlearning_0123456789abcdef\n", SPEC_UPDATED))
    assert out["changed"] == [] and seen == ["andbible", "havruta"]
