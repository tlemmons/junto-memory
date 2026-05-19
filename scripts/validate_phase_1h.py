"""Phase 1h — synthetic load harness exercising the full push-control state machine.

Drives soft + hard limits in a controlled test scope so the live `junto` bus
is untouched. Uses a synthetic project name ("_pc_load_test_") with a small
ceiling so we hit hard-trip in ~15 sends instead of 100.

Exercises:
  1. Below budget: no suppression
  2. Budget trip: push_suppressed=true reason=push_budget
  3. Hard ceiling trip: handle_hard_trip fires (alert + notice + suspension)
  4. Post-suspension: send → reason=agent_suspended
  5. Unsuspend recovery path

Calls push_control directly (bypassing memory_send_message) so we don't
need MCP auth or a registered sender identity on the live junto project.

Usage:
    docker exec -e PYTHONPATH=/app/src mcp-rag-arch python /tmp/validate_phase_1h.py
"""

import sys
from datetime import datetime, timezone

from shared_memory import push_control
from shared_memory.clients import get_mongo


TEST_PROJECT = "_pc_load_test_"
TEST_SENDER = "_pc_load_sender_"
TEST_PEER = "_pc_load_peer_"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}")


def main() -> None:
    db = get_mongo()
    if db is None:
        fail("MongoDB unavailable")

    print("=== Phase 1h synthetic load harness ===\n")

    # ── Pre-clean
    db.push_control_config.delete_many({"scope": TEST_PROJECT})
    db.alerts.delete_many({"agent_project": TEST_PROJECT})
    db.registered_agents.delete_many({"project": TEST_PROJECT})
    db.messages.delete_many({"from_project": TEST_PROJECT})
    db.messages.delete_many({"to_project": TEST_PROJECT})
    push_control.reset_emission_counters()

    # ── Set a small ceiling on the test project so we don't have to send 100
    push_control.set_config_value(db, TEST_PROJECT, "depth_cap", 20, actor="loadtest")
    push_control.set_config_value(db, TEST_PROJECT, "push_budget", 8, actor="loadtest")
    push_control.set_config_value(db, TEST_PROJECT, "hard_ceiling", 15, actor="loadtest")
    cfg = push_control.get_effective_config(db, TEST_PROJECT)
    if cfg["push_budget"] != 8 or cfg["hard_ceiling"] != 15:
        fail(f"config not applied: {cfg}")
    ok(f"test config: depth_cap={cfg['depth_cap']} push_budget={cfg['push_budget']} hard_ceiling={cfg['hard_ceiling']}")

    # Register the sender so set_agent_suspended has a doc.
    db.registered_agents.update_one(
        {"project": TEST_PROJECT, "name": TEST_SENDER},
        {"$set": {"project": TEST_PROJECT, "name": TEST_SENDER, "tier": "agent", "suspended": False}},
        upsert=True,
    )

    # ── Send 8: all within budget
    print("\n--- Sending 8 messages (within push_budget=8) ---")
    for i in range(1, 9):
        eval_result = push_control.evaluate_send(
            db=db,
            sender_instance=TEST_SENDER,
            sender_project=TEST_PROJECT,
            chain_depth=0,
            recipient_instance=TEST_PEER,
            recipient_project=TEST_PROJECT,
            recency_bypass=False,
        )
        if eval_result["suppress"]:
            fail(f"send #{i}: expected no suppression, got {eval_result}")
        # Persist a fake message doc for the incident-window read later
        db.messages.insert_one({
            "_id": f"msg_loadtest_{i:02d}",
            "from_instance": TEST_SENDER,
            "from_project": TEST_PROJECT,
            "to_instance": TEST_PEER,
            "to_project": TEST_PROJECT,
            "message": f"load test send #{i}",
            "chain_depth": 0,
            "status": "pending",
            "push_suppressed": eval_result["suppress"],
            "emission_count": eval_result["emission_count"],
            "created_at": datetime.now(timezone.utc),
        })
    final = push_control.get_emission_count(TEST_SENDER, TEST_PROJECT)
    if final != 8:
        fail(f"after 8 sends, counter should be 8, got {final}")
    ok(f"8 sends: all push_suppressed=false, emission_count=8")

    # ── Send 9-14: above push_budget (8), below hard_ceiling (15) → suppressed soft
    print("\n--- Sending 9-14 (above budget=8, below ceiling=15) ---")
    soft_trip_count = 0
    for i in range(9, 15):
        eval_result = push_control.evaluate_send(
            db=db,
            sender_instance=TEST_SENDER,
            sender_project=TEST_PROJECT,
            chain_depth=0,
            recipient_instance=TEST_PEER,
            recipient_project=TEST_PROJECT,
            recency_bypass=False,
        )
        if not eval_result["suppress"]:
            fail(f"send #{i}: expected suppress=true, got {eval_result}")
        if eval_result["reason"] != "push_budget":
            fail(f"send #{i}: expected reason=push_budget, got {eval_result['reason']}")
        if eval_result["hard_trip"]:
            fail(f"send #{i}: hard_trip should be False (we're under ceiling)")
        soft_trip_count += 1
        db.messages.insert_one({
            "_id": f"msg_loadtest_{i:02d}",
            "from_instance": TEST_SENDER,
            "from_project": TEST_PROJECT,
            "to_instance": TEST_PEER,
            "to_project": TEST_PROJECT,
            "message": f"load test send #{i} (suppressed)",
            "chain_depth": 0,
            "status": "pending",
            "push_suppressed": True,
            "push_suppress_reason": "push_budget",
            "emission_count": eval_result["emission_count"],
            "created_at": datetime.now(timezone.utc),
        })
    ok(f"6 sends 9-14: all suppressed with reason=push_budget, hard_trip=false")

    # ── Send 15: AT hard_ceiling → first hard trip
    print("\n--- Sending #15 (= hard_ceiling=15, should be hard_trip) ---")
    eval_result = push_control.evaluate_send(
        db=db,
        sender_instance=TEST_SENDER,
        sender_project=TEST_PROJECT,
        chain_depth=0,
        recipient_instance=TEST_PEER,
        recipient_project=TEST_PROJECT,
        recency_bypass=False,
    )
    if not eval_result["suppress"]:
        fail(f"send #15: expected suppress=true, got {eval_result}")
    if eval_result["reason"] != "hard_ceiling":
        fail(f"send #15: expected reason=hard_ceiling, got {eval_result['reason']}")
    if not eval_result["hard_trip"]:
        fail(f"send #15: expected hard_trip=true, got {eval_result}")
    ok(f"send #15: hard_trip=True, reason=hard_ceiling, count={eval_result['emission_count']}")

    # Execute the hard-trip orchestration (would normally be triggered by
    # messaging.py on hard_trip=True)
    trip_result = push_control.handle_hard_trip(
        db=db,
        sender_instance=TEST_SENDER,
        sender_project=TEST_PROJECT,
        emission_count=eval_result["emission_count"],
        trigger="hard_ceiling",
        trip_time=datetime.now(timezone.utc),
        cfg=cfg,
    )
    if not trip_result.get("alert_id"):
        fail(f"handle_hard_trip returned no alert_id: {trip_result}")
    if not trip_result.get("suspended"):
        fail(f"handle_hard_trip didn't suspend agent: {trip_result}")
    ok(f"hard-trip orchestrated: alert={trip_result['alert_id']} notices={len(trip_result['notice_ids'])} suspended={trip_result['suspended']}")

    # ── Verify agent.suspended
    if not push_control.is_agent_suspended(db, TEST_PROJECT, TEST_SENDER):
        fail("agent not flagged suspended after hard trip")
    ok(f"agent.suspended=True confirmed")

    # ── Send #16: agent is now suspended → reason=agent_suspended
    eval_result = push_control.evaluate_send(
        db=db,
        sender_instance=TEST_SENDER,
        sender_project=TEST_PROJECT,
        chain_depth=0,
        recipient_instance=TEST_PEER,
        recipient_project=TEST_PROJECT,
        recency_bypass=False,
    )
    if not eval_result["suppress"] or eval_result["reason"] != "agent_suspended":
        fail(f"post-suspension send: expected reason=agent_suspended, got {eval_result}")
    ok(f"post-suspension send: reason=agent_suspended")

    # ── Verify alert content
    alert = db.alerts.find_one({"_id": trip_result["alert_id"]})
    if alert["agent_instance"] != TEST_SENDER:
        fail(f"alert.agent_instance mismatch: {alert['agent_instance']}")
    if alert["agent_project"] != TEST_PROJECT:
        fail(f"alert.agent_project mismatch: {alert['agent_project']}")
    if alert["trigger"] != "hard_ceiling":
        fail(f"alert.trigger mismatch: {alert['trigger']}")
    if alert["acknowledged"]:
        fail(f"alert.acknowledged should be False at insert")
    if alert["peer_notice_inserted"] is not True:
        fail(f"alert.peer_notice_inserted should be True (peer is in recipient_set)")
    if TEST_PEER + "@" + TEST_PROJECT not in alert["recipient_set"]:
        fail(f"alert.recipient_set missing peer: {alert['recipient_set']}")
    ok(f"alert content correct: trigger={alert['trigger']} shape={alert['shape']} peer_inserted={alert['peer_notice_inserted']}")

    # ── Verify recovery notices in BOTH endpoints' inboxes
    notice_ids = trip_result["notice_ids"]
    sender_notices = list(db.messages.find({
        "_id": {"$in": notice_ids},
        "to_instance": TEST_SENDER,
    }))
    peer_notices = list(db.messages.find({
        "_id": {"$in": notice_ids},
        "to_instance": TEST_PEER,
    }))
    if len(sender_notices) != 1 or len(peer_notices) != 1:
        fail(f"recovery notice counts wrong: sender={len(sender_notices)} peer={len(peer_notices)} (expected 1+1)")
    for n in sender_notices + peer_notices:
        if not n.get("is_system_notice"):
            fail(f"recovery notice {n['_id']} missing is_system_notice flag")
        if not n.get("push_suppressed"):
            fail(f"recovery notice {n['_id']} missing push_suppressed=True (must be non-pushing)")
        if n.get("from_instance") != "system":
            fail(f"recovery notice {n['_id']} from_instance != 'system'")
    ok(f"recovery notices: 1 to sender + 1 to peer, all is_system_notice=True push_suppressed=True from=system")

    # ── Verify ack lifecycle
    ack_result = push_control.acknowledge_alert(db, trip_result["alert_id"], actor="loadtest_validator")
    if "ok" not in ack_result:
        fail(f"ack failed: {ack_result}")
    # Confirm not in unack list
    unack = push_control.list_alerts(db, unacknowledged_only=True, project=TEST_PROJECT)
    if any(a["_id"] == trip_result["alert_id"] for a in unack):
        fail(f"acked alert still in unack list")
    ok(f"alert acked; not in unack list")

    # ── Verify unsuspend recovery
    push_control.set_agent_suspended(
        db, TEST_PROJECT, TEST_SENDER, False,
        reason="loadtest recovery", actor="loadtest_validator",
    )
    push_control.reset_emission_counters()  # fresh hour, fresh count
    eval_result = push_control.evaluate_send(
        db=db,
        sender_instance=TEST_SENDER,
        sender_project=TEST_PROJECT,
        chain_depth=0,
        recipient_instance=TEST_PEER,
        recipient_project=TEST_PROJECT,
        recency_bypass=False,
    )
    if eval_result["suppress"]:
        fail(f"after unsuspend + counter reset, send should NOT suppress: {eval_result}")
    ok(f"unsuspended + counter reset: send → suppress=false")

    print("\n=== Cleanup ===")
    db.push_control_config.delete_many({"scope": TEST_PROJECT})
    db.alerts.delete_many({"agent_project": TEST_PROJECT})
    db.registered_agents.delete_many({"project": TEST_PROJECT})
    db.messages.delete_many({"from_project": TEST_PROJECT})
    db.messages.delete_many({"to_project": TEST_PROJECT})
    push_control.reset_emission_counters()
    ok("cleanup complete")

    print("\n=== ALL PHASE 1h LOAD-HARNESS ASSERTIONS PASSED ===")


if __name__ == "__main__":
    main()
