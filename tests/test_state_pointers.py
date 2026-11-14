"""State-spec pointer check (interface:state-pointer-check-v0).

The pointer check computes which live pointers of a state spec moved after it
was written: closed or touched backlog items, delivered reminders, replies to
cited messages, superseded learnings. Idea borrowed from orbit-spec.
"""

import asyncio
from datetime import datetime

from shared_memory import state_pointers as sp

SPEC_UPDATED = "2026-11-13T03:12:26+00:00"
OWNER = "memory"

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


def _get(d, dotted):
    for part in dotted.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d


class _FakeMongoCol:
    def __init__(self, docs):
        self.docs = docs

    def _match(self, d, q):
        for k, v in q.items():
            cur = _get(d, k)
            if isinstance(v, dict) and "$gt" in v:
                if cur is None or not cur > v["$gt"]:
                    return False
            elif isinstance(v, dict) and "$in" in v:
                if cur not in v["$in"]:
                    return False
            elif cur != v:
                return False
        return True

    def find(self, q, proj=None):
        return [d for d in self.docs if self._match(d, q)]


def _sent(doc_id, ts, frm="memory", to="inbox", reply_to=None):
    payload = {"from_instance": frm, "to_instance": to}
    if reply_to:
        payload["in_response_to"] = reply_to
    return {"op_type": "message.sent", "ref": {"doc_id": doc_id}, "payload": payload, "ts": ts}


class _FakeDB:
    def __init__(self):
        self.scheduled_messages = _FakeMongoCol([{"_id": "msg_sched_222222222222", "status": "delivered"}])
        self.op_log = _FakeMongoCol([
            _sent("msg_111111111111", "2026-11-12T00:00:00+00:00"),
            _sent("msg_999999999999", "2026-11-14T00:00:00+00:00", "inbox", "memory", "msg_111111111111"),
            _sent("msg_888888888888", "2026-11-12T00:00:00+00:00", "inbox", "memory", "msg_111111111111"),
            # Someone else's thread: the owner cites it, but gets no reply count.
            _sent("msg_555555555555", "2026-11-12T00:00:00+00:00", "frames-team", "coordinator"),
            _sent("msg_666666666666", "2026-11-14T00:00:00+00:00", "coordinator", "frames-team",
                  "msg_555555555555"),
        ])
        self.messages = _FakeMongoCol([
            # A live message with no op_log row.
            {"_id": "msg_444444444444", "from_instance": "inbox", "to_instance": "memory"},
            # A live-only reply to msg_111, and the op_log's msg_999 again (count once).
            {"_id": "msg_777777777777", "in_response_to": "msg_111111111111",
             "created_at": datetime(2026, 11, 14, 1, 0, 0)},
            {"_id": "msg_999999999999", "in_response_to": "msg_111111111111",
             "created_at": datetime(2026, 11, 14, 0, 0, 0)},
        ])


class _FakeChromaCol:
    def __init__(self, metas):
        self.metas = metas

    async def get(self, ids=None, include=None):
        hit = [i for i in ids if i in self.metas]
        return {"ids": hit, "metadatas": [self.metas[i] for i in hit]}


def _patch(monkeypatch, project_metas, shared_metas, other=None):
    seen = []

    async def _proj(client, project):
        seen.append(project)
        return _FakeChromaCol((other or {}).get(project, project_metas))

    async def _shared(client, kind):
        return _FakeChromaCol(shared_metas)

    monkeypatch.setattr(sp, "get_project_collection", _proj)
    monkeypatch.setattr(sp, "get_shared_collection", _shared)
    return seen


def _run(content, db=None, updated=SPEC_UPDATED, allowed=None, project="junto"):
    return asyncio.run(sp.check_pointers(db or _FakeDB(), object(), project, content, updated,
                                         owner=OWNER, allowed=allowed))


