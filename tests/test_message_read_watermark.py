"""Read-watermark regression (design:message-read-watermark-v0, 2026-06-10).

memory_get_messages had no per-recipient read state, so every `go` redisplayed
the full ~7-day window (the `delivered` flag is computed from status, and status
never leaves "pending"). These tests pin the watermark contract:

  - a top read by the owning agent defaults to unseen-only (created_at > watermark)
  - the watermark advances forward-only, and ONLY when the complete unseen set
    was handed over (has_more=False) — a truncated page must NOT advance it
    (else the older-unseen tail is silently dropped)
  - include_seen=True is a full-window catch-up that neither filters nor advances
  - cursor pagination / for_instance peeks bypass the watermark entirely

The inbox:// resource path (push delivery, control UI) is a SEPARATE function
(read_inbox) and is intentionally not touched by this filter.
"""

import json
from datetime import timedelta

from shared_memory.helpers import utc_now


# ── Minimal Mongo query matcher (only the shapes get_messages produces) ──
def _match(doc, query):
    for key, cond in query.items():
        if key == "$and":
            if not all(_match(doc, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_match(doc, c) for c in cond):
                return False
        elif isinstance(cond, dict):
            val = doc.get(key)
            for op, operand in cond.items():
                if op == "$gt" and not (val is not None and val > operand):
                    return False
                elif op == "$lt" and not (val is not None and val < operand):
                    return False
                elif op == "$gte" and not (val is not None and val >= operand):
                    return False
                elif op == "$ne" and val == operand:
                    return False
                elif op == "$in" and val not in operand:
                    return False
                elif op == "$exists":
                    present = key in doc
                    if present != operand:
                        return False
        else:
            if doc.get(key) != cond:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec):
        # Apply spec right-to-left so the leftmost key is primary.
        for field, direction in reversed(spec):
            self._docs.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction < 0),
            )
        return self

    def limit(self, n):
        return iter(self._docs[:n])


class _FakeMessages:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def find(self, query, projection=None):
        return _FakeCursor([d for d in self._docs if _match(d, query)])

    def count_documents(self, query):
        return sum(1 for d in self._docs if _match(d, query))

    def insert_one(self, doc):
        self._docs.append(dict(doc))


class _FakeAgentDirectory:
    def __init__(self):
        self.rows = {}

    def find_one(self, filt, projection=None):
        return self.rows.get((filt.get("project"), filt.get("instance")))

    def update_one(self, filt, upd, upsert=False):
        key = (filt.get("project"), filt.get("instance"))
        row = self.rows.get(key)
        if row is None:
            if not upsert:
                return
            row = {"project": filt.get("project"), "instance": filt.get("instance")}
            self.rows[key] = row
        for k, v in (upd.get("$set") or {}).items():
            row[k] = v
        for k, v in (upd.get("$max") or {}).items():
            if row.get(k) is None or v > row[k]:
                row[k] = v


class _FakeDB:
    def __init__(self, messages, directory=None):
        self.messages = _FakeMessages(messages)
        self.agent_directory = directory or _FakeAgentDirectory()


def _msg(_id, created_at, to_instance="memory", to_project="junto"):
    return {
        "_id": _id,
        "to_instance": to_instance,
        "to_project": to_project,
        "from_instance": "peer",
        "from_project": "junto",
        "message": _id,
        "category": "info",
        "priority": "normal",
        "status": "pending",
        "created_at": created_at,
    }


def _setup(monkeypatch, fake_db, instance="memory", project="junto", role="agent"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m

    sid = f"_test_watermark_{instance}_{id(fake_db)}"
    active_sessions[sid] = {"role": role, "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_is_project_admin", lambda *a, **k: False)
    return sid, m, active_sessions


async def test_default_read_filters_out_seen_messages(monkeypatch):
    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    new = _msg("new", now)
    directory = _FakeAgentDirectory()
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory",
        "messages_seen_through": now - timedelta(hours=1),
    }
    fake_db = _FakeDB([old, new], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid))
        ids = [x["id"] for x in res["messages"]]
        assert ids == ["new"], f"watermark should hide 'old'; got {ids}"
    finally:
        sessions.pop(sid, None)


async def test_full_read_advances_watermark_forward(monkeypatch):
    now = utc_now()
    m1 = _msg("m1", now - timedelta(minutes=10))
    m2 = _msg("m2", now)
    fake_db = _FakeDB([m1, m2])  # no prior watermark
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=20))
        assert res["has_more"] is False
        assert {x["id"] for x in res["messages"]} == {"m1", "m2"}
        row = fake_db.agent_directory.rows[("junto", "memory")]
        assert row["messages_seen_through"] == now, "watermark should advance to newest"
    finally:
        sessions.pop(sid, None)


