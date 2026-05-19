"""Synthetic validation for Phase 1d/1e/1g — hard-ceiling escalation path.

Invokes push_control.handle_hard_trip with synthetic data so we exercise the
full orchestration (alert write, recovery notice insertion, agent suspension,
webhook scheduling) without actually flooding messages on the live bus or
suspending any real agent identity.

Usage (inside container):
    docker exec mcp-rag-arch python -m scripts.validate_phase_1d

Cleans up after itself: removes the test agent registration, test alert,
test recovery notices, and unsets any push_control_config override created
along the way.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from shared_memory import push_control
from shared_memory.clients import get_mongo

TEST_PROJECT = "junto"
TEST_SENDER = "_pc_test_sender_"
TEST_PEER_1 = "_pc_test_peer_1_"
TEST_PEER_2 = "_pc_test_peer_2_"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}")


async def main() -> None:
    db = get_mongo()
    if db is None:
        fail("MongoDB unavailable — cannot validate")

    print("=== Phase 1d/1e/1g validation ===")
    print(f"  test sender: {TEST_SENDER}@{TEST_PROJECT}")
    print(f"  test peers : {TEST_PEER_1}, {TEST_PEER_2}")
    print()

    # ── Pre-clean: drop any leftover test docs from a prior interrupted run.
    db.messages.delete_many({"_id": {"$regex": "^msg_pctest_"}})
    db.messages.delete_many({
        "system_notice_kind": "push_control.recovery",
        "to_instance": {"$regex": "^_pc_test_"},
    })
    db.alerts.delete_many({"agent_instance": TEST_SENDER})
    db.registered_agents.delete_many(
        {"name": {"$in": [TEST_SENDER, TEST_PEER_1, TEST_PEER_2]}}
    )

    # ── Setup: register the test agent so set_agent_suspended has a doc to update.
    db.registered_agents.update_one(
        {"project": TEST_PROJECT, "name": TEST_SENDER},
        {"$set": {
            "project": TEST_PROJECT, "name": TEST_SENDER,
            "tier": "agent", "role_description": "synthetic test sender",
            "suspended": False,
        }},
        upsert=True,
    )

    # Seed a fake message history so compute_incident_window has samples to find.
    now = datetime.now(timezone.utc)
    fake_msgs = []
    for i in range(8):
        peer = TEST_PEER_1 if i % 2 == 0 else TEST_PEER_2
        fake_msgs.append({
            "_id": f"msg_pctest_{i:02d}",
            "from_instance": TEST_SENDER,
            "from_project": TEST_PROJECT,
            "to_instance": peer,
            "to_project": TEST_PROJECT,
            "message": f"fake spiral message body #{i} — varied content to test classifier",
            "chain_depth": 0,
            "status": "pending",
            "push_suppressed": False,
            "created_at": now - timedelta(seconds=60 - i * 5),
        })
    if fake_msgs:
        db.messages.insert_many(fake_msgs)

    # ── 1. Invoke the orchestrator
    cfg = push_control.get_effective_config(db, TEST_PROJECT)
    print(f"  effective config: depth_cap={cfg['depth_cap']} push_budget={cfg['push_budget']} hard_ceiling={cfg['hard_ceiling']} webhook_url={cfg['webhook_url']}")

    result = push_control.handle_hard_trip(
        db=db,
        sender_instance=TEST_SENDER,
        sender_project=TEST_PROJECT,
        emission_count=cfg["hard_ceiling"],
        trigger="hard_ceiling",
        trip_time=now,
        cfg=cfg,
    )

    print(f"  handle_hard_trip result: {result}")
    print()

    # ── 2. Assertions
    alert_id = result.get("alert_id")
    if not alert_id:
        fail("no alert_id returned")
    ok(f"alert_id returned: {alert_id}")

    alert = db.alerts.find_one({"_id": alert_id})
    if not alert:
        fail(f"alert {alert_id} not found in Mongo")
    ok(f"alert persisted; trigger={alert['trigger']} count={alert['prior_hour_message_count']}")
    if alert["agent_instance"] != TEST_SENDER:
        fail(f"alert.agent_instance mismatch: {alert['agent_instance']}")
    if alert["shape"] not in ("identical_repeating", "varied"):
        fail(f"alert.shape invalid: {alert['shape']}")
    ok(f"alert shape={alert['shape']} explainer={alert['shape_explainer']!r}")
    ok(f"alert recipient_set={alert['recipient_set']}")
    if not alert["peer_notice_inserted"]:
        fail("peer_notice_inserted is False (notices should have been inserted)")
    ok(f"alert peer_notice_inserted={alert['peer_notice_inserted']}")
    if alert["acknowledged"]:
        fail("alert.acknowledged should be False at insert time")
    ok("alert.acknowledged=False (correct — operator must ack)")

    # ── 3. Suspension check
    sender_doc = db.registered_agents.find_one({"project": TEST_PROJECT, "name": TEST_SENDER})
    if not sender_doc:
        fail("test sender disappeared from registered_agents")
    if not sender_doc.get("suspended"):
        fail("test sender not flagged suspended=True")
    ok(f"sender.suspended=True reason={sender_doc.get('suspended_reason')!r}")

    # ── 4. Recovery notice insertion
    notice_ids = result.get("notice_ids", [])
    if not notice_ids:
        fail("no recovery notices inserted")
    # Expect 1 self + 2 peers (TEST_PEER_1, TEST_PEER_2) = 3 total
    if len(notice_ids) != 3:
        fail(f"expected 3 recovery notices, got {len(notice_ids)}")
    ok(f"recovery notices inserted: {len(notice_ids)}")
    for nid in notice_ids:
        notice = db.messages.find_one({"_id": nid})
        if not notice:
            fail(f"recovery notice {nid} not found")
        if not notice.get("is_system_notice"):
            fail(f"recovery notice {nid} missing is_system_notice=True")
        if not notice.get("push_suppressed"):
            fail(f"recovery notice {nid} missing push_suppressed=True (must be non-pushing)")
        if notice.get("from_instance") != "system":
            fail(f"recovery notice {nid} from_instance={notice.get('from_instance')!r} (expected 'system')")
    ok("all recovery notices have is_system_notice=True, push_suppressed=True, from='system'")

    # ── 5. Notice positioning — created_at must be before any incident message.
    # Normalize both sides to UTC-aware datetimes since PyMongo may strip tzinfo
    # on roundtrip depending on driver config.
    def _aware(d):
        if isinstance(d, datetime) and d.tzinfo is None:
            return d.replace(tzinfo=timezone.utc)
        return d
    earliest_incident = _aware(min(m["created_at"] for m in fake_msgs))
    for nid in notice_ids:
        notice = db.messages.find_one({"_id": nid})
        notice_ts = _aware(notice["created_at"])
        if notice_ts >= earliest_incident:
            fail(f"notice {nid} created_at={notice_ts} is NOT before earliest incident {earliest_incident}")
    ok(f"all notices positioned before earliest incident message ({earliest_incident.isoformat()})")

    # ── 6. Audit log
    audit_entries = list(db.audit_log.find(
        {"event_type": {"$regex": "^push_control\\."}, "timestamp": {"$gte": now - timedelta(seconds=30)}}
    ))
    event_types = sorted(set(e["event_type"] for e in audit_entries))
    print(f"  audit events fired: {event_types}")
    for expected in ("push_control.alert_fired", "push_control.agent_suspended", "push_control.recovery_notice"):
        if expected not in event_types:
            fail(f"missing audit event: {expected}")
    ok("all expected audit events fired")

    # ── 7. Webhook scheduling — None since no webhook_url is configured
    if cfg["webhook_url"] is None:
        if result["webhook_scheduled"]:
            fail("webhook_scheduled=True but no webhook_url configured")
        ok("webhook_scheduled=False (correct — no URL configured)")
    else:
        # If configured, should have scheduled
        if not result["webhook_scheduled"]:
            fail(f"webhook_url={cfg['webhook_url']} but webhook_scheduled=False")
        ok(f"webhook scheduled to {cfg['webhook_url']}")

    # ── 8. Verify suspension blocks future pushes
    pc_eval = push_control.evaluate_send(
        db=db,
        sender_instance=TEST_SENDER,
        sender_project=TEST_PROJECT,
        chain_depth=0,
        recipient_instance=TEST_PEER_1,
        recipient_project=TEST_PROJECT,
        recency_bypass=False,
    )
    if not pc_eval["suppress"]:
        fail(f"suspended sender's next send should suppress, got {pc_eval}")
    if pc_eval["reason"] != "agent_suspended":
        fail(f"expected reason='agent_suspended', got {pc_eval['reason']!r}")
    ok("post-suspension send → push_suppressed=True reason='agent_suspended'")

    # ── 9. Verify recipient-suspension blocks
    push_control.set_agent_suspended(db, TEST_PROJECT, TEST_SENDER, False, "test cleanup")

    # Register the peer so set_agent_suspended has a doc to update (the
    # production path only sees registered recipients — unregistered ones
    # are rejected earlier in memory_send_message).
    db.registered_agents.update_one(
        {"project": TEST_PROJECT, "name": TEST_PEER_1},
        {"$set": {"project": TEST_PROJECT, "name": TEST_PEER_1, "tier": "agent", "suspended": False}},
        upsert=True,
    )
    push_control.set_agent_suspended(db, TEST_PROJECT, TEST_PEER_1, True, "synthetic test", actor="test")

    pc_eval2 = push_control.evaluate_send(
        db=db,
        sender_instance="other_test_agent_",  # an unrelated sender
        sender_project=TEST_PROJECT,
        chain_depth=0,
        recipient_instance=TEST_PEER_1,
        recipient_project=TEST_PROJECT,
        recency_bypass=False,
    )
    if not pc_eval2["suppress"] or pc_eval2["reason"] != "recipient_suspended":
        fail(f"recipient-suspended path failed: {pc_eval2}")
    ok("recipient_suspended → push_suppressed=True reason='recipient_suspended'")

    print()
    print("=== Cleanup ===")

    # Reset suspension flags
    push_control.set_agent_suspended(db, TEST_PROJECT, TEST_SENDER, False, "test cleanup")
    push_control.set_agent_suspended(db, TEST_PROJECT, TEST_PEER_1, False, "test cleanup")

    # Remove test docs
    db.registered_agents.delete_one({"project": TEST_PROJECT, "name": TEST_SENDER})
    db.registered_agents.delete_one({"project": TEST_PROJECT, "name": TEST_PEER_1})
    db.registered_agents.delete_one({"project": TEST_PROJECT, "name": TEST_PEER_2})
    db.messages.delete_many({"_id": {"$regex": "^msg_pctest_"}})
    db.messages.delete_many({"_id": {"$in": notice_ids}})
    db.alerts.delete_one({"_id": alert_id})
    # Trim test audit entries to keep audit log clean
    db.audit_log.delete_many({
        "event_type": {"$regex": "^push_control\\."},
        "$or": [
            {"actor": TEST_SENDER},
            {"details.agent": {"$regex": TEST_SENDER}},
            {"details.suspended_agent": {"$regex": TEST_SENDER}},
        ],
        "timestamp": {"$gte": now - timedelta(seconds=120)},
    })

    # Reset in-process counter that handle_hard_trip didn't increment
    # but which we touched via evaluate_send above.
    push_control.reset_emission_counters()

    ok("cleanup complete")
    print()
    print("=== ALL PHASE 1d/1e/1g VALIDATIONS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
