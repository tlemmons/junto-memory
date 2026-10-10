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
