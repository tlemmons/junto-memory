"""Tests for the idle→park nudge timer (shared_memory.idle_park)."""

import pytest
from mcp import types as _mcp_types

from shared_memory import idle_park
from shared_memory.idle_park import (
    REARM_GRACE_SECONDS,
    build_call_tool_handler_with_idle_park,
    due_agents,
    mark_nudged,
    note_tool_call,
)
from shared_memory.state import active_sessions

KEY = ("junto", "memory")


@pytest.fixture(autouse=True)
def _sessions():
    idle_park._timers.clear()
    active_sessions["s_agent"] = {"project": "junto", "claude_instance": "memory"}
    active_sessions["s_plugin"] = {"project": "junto", "claude_instance": "memory",
                                   "task_description": "junto-inbox channel plugin"}
    yield
    idle_park._timers.clear()
    active_sessions.pop("s_agent", None)
    active_sessions.pop("s_plugin", None)


def _fifty(_p, _a):
    return 50


def test_real_call_arms_one_timer_per_agent_name():
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_send_message", "s_plugin", now=10)
    assert list(idle_park._timers) == [KEY]
    assert idle_park._timers[KEY]["last_real"] == 10


def test_background_tools_never_arm_or_reset():
    for tool in idle_park.BACKGROUND_TOOLS:
        note_tool_call(tool, "s_plugin", now=0)
    assert idle_park._timers == {}
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_heartbeat", "s_plugin", now=2000)
    note_tool_call("memory_report_context", "s_agent", now=2500)
    assert idle_park._timers[KEY]["last_real"] == 0


def test_unknown_session_ignored():
    note_tool_call("memory_query", "nope", now=0)
    note_tool_call("memory_query", None, now=0)
    assert idle_park._timers == {}


def test_due_after_threshold_and_fires_once():
    note_tool_call("memory_query", "s_agent", now=0)
    assert due_agents(_fifty, now=49 * 60) == []
    assert due_agents(_fifty, now=50 * 60) == [("junto", "memory", 50)]
    mark_nudged("junto", "memory", now=50 * 60)
    assert due_agents(_fifty, now=500 * 60) == []


def test_reply_turn_within_grace_does_not_rearm_but_later_activity_does():
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    note_tool_call("memory_get_messages", "s_agent", now=3000 + 30)
    assert idle_park._timers[KEY]["nudged_at"] == 3000
    note_tool_call("memory_query", "s_agent", now=3000 + REARM_GRACE_SECONDS + 1)
    assert idle_park._timers[KEY]["nudged_at"] is None
    assert due_agents(_fifty, now=3000 + REARM_GRACE_SECONDS + 1 + 50 * 60) != []


def test_end_session_disarms():
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_end_session", "s_agent", now=5)
    assert idle_park._timers == {}
    assert due_agents(_fifty, now=10_000) == []


def test_opt_out_and_disabled_threshold_skip():
    note_tool_call("memory_query", "s_agent", now=0)
    assert due_agents(lambda p, a: 0, now=10_000) == []


def test_threshold_from_config_optout(monkeypatch):
    from shared_memory import push_control

    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_minutes": 40, "idle_park_optout": "nimbusSupport, memory"})
    assert idle_park._threshold_from_config(None, "junto", "memory") == 0
    assert idle_park._threshold_from_config(None, "junto", "other") == 40


class _FakeMessages:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)


class _FakeDb:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_sweep_sends_injecting_system_notice(monkeypatch):
    from shared_memory.tools import messaging

    pushed = []

    async def fake_notify(project, agent, packet=None):
        pushed.append((project, agent, packet))

    monkeypatch.setattr(messaging, "_notify_inbox_for_send", fake_notify)
    monkeypatch.setattr(idle_park, "_threshold_from_config", lambda db, p, a: 50)
    note_tool_call("memory_query", "s_agent", now=0)
    idle_park._timers[KEY]["last_real"] -= 10_000_000  # long idle in monotonic terms

    db = _FakeDb()
    assert await idle_park.sweep_once(db) == 1
    assert await idle_park.sweep_once(db) == 0  # once per idle period
    doc = db.messages.docs[0]
    assert doc["to_instance"] == "memory" and doc["system_notice_kind"] == "idle_park_nudge"
    assert pushed[0][2]["mode"] == "inject"


