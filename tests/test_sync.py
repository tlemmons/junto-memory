"""Unit tests for memory_sync_pull — Phase 2 replication read endpoint.

Covers `design:local-first-junto-v0-mvp` v0.4.0 §5.1.

Targets the `_pull_op_log` core helper for cursor / pagination / project-
filter semantics, the "sync" auth permission, and the `_jsonable` JSON
date serializer. End-to-end the async tool is exercised via asyncio.run
with monkeypatched state.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from shared_memory import auth
from shared_memory import op_log
from shared_memory.tools import sync as sync_tool


# ───────────────────────────────────────────────────────────────────
# Fakes that mimic just enough of pymongo for _pull_op_log
# ───────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def sort(self, spec):
        # spec is list of (field, direction); we only care about seq ASC
        for field, direction in reversed(spec):
            self._rows.sort(key=lambda r: r.get(field), reverse=(direction < 0))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeOpLog:
    def __init__(self, rows):
        self._rows = rows

    def distinct(self, field):
        return sorted({r.get(field) for r in self._rows if r.get(field) is not None})

    def find(self, query, projection=None):
        # projection is a Mongo perf hint (only fetch listed fields); the
        # fake honors the find() filter shape and ignores projection — tests
        # work against full rows.
        out = []
        for r in self._rows:
            if not self._match(r, query):
                continue
            out.append(r)
        return _FakeCursor(out)

    @staticmethod
    def _match(row, q):
        for k, v in q.items():
            if k == "$or":
                if not any(_FakeOpLog._match(row, sub) for sub in v):
                    return False
                continue
            actual = _resolve(row, k)
            if isinstance(v, dict):
                for op, arg in v.items():
                    if op == "$gt":
                        if not (isinstance(actual, (int, float)) and actual > arg):
                            return False
                    elif op == "$in":
                        if actual not in arg:
                            return False
                    elif op == "$exists":
                        present = _has_path(row, k)
                        if present is not arg:
                            return False
                    else:  # pragma: no cover
                        raise NotImplementedError(f"fake doesn't support {op}")
            else:
                if actual != v:
                    return False
        return True


def _resolve(row, dotted):
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _has_path(row, dotted):
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


class _FakeDB:
    def __init__(self, rows):
        self._cols = {op_log.OPLOG_COLLECTION: _FakeOpLog(rows)}

    def __getitem__(self, name):
        return self._cols[name]


# ───────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────


def _op(seq, *, origin="central", op_type="learning.recorded", project="junto", **extra):
    """Make an op_log row matching the §4.2 schema shape."""
    base = {
        "_id": f"op_seq{seq}",
        "seq": seq,
        "ts": "2026-05-15T16:00:00+00:00",
        "origin": origin,
        "intent_id": None,
        "actor": {"agent": "memory", "project": project, "session_id": "s"},
        "op_type": op_type,
        "ref": {"collection": "shared_patterns", "doc_id": f"doc_{seq}"},
        "payload": {"i": seq},
        "schema_version": 1,
    }
    base.update(extra)
    return base


# ───────────────────────────────────────────────────────────────────
# Core _pull_op_log logic
# ───────────────────────────────────────────────────────────────────


def test_pull_cold_start_returns_everything_sorted():
    rows = [_op(3), _op(1), _op(2)]
    db = _FakeDB(rows)

    result = sync_tool._pull_op_log(db, cursors={}, limit=500, projects=None)

    assert [o["seq"] for o in result["ops"]] == [1, 2, 3]
    assert result["next_cursor"] == {"central": 3}
    assert result["has_more"] == {"central": False}


def test_pull_advances_past_cursor():
    rows = [_op(s) for s in [1, 2, 3, 4, 5]]
    db = _FakeDB(rows)

    result = sync_tool._pull_op_log(db, cursors={"central": 3}, limit=500, projects=None)

    assert [o["seq"] for o in result["ops"]] == [4, 5]
    assert result["next_cursor"] == {"central": 5}
    assert result["has_more"] == {"central": False}


def test_pull_respects_limit_and_sets_has_more():
    rows = [_op(s) for s in range(1, 11)]  # seq 1..10
    db = _FakeDB(rows)

    result = sync_tool._pull_op_log(db, cursors={}, limit=3, projects=None)

    assert [o["seq"] for o in result["ops"]] == [1, 2, 3]
    assert result["next_cursor"] == {"central": 3}
    assert result["has_more"] == {"central": True}


def test_pull_exhausts_paged_origin():
    rows = [_op(s) for s in range(1, 11)]
    db = _FakeDB(rows)

    page1 = sync_tool._pull_op_log(db, cursors={}, limit=3, projects=None)
    page2 = sync_tool._pull_op_log(db, cursors=page1["next_cursor"], limit=3, projects=None)
    page3 = sync_tool._pull_op_log(db, cursors=page2["next_cursor"], limit=3, projects=None)
    page4 = sync_tool._pull_op_log(db, cursors=page3["next_cursor"], limit=3, projects=None)

    seen = [o["seq"] for o in page1["ops"] + page2["ops"] + page3["ops"] + page4["ops"]]
    assert seen == list(range(1, 11))
    assert page4["has_more"].get("central", False) is False


def test_pull_multi_origin_independent_cursors():
    rows = [
        _op(1, origin="central"),
        _op(2, origin="central"),
        _op(1, origin="lan-spg"),
        _op(2, origin="lan-spg"),
        _op(3, origin="lan-spg"),
    ]
    db = _FakeDB(rows)

    # Caller has 'central' up to 1 but has never seen 'lan-spg'
    result = sync_tool._pull_op_log(
        db, cursors={"central": 1}, limit=500, projects=None
    )

    assert sorted([(o["origin"], o["seq"]) for o in result["ops"]]) == [
        ("central", 2),
        ("lan-spg", 1),
        ("lan-spg", 2),
        ("lan-spg", 3),
    ]
    assert result["next_cursor"] == {"central": 2, "lan-spg": 3}
    assert result["has_more"] == {"central": False, "lan-spg": False}


def test_pull_origin_in_cursor_but_absent_from_db_returns_nothing_for_it():
    rows = [_op(s, origin="central") for s in [1, 2]]
    db = _FakeDB(rows)

    result = sync_tool._pull_op_log(
        db, cursors={"central": 0, "phantom-origin": 5}, limit=500, projects=None
    )

    # Phantom origin contributes no rows and is absent from next_cursor.
    assert [o["seq"] for o in result["ops"]] == [1, 2]
    assert "phantom-origin" not in result["next_cursor"]
    assert "phantom-origin" not in result["has_more"]


def test_pull_project_filter_includes_only_matching_project():
    rows = [
        _op(1, project="junto"),
        _op(2, project="nimbus"),
        _op(3, project="junto"),
        _op(4, project="claudecontrol"),
    ]
    db = _FakeDB(rows)

    result = sync_tool._pull_op_log(
        db, cursors={}, limit=500, projects=["junto"]
    )

    assert [o["seq"] for o in result["ops"]] == [1, 3]


def test_pull_project_filter_passes_through_internal_events():
    """Ops without actor.project (server-side internal events) are always
    delivered regardless of project filter — see module docstring OQ #2."""
    rows = [
        _op(1, project="junto"),
        # Internal event with no project at all
        {
            "_id": "op_internal_2",
            "seq": 2,
            "ts": "2026-05-15T16:00:00+00:00",
            "origin": "central",
            "intent_id": None,
            "actor": {"agent": "system", "session_id": "internal"},  # NO project
            "op_type": "lock.expired",
            "ref": {"collection": "locks", "doc_id": "lock_x"},
            "payload": {},
            "schema_version": 1,
        },
        _op(3, project="nimbus"),
    ]
    db = _FakeDB(rows)

    result = sync_tool._pull_op_log(
        db, cursors={}, limit=500, projects=["junto"]
    )

    seen_seqs = sorted(o["seq"] for o in result["ops"])
    assert seen_seqs == [1, 2]