def test_live_pointers_sections_and_dedup():
    assert sp.live_pointers(CONTENT) == [
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


def test_heading_ends_section():
    content = "## Next Steps\n- backlog_aaaaaaaaaaaa\n## Decisions Taken\n- backlog_bbbbbbbbbbbb\n"
    assert [i for i, _ in sp.live_pointers(content)] == ["backlog_aaaaaaaaaaaa"]


def test_code_fence_comment_is_not_a_heading():
    content = "## Next Steps\n```bash\n# restart server\n```\n- backlog_aaaaaaaaaaaa\n"
    assert [i for i, _ in sp.live_pointers(content)] == ["backlog_aaaaaaaaaaaa"]


def test_reopened_is_not_a_live_heading():
    assert sp.live_pointers("## Reopened (history)\nbacklog_aaaaaaaaaaaa\n") == []


def test_check_pointers_reports_only_changes(monkeypatch):
    _patch(
        monkeypatch,
        {"backlog_aaaaaaaaaaaa": {"backlog_status": "done", "updated": "2026-11-12T00:00:00+00:00"}},
        {"learning_0123456789abcdef": {"status": "superseded"}},
    )
    out = _run(CONTENT)
    assert out["checked"] == 5 and out["truncated"] is False
    assert {c["id"]: c["what"] for c in out["changed"]} == {
        "backlog_aaaaaaaaaaaa": "status done",
        "msg_111111111111": "2 replies since spec written",  # op_log + live-only; older reply excluded
        "msg_sched_222222222222": "delivered",
        "msg_333333333333": "no record (bad id, or aged out before history was kept)",
        "learning_0123456789abcdef": "status superseded",
    }


def test_unchanged_pointers_not_listed(monkeypatch):
    _patch(monkeypatch, {"backlog_aaaaaaaaaaaa": {"backlog_status": "open",
                                                  "updated": "2026-11-01T00:00:00+00:00"}}, {})
    out = _run("## Next Steps\n- backlog_aaaaaaaaaaaa\n")
    assert out["checked"] == 1 and out["changed"] == []


def test_backlog_touched_after_spec(monkeypatch):
    _patch(monkeypatch, {"backlog_aaaaaaaaaaaa": {"backlog_status": "open",
                                                  "updated": "2026-11-14T00:00:00+00:00"}}, {})
    out = _run("## Blockers\nbacklog_aaaaaaaaaaaa\n")
    assert out["changed"][0]["what"] == "updated since spec written (status open)"


def test_backlog_naive_legacy_timestamp_compares_as_utc(monkeypatch):
    _patch(monkeypatch, {"backlog_aaaaaaaaaaaa": {"backlog_status": "open",
                                                  "updated": "2026-11-13T03:12:26"}}, {})
    assert _run("## Blockers\nbacklog_aaaaaaaaaaaa\n")["changed"] == []


def test_empty_spec_updated_skips_time_checks(monkeypatch):
    _patch(monkeypatch, {"backlog_aaaaaaaaaaaa": {"backlog_status": "open",
                                                  "updated": "2026-11-14T00:00:00+00:00"}}, {})
    out = _run("## Next Steps\nbacklog_aaaaaaaaaaaa msg_111111111111\n", updated="")
    assert out["checked"] == 2 and out["changed"] == []


def test_no_reply_count_on_others_threads(monkeypatch):
    _patch(monkeypatch, {}, {})
    assert _run("## Next Steps\nmsg_555555555555\n")["changed"] == []


def test_live_message_without_op_log_is_found(monkeypatch):
    _patch(monkeypatch, {}, {})
    assert _run("## Next Steps\nmsg_444444444444\n")["changed"] == []


def test_no_live_ids_returns_none():
    assert _run("## Status\nbacklog_aaaaaaaaaaaa\n") is None


def test_errors_never_raise(monkeypatch):
    async def _boom(client, project):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(sp, "get_project_collection", _boom)
    assert _run("## Next Steps\nbacklog_aaaaaaaaaaaa\n") == {"error": "RuntimeError: chroma down"}


def test_cross_project_pointers_resolved_when_allowed(monkeypatch):
    seen = _patch(monkeypatch, {}, {}, other={"nimbus": {
        "learning_0123456789abcdef": {"status": "active"},
        "backlog_aaaaaaaaaaaa": {"backlog_status": "done"},
    }})
    db = _FakeDB()
    db.op_log.docs += [
        {"op_type": "learning.recorded", "ref": {"collection": "proj_nimbus", "doc_id": "learning_0123456789abcdef"}},
        {"op_type": "backlog.added", "ref": {"collection": "proj_nimbus", "doc_id": "backlog_aaaaaaaaaaaa"}},
    ]
    out = _run("## Blockers\nbacklog_aaaaaaaaaaaa\n## Key Context\nlearning_0123456789abcdef\n",
               db=db, allowed=lambda c: True)
    assert {c["id"]: c["what"] for c in out["changed"]} == {"backlog_aaaaaaaaaaaa": "status done"}
    assert "nimbus" in seen


def test_cross_project_lookup_denied_without_access(monkeypatch):
    seen = _patch(monkeypatch, {}, {}, other={"nimbus": {"learning_0123456789abcdef": {"status": "active"}}})
    db = _FakeDB()
    db.op_log.docs.append(
        {"op_type": "learning.recorded", "ref": {"collection": "proj_nimbus", "doc_id": "learning_0123456789abcdef"}})
    out = _run("## Key Context\nlearning_0123456789abcdef\n", db=db)
    assert out["changed"][0]["what"] == "not found" and "nimbus" not in seen
