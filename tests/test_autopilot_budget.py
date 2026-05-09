"""Bug A regression (2026-05-09): budget auto-disable must fire on
uniformly-depth-breach traffic.

Pre-fix the depth_breach early-return at autopilot.py:519 returned before the
budget gate at autopilot.py:522, so an agent receiving traffic with chain_depth
above its depth_cap could exceed hourly_budget without ever tripping the
auto-disable side effect. Surfaced live on server-team@nimbus 2026-05-09 with
depth_cap=1 (junto-inbox session-bind default) and 18+ events accumulated
against budget=10 with enabled still True.
"""

import json


class _FakeAutopilotEvents:
    def __init__(self):
        self._docs = []

    def insert_one(self, doc):
        self._docs.append(dict(doc))

    def count_documents(self, filt):
        proj = filt.get("project")
        agent = filt.get("agent")
        cutoff = filt.get("logged_at", {}).get("$gte") if isinstance(filt.get("logged_at"), dict) else None
        n = 0
        for d in self._docs:
            if d.get("project") != proj or d.get("agent") != agent:
                continue
            if cutoff is not None and d.get("logged_at") < cutoff:
                continue
            n += 1
        return n


class _FakeAgentAutopilot:
    def __init__(self, doc):
        self._doc = dict(doc) if doc else None

    def find_one(self, filt):
        if (
            self._doc
            and self._doc.get("project") == filt.get("project")
            and self._doc.get("agent") == filt.get("agent")
        ):
            return dict(self._doc)
        return None

    def update_one(self, filt, upd):
        if (
            self._doc
            and self._doc.get("project") == filt.get("project")
            and self._doc.get("agent") == filt.get("agent")
        ):
            for k, v in (upd.get("$set") or {}).items():
                self._doc[k] = v

            class _R:
                modified_count = 1

            return _R()

        class _R0:
            modified_count = 0

        return _R0()


class _FakeMessages:
    def __init__(self):
        self._docs = []

    def find_one(self, filt, projection=None):
        for d in self._docs:
            if d.get("_id") == filt.get("_id"):
                return d
        return None

    def insert_one(self, doc):
        self._docs.append(dict(doc))


class _FakeDB:
    def __init__(self, autopilot_doc):
        self.autopilot_events = _FakeAutopilotEvents()
        self.agent_autopilot = _FakeAgentAutopilot(autopilot_doc)
        self.messages = _FakeMessages()


async def test_budget_auto_disable_fires_on_uniformly_depth_breach_traffic(monkeypatch):
    """The 11th depth-breach call against budget=10 must auto-disable.

    Pre-2026-05-09 fix: depth_breach branch returned at autopilot.py:519
    before reaching the budget gate, so this test would FAIL — the row would
    stay enabled=True forever.
    """
    from shared_memory.state import active_sessions
    from shared_memory.tools import autopilot as ap_mod

    sid = "_test_autopilot_bug_a_regression"
    active_sessions[sid] = {
        "role": "agent",
        "claude_instance": "test",
        "project": "nimbus",
    }

    fake_db = _FakeDB(
        {
            "project": "nimbus",
            "agent": "server-team",
            "enabled": True,
            "depth_cap": 1,
            "hourly_budget": 10,
            "destructive_gate": False,
            "paused_at": None,
            "paused_reason": "",
        }
    )

    monkeypatch.setattr(ap_mod, "get_mongo", lambda: fake_db)

    try:
        responses = []
        for _ in range(11):
            r = await ap_mod.memory_autopilot_check_budget(
                session_id=sid,
                project="nimbus",
                agent="server-team",
                chain_depth=2,  # exceeds depth_cap=1 — uniformly depth-breach
                message_id=None,
            )
            responses.append(json.loads(r))

        # First 10 calls: count goes 1..10, all <= budget=10, no auto-disable
        for i, r in enumerate(responses[:10]):
            assert r["allowed"] is False
            assert r.get("auto_disabled") is not True, (
                f"auto-disable fired prematurely at call {i + 1} (count={r['current_count']})"
            )
            assert r["depth_breach"] is True, (
                f"call {i + 1} should report depth_breach=True; got {r}"
            )

        # 11th call: count=11 > budget=10, must auto-disable
        last = responses[10]
        assert last["current_count"] == 11
        assert last["auto_disabled"] is True, (
            f"auto-disable did not fire on 11th depth-breach call; response: {last}"
        )

        # Verify the agent_autopilot row was actually flipped
        row = fake_db.agent_autopilot.find_one(
            {"project": "nimbus", "agent": "server-team"}
        )
        assert row["enabled"] is False
        assert row["paused_at"] is not None
        assert row["updated_by"] == "system:autopilot-budget"
        assert "hourly budget breached" in row["paused_reason"]

        # Verify the system blocker message was inserted
        assert len(fake_db.messages._docs) == 1
        alert = fake_db.messages._docs[0]
        assert alert["category"] == "blocker"
        assert alert["priority"] == "urgent"
        assert alert["from_instance"] == "system"
        assert alert["to_instance"] == "server-team"
    finally:
        active_sessions.pop(sid, None)


async def test_budget_breach_includes_depth_breach_flag_in_response(monkeypatch):
    """When both gates trip, the auto-disable response should report
    depth_breach=True so the caller knows both conditions held. This is
    secondary to the auto-disable side effect but useful for observability.
    """
    from shared_memory.state import active_sessions
    from shared_memory.tools import autopilot as ap_mod

    sid = "_test_autopilot_bug_a_response_shape"
    active_sessions[sid] = {
        "role": "agent",
        "claude_instance": "test",
        "project": "nimbus",
    }
    fake_db = _FakeDB(
        {
            "project": "nimbus",
            "agent": "server-team",
            "enabled": True,
            "depth_cap": 1,
            "hourly_budget": 2,  # tighter to test in fewer iterations
            "destructive_gate": False,
            "paused_at": None,
            "paused_reason": "",
        }
    )
    monkeypatch.setattr(ap_mod, "get_mongo", lambda: fake_db)

    try:
        for _ in range(2):
            await ap_mod.memory_autopilot_check_budget(
                session_id=sid,
                project="nimbus",
                agent="server-team",
                chain_depth=2,
                message_id=None,
            )
        r = json.loads(
            await ap_mod.memory_autopilot_check_budget(
                session_id=sid,
                project="nimbus",
                agent="server-team",
                chain_depth=2,
                message_id=None,
            )
        )
        assert r["auto_disabled"] is True
        assert r["depth_breach"] is True
        assert r["current_count"] == 3
    finally:
        active_sessions.pop(sid, None)