def test_pull_empty_db_returns_clean_empty():
    db = _FakeDB([])
    result = sync_tool._pull_op_log(db, cursors={}, limit=500, projects=None)
    assert result["ops"] == []
    assert result["next_cursor"] == {}
    assert result["has_more"] == {}


def test_pull_response_carries_server_origin():
    db = _FakeDB([])
    result = sync_tool._pull_op_log(db, cursors={}, limit=500, projects=None)
    from shared_memory.config import ORIGIN_SERVER_ID
    assert result["server_origin"] == ORIGIN_SERVER_ID


# ───────────────────────────────────────────────────────────────────
# Auth: sync permission table
# ───────────────────────────────────────────────────────────────────


def test_sync_permission_admin_owner_only():
    assert auth.PERMISSIONS["sync"] == ["admin", "owner"]


def test_sync_permission_denies_agent_and_user():
    assert auth.check_permission("agent", "sync") is False
    assert auth.check_permission("user", "sync") is False
    assert auth.check_permission("readonly", "sync") is False
    assert auth.check_permission("admin", "sync") is True
    assert auth.check_permission("owner", "sync") is True


# ───────────────────────────────────────────────────────────────────
# JSON serialization
# ───────────────────────────────────────────────────────────────────


def test_jsonable_converts_datetime_to_isoformat():
    dt = datetime(2026, 5, 15, 16, 30, 0, tzinfo=timezone.utc)
    out = sync_tool._jsonable(dt)
    assert out == "2026-05-15T16:30:00+00:00"


def test_jsonable_raises_for_unknown_types():
    with pytest.raises(TypeError):
        sync_tool._jsonable(object())