async def test_wrapper_notes_then_delegates():
    seen = []

    async def inner(req):
        seen.append(req.params.name)
        return "ok"

    handler = build_call_tool_handler_with_idle_park(inner)
    req = _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(name="memory_query", arguments={"session_id": "s_agent"}),
    )
    assert await handler(req) == "ok"
    assert seen == ["memory_query"] and KEY in idle_park._timers


class _FakeStatus:
    def __init__(self, tokens):
        self.tokens = tokens

    def find_one(self, query, projection=None):
        return None if self.tokens is None else {"instance": query["instance"], "context_tokens": self.tokens}


class _StatusDb:
    def __init__(self, tokens):
        self.agent_status = _FakeStatus(tokens)


@pytest.mark.parametrize("tokens,expected", [(150_000, 0), (200_000, 50), (600_000, 50), (None, 50)])
def test_min_context_gate(monkeypatch, tokens, expected):
    from shared_memory import push_control

    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_minutes": 50, "idle_park_min_context": 200_000})
    assert idle_park._threshold_from_config(_StatusDb(tokens), "junto", "memory") == expected


# --- Phase 2: auto-reset (design:idle-park-autoreset-v0) -------------------------

class _FakeResult:
    def __init__(self, matched, upserted_id=None):
        self.matched_count = matched
        self.upserted_id = upserted_id


class _FakeResets:
    """Just enough of a pymongo collection for idle_park's reset queries."""

    def __init__(self):
        self.docs = []

    @staticmethod
    def _match(doc, flt):
        for k, cond in flt.items():
            v = doc.get(k)
            if isinstance(cond, dict):
                if "$in" in cond and v not in cond["$in"]:
                    return False
                if "$lt" in cond and not (v is not None and v < cond["$lt"]):
                    return False
            elif v != cond:
                return False
        return True

    @staticmethod
    def _apply(doc, update):
        doc.update(update.get("$set", {}))
        for k, n in update.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + n

    def update_one(self, flt, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, flt):
                self._apply(doc, update)
                return _FakeResult(1)
        if upsert:
            doc = dict(update.get("$setOnInsert", {}))
            self._apply(doc, update)
            self.docs.append(doc)
            return _FakeResult(0, doc["_id"])
        return _FakeResult(0)

    def update_many(self, flt, update):
        n = 0
        for doc in self.docs:
            if self._match(doc, flt):
                self._apply(doc, update)
                n += 1
        return _FakeResult(n)

    def find(self, flt):
        return [d for d in self.docs if self._match(d, flt)]

    def find_one(self, flt):
        found = self.find(flt)
        return found[0] if found else None

    def count_documents(self, flt):
        return len(self.find(flt))


class _ResetsDb:
    def __init__(self):
        self.idle_park_resets = _FakeResets()


@pytest.fixture
def scratch_db():
    idle_park._parked_after_nudge.clear()
    yield _ResetsDb()
    idle_park._parked_after_nudge.clear()


def _autoreset(monkeypatch, agents):
    from shared_memory import push_control

    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_autoreset": agents})


def _nudged_park():
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    note_tool_call("memory_end_session", "s_agent", now=3100)


def test_park_without_nudge_queues_nothing():
    idle_park._parked_after_nudge.clear()
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_end_session", "s_agent", now=5)
    assert list(idle_park._parked_after_nudge) == []


def test_park_after_rearm_queues_nothing():
    idle_park._parked_after_nudge.clear()
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    note_tool_call("memory_query", "s_agent", now=3000 + REARM_GRACE_SECONDS + 1)
    note_tool_call("memory_end_session", "s_agent", now=3000 + REARM_GRACE_SECONDS + 2)
    assert list(idle_park._parked_after_nudge) == []


def test_nudged_park_opted_in_goes_pending_once(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "nimbusSupport, memory")
    _nudged_park()
    _nudged_park()  # a second nudged park while one is pending is a no-op
    resets = idle_park.pending_resets_for(scratch_db, ["memory", "other"])
    assert [(r["project"], r["agent"]) for r in resets] == [("junto", "memory")]
    assert scratch_db.idle_park_resets.count_documents({}) == 1


def test_nudged_park_not_opted_in_is_dropped(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "nimbusSupport")
    _nudged_park()
    assert idle_park.pending_resets_for(scratch_db, ["memory"]) == []
    assert scratch_db.idle_park_resets.count_documents({}) == 0


