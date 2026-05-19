"""Synthetic validation for Phase 1f — push-control config CRUD + alert ack.

Calls the push_control helpers directly (bypassing memory_admin's auth
gate) since we don't have an owner-tier API key in the test environment.
Validates that the underlying set/get/reset/ack/unsuspend behavior is
correct end-to-end against Mongo.

Usage:
    docker exec mcp-rag-arch python /tmp/validate_phase_1f.py
"""

import sys
from datetime import datetime, timedelta, timezone

from shared_memory import push_control
from shared_memory.clients import get_mongo


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}")


def main() -> None:
    db = get_mongo()
    if db is None:
        fail("MongoDB unavailable")

    print("=== Phase 1f validation: config CRUD + alert ack ===\n")

    # ── Pre-clean
    db.push_control_config.delete_many({"scope": {"$in": ["_pc_test_project_", push_control.DEFAULT_SCOPE]}})
    db.alerts.delete_many({"_id": {"$regex": "^alert_pctest_"}})
    db.registered_agents.delete_many({"name": "_pc_test_agent_"})

    # ── 1. get_effective_config with no docs → all defaults
    cfg = push_control.get_effective_config(db, None)
    if cfg["depth_cap"] != push_control.DEFAULT_DEPTH_CAP:
        fail(f"expected default depth_cap={push_control.DEFAULT_DEPTH_CAP}, got {cfg['depth_cap']}")
    if cfg["push_budget"] != push_control.DEFAULT_PUSH_BUDGET:
        fail(f"expected default push_budget={push_control.DEFAULT_PUSH_BUDGET}, got {cfg['push_budget']}")
    if cfg["_sources"]["depth_cap"] != "code_default":
        fail(f"expected depth_cap source code_default, got {cfg['_sources']['depth_cap']}")
    ok(f"defaults: depth_cap={cfg['depth_cap']} push_budget={cfg['push_budget']} hard_ceiling={cfg['hard_ceiling']}")

    # ── 2. set_config_value: server default
    r = push_control.set_config_value(db, None, "depth_cap", 14, actor="test_validator")
    if "ok" not in r or not r["ok"]:
        fail(f"set_config_value(server) failed: {r}")
    ok(f"set server default depth_cap=14: {r}")

    cfg = push_control.get_effective_config(db, None)
    if cfg["depth_cap"] != 14:
        fail(f"after set: expected 14, got {cfg['depth_cap']}")
    if cfg["_sources"]["depth_cap"] != "server_default":
        fail(f"expected source server_default, got {cfg['_sources']['depth_cap']}")
    ok(f"server default updated: depth_cap=14 from {cfg['_sources']['depth_cap']}")

    # ── 3. set_config_value: project override
    r = push_control.set_config_value(db, "_pc_test_project_", "depth_cap", 8, actor="test_validator")
    if "ok" not in r or not r["ok"]:
        fail(f"set project override failed: {r}")
    cfg = push_control.get_effective_config(db, "_pc_test_project_")
    if cfg["depth_cap"] != 8:
        fail(f"project override not applied: {cfg['depth_cap']}")
    ok(f"project override: _pc_test_project_.depth_cap=8")

    # Server-default project still sees the server default (14)
    cfg_other = push_control.get_effective_config(db, "some_other_project")
    if cfg_other["depth_cap"] != 14:
        fail(f"some_other_project: expected 14 (server default), got {cfg_other['depth_cap']}")
    ok(f"server default applies to other projects: depth_cap=14")

    # ── 4. Validation: bad recovery_behavior
    r = push_control.set_config_value(db, None, "recovery_behavior", "invalid_mode", actor="test")
    if "error" not in r:
        fail(f"expected validation error on invalid recovery_behavior, got {r}")
    ok(f"validation rejects bad recovery_behavior: {r['error']}")

    # ── 5. Validation: hard_ceiling < push_budget
    r = push_control.set_config_value(db, None, "hard_ceiling", 10, actor="test")
    if "error" not in r:
        fail(f"expected validation error on hard_ceiling < push_budget, got {r}")
    ok(f"validation rejects hard_ceiling<push_budget: {r['error']}")

    # ── 6. Validation: unknown key
    r = push_control.set_config_value(db, None, "bogus_key", 99, actor="test")
    if "error" not in r:
        fail(f"expected unknown-key error, got {r}")
    ok(f"validation rejects unknown key: {r['error']}")

    # ── 7. Reset one key on the project override
    r = push_control.reset_config(db, "_pc_test_project_", "depth_cap", actor="test_validator")
    if "ok" not in r or not r["ok"]:
        fail(f"reset key failed: {r}")
    cfg = push_control.get_effective_config(db, "_pc_test_project_")
    # depth_cap should now fall back to server default (14)
    if cfg["depth_cap"] != 14:
        fail(f"after reset: expected fallback to 14, got {cfg['depth_cap']}")
    ok(f"reset key falls back to server default: depth_cap=14")

    # ── 8. Reset all overrides on the project
    push_control.set_config_value(db, "_pc_test_project_", "push_budget", 25, actor="test")
    push_control.set_config_value(db, "_pc_test_project_", "hard_ceiling", 80, actor="test")
    r = push_control.reset_config(db, "_pc_test_project_", None, actor="test_validator")
    if "ok" not in r or not r["ok"]:
        fail(f"reset all failed: {r}")
    cfg = push_control.get_effective_config(db, "_pc_test_project_")
    if cfg["push_budget"] != push_control.DEFAULT_PUSH_BUDGET:
        fail(f"after reset-all push_budget: expected default {push_control.DEFAULT_PUSH_BUDGET}, got {cfg['push_budget']}")
    ok(f"reset-all clears project overrides")

    # ── 9. Cannot reset server default
    r = push_control.reset_config(db, None, "depth_cap", actor="test")
    if "error" not in r:
        fail(f"expected error on reset of server default, got {r}")
    ok(f"server-default reset rejected: {r['error']}")

    # ── 10. Alert ack lifecycle
    alert_id = push_control.write_alert(
        db=db,
        agent_instance="_pc_test_agent_",
        agent_project="junto",
        trigger="hard_ceiling",
        prior_hour_message_count=100,
        window_start=datetime.now(timezone.utc) - timedelta(seconds=300),
        window_end=datetime.now(timezone.utc),
        recipient_set=["_pc_test_peer_@junto"],
        shape="varied",
        shape_explainer="test",
        sample_messages=[],
        peer_notice_inserted=False,
    )
    if not alert_id:
        fail("alert write failed")
    ok(f"alert written: {alert_id}")

    # Verify unack list contains it
    alerts = push_control.list_alerts(db, unacknowledged_only=True, limit=10)
    if not any(a["_id"] == alert_id for a in alerts):
        fail(f"unack list missing alert {alert_id}")
    ok("unack list includes the new alert")

    # Ack it
    r = push_control.acknowledge_alert(db, alert_id, actor="test_validator")
    if "ok" not in r or not r["ok"]:
        fail(f"ack failed: {r}")
    ok(f"alert acknowledged: {r}")

    # Verify unack list no longer contains it
    alerts = push_control.list_alerts(db, unacknowledged_only=True, limit=10)
    if any(a["_id"] == alert_id for a in alerts):
        fail(f"unack list still contains acked alert {alert_id}")
    ok("unack list excludes acked alert")

    # Double-ack should error
    r = push_control.acknowledge_alert(db, alert_id, actor="test_validator")
    if "error" not in r:
        fail(f"expected error on double-ack, got {r}")
    ok(f"double-ack rejected: {r['error']}")

    # ── 11. Suspend / unsuspend
    db.registered_agents.update_one(
        {"project": "junto", "name": "_pc_test_agent_"},
        {"$set": {"project": "junto", "name": "_pc_test_agent_", "tier": "agent", "suspended": False}},
        upsert=True,
    )
    push_control.set_agent_suspended(db, "junto", "_pc_test_agent_", True, "test", actor="test")
    if not push_control.is_agent_suspended(db, "junto", "_pc_test_agent_"):
        fail("expected agent suspended after set")
    ok("agent suspended via set_agent_suspended")

    push_control.set_agent_suspended(db, "junto", "_pc_test_agent_", False, "test cleanup", actor="test")
    if push_control.is_agent_suspended(db, "junto", "_pc_test_agent_"):
        fail("expected agent NOT suspended after unset")
    ok("agent unsuspended via set_agent_suspended(False)")

    print("\n=== Cleanup ===")
    db.push_control_config.delete_many({"scope": {"$in": ["_pc_test_project_", push_control.DEFAULT_SCOPE]}})
    db.alerts.delete_one({"_id": alert_id})
    db.registered_agents.delete_one({"project": "junto", "name": "_pc_test_agent_"})
    ok("cleanup complete")

    print("\n=== ALL PHASE 1f VALIDATIONS PASSED ===")


if __name__ == "__main__":
    main()