def test_pull_serializes_datetime_payload_via_tool():
    """End-to-end: the async tool serializes a payload containing a
    datetime without raising, and the JSON round-trips clean."""
    from shared_memory import state

    rows = [
        _op(
            1,
            payload={
                "body": "hello",
                "created_at": datetime(2026, 5, 15, 16, 0, 0, tzinfo=timezone.utc),
            },
        )
    ]
    fake_db = _FakeDB(rows)
    fake_session_id = "fake_sync_session"

    # Pin a fake admin session in active_sessions
    state.active_sessions[fake_session_id] = {
        "claude_instance": "memory",
        "project": "junto",
        "role": "admin",
    }
    try:
        # Patch get_mongo at the lookup site (sync.py imports get_mongo
        # by name, so the closest live reference is on the module).
        original_get_mongo = sync_tool.get_mongo
        sync_tool.get_mongo = lambda: fake_db
        try:
            raw = asyncio.run(
                sync_tool.memory_sync_pull(
                    session_id=fake_session_id,
                    since_cursor_by_origin=None,
                    limit=500,
                    projects=None,
                )
            )
        finally:
            sync_tool.get_mongo = original_get_mongo
    finally:
        del state.active_sessions[fake_session_id]

    parsed = json.loads(raw)
    assert parsed["ops"][0]["payload"]["created_at"] == "2026-05-15T16:00:00+00:00"


def test_pull_tool_rejects_missing_session():
    """No session_id → require_session error returned verbatim (not JSON)."""
    raw = asyncio.run(
        sync_tool.memory_sync_pull(
            session_id="nope-not-real",
            since_cursor_by_origin=None,
            limit=500,
        )
    )
    assert "Session" in raw and "not found" in raw


def test_pull_tool_rejects_invalid_limit():
    from shared_memory import state

    state.active_sessions["limit_test"] = {
        "claude_instance": "memory",
        "project": "junto",
        "role": "admin",
    }
    try:
        raw = asyncio.run(
            sync_tool.memory_sync_pull(
                session_id="limit_test",
                limit=0,
            )
        )
    finally:
        del state.active_sessions["limit_test"]

    parsed = json.loads(raw)
    assert "limit" in parsed["error"].lower()


# ───────────────────────────────────────────────────────────────────
# head_only mode — backlog_2e601854581f (cursor-head probe)
# ───────────────────────────────────────────────────────────────────


def test_head_only_returns_no_ops():
    rows = [_op(1), _op(2), _op(3)]
    db = _FakeDB(rows)
    result = sync_tool._pull_op_log(
        db, cursors={}, limit=500, projects=None, head_only=True
    )
    assert result["ops"] == []


def test_head_only_returns_max_seq_per_origin():
    rows = [
        _op(1, origin="A"), _op(5, origin="A"), _op(3, origin="A"),
        _op(2, origin="B"), _op(7, origin="B"),
    ]
    db = _FakeDB(rows)
    result = sync_tool._pull_op_log(
        db, cursors={}, limit=500, projects=None, head_only=True
    )
    assert result["next_cursor"] == {"A": 5, "B": 7}
    assert result["has_more"] == {"A": True, "B": True}


def test_head_only_respects_cursor():
    rows = [_op(1), _op(2), _op(3), _op(4)]
    db = _FakeDB(rows)
    result = sync_tool._pull_op_log(
        db, cursors={"central": 2}, limit=500, projects=None, head_only=True
    )
    assert result["next_cursor"] == {"central": 4}
    assert result["has_more"] == {"central": True}


def test_head_only_origin_caught_up_returns_empty_for_it():
    rows = [_op(1), _op(2), _op(3)]
    db = _FakeDB(rows)
    result = sync_tool._pull_op_log(
        db, cursors={"central": 3}, limit=500, projects=None, head_only=True
    )
    assert result["next_cursor"] == {}
    assert result["has_more"] == {}
    assert result["ops"] == []


def test_head_only_with_project_filter():
    rows = [
        _op(1, project="junto"),
        _op(2, project="nimbus"),
        _op(3, project="junto"),
        _op(4, project="nimbus"),
    ]
    db = _FakeDB(rows)
    result = sync_tool._pull_op_log(
        db, cursors={}, limit=500, projects=["junto"], head_only=True
    )
    assert result["next_cursor"] == {"central": 3}


def test_head_only_ignores_limit():
    """head_only path always returns just the head regardless of limit value."""
    rows = [_op(seq) for seq in range(1, 11)]
    db = _FakeDB(rows)
    result = sync_tool._pull_op_log(
        db, cursors={}, limit=1, projects=None, head_only=True
    )
    assert result["next_cursor"] == {"central": 10}
    assert result["ops"] == []


def test_head_only_empty_db_returns_empty_cursor():
    db = _FakeDB([])
    result = sync_tool._pull_op_log(
        db, cursors={}, limit=500, projects=None, head_only=True
    )
    assert result["next_cursor"] == {}
    assert result["ops"] == []


# ───────────────────────────────────────────────────────────────────
# memory_sync_push: covered in tests/test_sync_push.py against the
# materializer. The stub-era test that lived here was retired when the
# materializer shipped (commit landing this).
# ───────────────────────────────────────────────────────────────────