async def test_recency_primary_sort_does_not_strand_urgent(monkeypatch):
    """design:inbox-surfacing-v0: priority is a STRING, so DB-sorting on it
    (low<normal<urgent) before .limit() stranded urgent behind a large normal
    backlog. Recency-primary must surface the newest messages regardless of
    priority — a recent urgent is never buried; a stale urgent pages back.
    Fails on the old [("priority",1),("created_at",-1)] sort."""
    now = utc_now()
    docs = [_msg(f"normal{i}", now - timedelta(minutes=10 - i)) for i in range(5)]
    u_new = _msg("urgent_new", now)  # newest of all
    u_new["priority"] = "urgent"
    docs.append(u_new)
    u_old = _msg("urgent_old", now - timedelta(hours=3))  # stale urgent
    u_old["priority"] = "urgent"
    docs.append(u_old)

    fake_db = _FakeDB(docs)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=3))
        ids = {x["id"] for x in res["messages"]}
        # newest 3 by created_at = urgent_new(now), normal4(-6m), normal3(-7m)
        assert "urgent_new" in ids, f"recent urgent stranded behind normal: {ids}"
        assert "urgent_old" not in ids, f"stale urgent should page back: {ids}"
        assert res["has_more"] is True
    finally:
        sessions.pop(sid, None)


async def test_truncated_read_does_not_advance_watermark(monkeypatch):
    """has_more=True means the agent saw only a page; advancing would silently
    drop the older-unseen tail. The watermark must stay put."""
    now = utc_now()
    m1 = _msg("m1", now - timedelta(minutes=10))
    m2 = _msg("m2", now)
    fake_db = _FakeDB([m1, m2])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=1))
        assert res["has_more"] is True
        assert fake_db.agent_directory.rows.get(("junto", "memory")) is None, (
            "watermark must NOT advance on a truncated read"
        )
    finally:
        sessions.pop(sid, None)


async def test_cursor_read_bypasses_and_does_not_advance(monkeypatch):
    """CONTRACT INVARIANT (inbox sign-off, msg_ff971862026c): get_messages with
    a cursor present never watermark-filters AND never advances. The junto-inbox
    plugin's page-2+ pagination (readInboxAndForward) relies on this — if it
    regresses, the plugin silently drops the page-2+ tail of >20-msg backlogs."""
    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    directory = _FakeAgentDirectory()
    wm = now  # watermark AHEAD of everything — would hide 'old' if applied
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory", "messages_seen_through": wm,
    }
    fake_db = _FakeDB([old], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        cursor = (now - timedelta(hours=1)).isoformat()
        res = json.loads(
            await m.memory_get_messages(session_id=sid, cursor=cursor, limit=20)
        )
        assert [x["id"] for x in res["messages"]] == ["old"], (
            "cursor read must ignore the watermark filter"
        )
        assert directory.rows[("junto", "memory")]["messages_seen_through"] == wm, (
            "cursor read must not advance the watermark"
        )
    finally:
        sessions.pop(sid, None)


async def test_inbox_resource_never_filters_never_advances(monkeypatch):
    """CONTRACT INVARIANT (inbox sign-off, msg_ff971862026c): inbox:// resource
    reads (read_inbox — the plugin's page-1 push path and control's UI) are
    never watermark-filtered and never advance the watermark."""
    from shared_memory.tools import messaging as m

    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    new = _msg("new", now)
    directory = _FakeAgentDirectory()
    wm = now + timedelta(minutes=1)  # ahead of BOTH messages
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory", "messages_seen_through": wm,
    }
    fake_db = _FakeDB([old, new], directory)
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_check_inbox_authz", lambda p, a: (True, ""))
    monkeypatch.setattr(
        m.push_control, "should_deliver_via_push_filter", lambda db, p, a: True
    )
    res = json.loads(await m.read_inbox("junto", "memory"))
    assert {x["id"] for x in res["messages"]} == {"old", "new"}, (
        "resource read must return the full pending window regardless of watermark"
    )
    assert directory.rows[("junto", "memory")]["messages_seen_through"] == wm, (
        "resource read must not advance the watermark"
    )


async def test_include_seen_bypasses_and_does_not_advance(monkeypatch):
    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    new = _msg("new", now)
    directory = _FakeAgentDirectory()
    wm = now - timedelta(hours=1)
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory", "messages_seen_through": wm,
    }
    fake_db = _FakeDB([old, new], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(
            await m.memory_get_messages(session_id=sid, include_seen=True, limit=20)
        )
        assert {x["id"] for x in res["messages"]} == {"old", "new"}, "catch-up shows all"
        assert fake_db.agent_directory.rows[("junto", "memory")]["messages_seen_through"] == wm, (
            "include_seen must not advance the watermark"
        )
    finally:
        sessions.pop(sid, None)