def test_resets_only_returned_for_reported_agents(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "memory")
    _nudged_park()
    assert idle_park.pending_resets_for(scratch_db, ["someone-else"]) == []
    assert len(idle_park.pending_resets_for(scratch_db, ["memory"])) == 1


def test_results_deferred_keeps_pending_done_closes(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "memory")
    _nudged_park()
    rid = idle_park.pending_resets_for(scratch_db, ["memory"])[0]["reset_id"]

    out = idle_park.apply_reset_results(scratch_db, [{"reset_id": rid, "outcome": "deferred", "detail": "busy"}])
    assert out == {"applied": [rid], "rejected": []}
    doc = scratch_db.idle_park_resets.find_one({"_id": rid})
    assert doc["status"] == "pending" and doc["attempts"] == 1 and doc["last_detail"] == "busy"

    idle_park.apply_reset_results(scratch_db, [{"reset_id": rid, "outcome": "done"}])
    assert scratch_db.idle_park_resets.find_one({"_id": rid})["status"] == "done"
    assert idle_park.pending_resets_for(scratch_db, ["memory"]) == []
    # closed resets can't be reopened; bad outcomes are rejected
    out = idle_park.apply_reset_results(scratch_db, [{"reset_id": rid, "outcome": "failed"},
                                                     {"reset_id": rid, "outcome": "bogus"}])
    assert out["applied"] == [] and len(out["rejected"]) == 2


def test_old_pending_reset_expires(monkeypatch, scratch_db):
    from datetime import timedelta

    from shared_memory.helpers import utc_now

    _autoreset(monkeypatch, "memory")
    _nudged_park()
    rid = idle_park.pending_resets_for(scratch_db, ["memory"])[0]["reset_id"]
    scratch_db.idle_park_resets.update_one(
        {"_id": rid}, {"$set": {"requested_at": utc_now() - timedelta(hours=idle_park.RESET_EXPIRY_HOURS + 1)}})
    assert idle_park.pending_resets_for(scratch_db, ["memory"]) == []
    assert scratch_db.idle_park_resets.find_one({"_id": rid})["status"] == "expired"


# ── turns reported by the statusline (note_turn) ──

from datetime import datetime, timedelta, timezone  # noqa: E402

from shared_memory.idle_park import note_turn  # noqa: E402

WALL = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def _stamp(seconds_before_now):
    return (WALL - timedelta(seconds=seconds_before_now)).isoformat().replace("+00:00", "Z")


def test_turn_without_junto_calls_keeps_agent_from_going_idle():
    # The coordinator case: last junto call at 0, then 40 min of conversation turns.
    note_tool_call("memory_define_spec", "s_agent", now=0)
    assert note_turn("memory", _stamp(60), now=3000, now_wall=WALL) is True
    assert idle_park._timers[KEY]["last_real"] == 2940
    assert due_agents(_fifty, now=3000 + 10) == []
    assert due_agents(_fifty, now=2940 + 50 * 60) == [("junto", "memory", 50)]


def test_turn_never_arms_a_timer():
    assert note_turn("memory", _stamp(0), now=100, now_wall=WALL) is False
    assert idle_park._timers == {}


def test_repeated_or_older_stamp_is_a_no_op():
    note_tool_call("memory_query", "s_agent", now=1000)
    assert note_turn("memory", _stamp(500), now=1000, now_wall=WALL) is False
    assert idle_park._timers[KEY]["last_real"] == 1000


def test_turn_answering_the_nudge_does_not_rearm():
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    assert note_turn("memory", _stamp(0), now=3060, now_wall=WALL) is False
    assert idle_park._timers[KEY]["nudged_at"] == 3000
    later = 3000 + REARM_GRACE_SECONDS + 60
    assert note_turn("memory", _stamp(0), now=later, now_wall=WALL) is True
    assert idle_park._timers[KEY]["nudged_at"] is None


def test_future_stamp_counts_as_now_and_bad_stamps_are_ignored():
    note_tool_call("memory_query", "s_agent", now=0)
    assert note_turn("memory", _stamp(-300), now=500, now_wall=WALL) is True
    assert idle_park._timers[KEY]["last_real"] == 500
    assert note_turn("memory", "not a date", now=900, now_wall=WALL) is False
    assert note_turn("memory", None, now=900, now_wall=WALL) is False
    assert note_turn("other-agent", _stamp(0), now=900, now_wall=WALL) is False
