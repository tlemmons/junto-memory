"""Integration tests for memory_sync_push against real Mongo + Chroma.

Pinned with @pytest.mark.integration — skipped automatically when the
local chromadb/mongodb services aren't reachable. The CI suite + dev
workflow run them when `docker compose up -d chromadb mongodb` is up.

Strategy: synthesize op_log entries with a test-namespaced origin
(`test-peer-<run_id>`), push through `_push_ops`, then assert against
the real source-store state. Cleanup at the end removes:
  - test-collection rows from Chroma (project-scoped, isolated)
  - op_log rows with origin == test-peer-<run_id>
  - agent_state_owner rows from the test project

This verifies the unit-test contract (embedding bit-equality, metadata
fidelity, op_log preservation) holds against the live backends — the
unit fakes can't catch driver-level shape mismatches.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import pytest

from shared_memory import op_log
from shared_memory.tools import sync as sync_tool


MONGO_HOST = os.environ.get("MONGO_HOST", "localhost")
MONGO_PORT = int(os.environ.get("MONGO_PORT", "27019"))
CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8001"))


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def services():
    if not _reachable(MONGO_HOST, MONGO_PORT):
        pytest.skip(f"mongodb {MONGO_HOST}:{MONGO_PORT} not reachable")
    if not _reachable(CHROMA_HOST, CHROMA_PORT):
        pytest.skip(f"chromadb {CHROMA_HOST}:{CHROMA_PORT} not reachable")
    yield


@pytest.fixture
async def real_clients(services):
    """Return (db, chroma, cleanup) tuple with isolated test scope."""
    import chromadb
    from pymongo import MongoClient

    # Mongo: same creds path as production clients.py, but the test owns
    # cleanup so we don't litter the real collections.
    mongo_user = os.environ.get("MONGO_USERNAME", "mcp_orch")
    mongo_pass = os.environ.get("MONGO_PASSWORD", "")
    if not mongo_pass:
        pytest.skip("MONGO_PASSWORD not set in environment")
    # directConnection=true bypasses replica-set member discovery so the
    # test can talk to the rs0 single node from outside the docker network
    # (members advertise as `mongodb:27017` which the host can't resolve).
    mongo_uri = (
        f"mongodb://{mongo_user}:{mongo_pass}@{MONGO_HOST}:{MONGO_PORT}/"
        f"shared_memory?authSource=admin&directConnection=true"
    )
    mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    db = mongo["shared_memory"]

    # Ensure agent_state_owner exists with the unique index.
    op_log.ensure_op_log_indexes(db)

    # Chroma async client.
    chroma = await chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)

    test_origin = f"test-peer-{uuid.uuid4().hex[:8]}"
    test_project = f"test_sync_{uuid.uuid4().hex[:8]}"

    state = {
        "db": db,
        "chroma": chroma,
        "test_origin": test_origin,
        "test_project": test_project,
        "test_collection": f"proj_{test_project}",
    }

    yield state

    # Cleanup: drop op_log entries with our test origin, the test agent
    # state owner row(s), and the test project's Chroma collection.
    try:
        db[op_log.OPLOG_COLLECTION].delete_many({"origin": test_origin})
    except Exception:
        pass
    try:
        db[op_log.AGENT_STATE_OWNER_COLLECTION].delete_many(
            {"project": test_project}
        )
    except Exception:
        pass
    try:
        await chroma.delete_collection(name=state["test_collection"])
    except Exception:
        pass
    mongo.close()


def _build_op(test_origin, seq, *, op_type, ref, payload, intent_id=None):
    return {
        "_id": f"op_test_{uuid.uuid4().hex[:12]}",
        "seq": seq,
        "ts": "2026-05-15T20:00:00+00:00",
        "origin": test_origin,
        "intent_id": intent_id,
        "actor": {"agent": "test-peer", "project": "test", "session_id": "t"},
        "op_type": op_type,
        "ref": ref,
        "payload": payload,
        "schema_version": 1,
    }


async def _push(db, chroma, ops, *, origin_server_id="central"):
    return await sync_tool._push_ops(
        db=db, chroma=chroma, ops=ops, origin_server_id=origin_server_id
    )


@pytest.mark.asyncio
async def test_integration_learning_recorded_round_trip(real_clients):
    """A real learning.recorded op materializes to a real Chroma row
    with the embedding bit-equal to what we sent."""
    s = real_clients
    test_coll_name = s["test_collection"]
    doc_id = f"learning_{uuid.uuid4().hex[:16]}"

    # Build a 384-d embedding the materializer should apply verbatim.
    # Use a recognizable pattern so we can assert it back unchanged.
    embedding = [round(i * 0.001, 6) for i in range(384)]

    op = _build_op(
        s["test_origin"], 1,
        op_type="learning.recorded",
        ref={"collection": test_coll_name, "doc_id": doc_id},
        payload={
            "title": "integration smoke",
            "details": "round-trip a learning op through memory_sync_push",
            "tags": ["test", "integration"],
            "created": "2026-05-15T20:00:00+00:00",
            "embedding": embedding,
        },
    )

    result = await _push(s["db"], s["chroma"], [op])
    assert result["applied_count"] == 1, result
    assert result["results"][0]["disposition"] == "applied"

    # Read back from Chroma — embedding must be bit-equal.
    coll = await s["chroma"].get_or_create_collection(name=test_coll_name)
    got = await coll.get(ids=[doc_id], include=["metadatas", "embeddings"])
    assert got["ids"] == [doc_id]
    fetched = got["embeddings"][0]
    if hasattr(fetched, "tolist"):
        fetched = fetched.tolist()
    assert fetched == embedding, "embedding drift on apply"
    assert got["metadatas"][0]["title"] == "integration smoke"
    assert got["metadatas"][0]["type"] == "learning"

    # Op_log preserves origin/seq/intent_id/_id.
    stored = s["db"][op_log.OPLOG_COLLECTION].find_one(
        {"origin": s["test_origin"], "seq": 1}
    )
    assert stored is not None
    assert stored["_id"] == op["_id"]

    # Re-push is deduped_seq.
    re_result = await _push(s["db"], s["chroma"], [op])
    assert re_result["results"][0]["disposition"] == "deduped_seq"


@pytest.mark.asyncio
async def test_integration_state_spec_origin_owner_first_push(real_clients):
    """A first state-spec push registers the peer's origin in agent_state_owner."""
    s = real_clients

    op = _build_op(
        s["test_origin"], 1,
        op_type="spec.defined",
        ref={
            "collection": s["test_collection"],
            "doc_id": "spec_state_remote_test_agent",
        },
        payload={
            "spec_name": "state:remote-test-agent",
            "version": "1.0.0",
            "previous_version": None,
            "owner": "remote-test-agent",
            "spec_type": "agent_state",
            "content": "state body",
            "tags": [],
            "json_schema": None,
            "project": s["test_project"],
            "updated_at": "2026-05-15T20:00:00+00:00",
            "embedding": [0.001] * 384,
            "origin_server_id": s["test_origin"],
        },
    )
    result = await _push(s["db"], s["chroma"], [op])
    assert result["results"][0]["disposition"] == "applied", result

    owner_row = s["db"][op_log.AGENT_STATE_OWNER_COLLECTION].find_one(
        {"project": s["test_project"], "agent": "remote-test-agent"}
    )
    assert owner_row is not None
    assert owner_row["registered_origin"] == s["test_origin"]


@pytest.mark.asyncio
async def test_integration_state_spec_mismatched_origin_rejected(real_clients):
    """After origin A claims a state spec, origin B's push is rejected."""
    s = real_clients
    other_origin = f"test-other-{uuid.uuid4().hex[:8]}"

    op_a = _build_op(
        s["test_origin"], 1,
        op_type="spec.defined",
        ref={
            "collection": s["test_collection"],
            "doc_id": "spec_state_contested_agent",
        },
        payload={
            "spec_name": "state:contested-agent",
            "version": "1.0.0",
            "previous_version": None,
            "owner": "contested-agent",
            "spec_type": "agent_state",
            "content": "first body",
            "tags": [],
            "json_schema": None,
            "project": s["test_project"],
            "updated_at": "2026-05-15T20:00:00+00:00",
            "embedding": [0.001] * 384,
            "origin_server_id": s["test_origin"],
        },
    )
    op_b = _build_op(
        other_origin, 1,
        op_type="spec.defined",
        ref={
            "collection": s["test_collection"],
            "doc_id": "spec_state_contested_agent",
        },
        payload={
            "spec_name": "state:contested-agent",
            "version": "1.0.0",
            "previous_version": None,
            "owner": "contested-agent",
            "spec_type": "agent_state",
            "content": "rival body",
            "tags": [],
            "json_schema": None,
            "project": s["test_project"],
            "updated_at": "2026-05-15T20:00:00+00:00",
            "embedding": [0.001] * 384,
            "origin_server_id": other_origin,
        },
    )

    r_a = await _push(s["db"], s["chroma"], [op_a])
    assert r_a["results"][0]["disposition"] == "applied"

    r_b = await _push(s["db"], s["chroma"], [op_b])
    assert r_b["results"][0]["disposition"] == "rejected_origin_owner"

    # Cleanup the other origin's ops too.
    s["db"][op_log.OPLOG_COLLECTION].delete_many({"origin": other_origin})


@pytest.mark.asyncio
async def test_integration_spec_updated_conflict_files_backlog(real_clients):
    """A spec.updated with mismatched previous_version files a backlog item."""
    s = real_clients

    # Land v1.0.0.
    op_def = _build_op(
        s["test_origin"], 1,
        op_type="spec.defined",
        ref={"collection": s["test_collection"], "doc_id": "spec_design_test"},
        payload={
            "spec_name": "design:test",
            "version": "1.0.0",
            "previous_version": None,
            "owner": "peer",
            "spec_type": "design",
            "content": "v1 body",
            "tags": [],
            "json_schema": None,
            "project": s["test_project"],
            "updated_at": "2026-05-15T20:00:00+00:00",
            "embedding": [0.002] * 384,
        },
    )
    await _push(s["db"], s["chroma"], [op_def])

    # Now locally override to v2.0.0 (simulate divergence).
    coll = await s["chroma"].get_or_create_collection(name=s["test_collection"])
    existing = await coll.get(ids=["spec_design_test"], include=["metadatas"])
    meta = dict(existing["metadatas"][0])
    meta["spec_version"] = "2.0.0"
    await coll.update(ids=["spec_design_test"], metadatas=[meta])

    # Push the conflicting fast-forward from v1.0.0 → v1.1.0.
    op_upd = _build_op(
        s["test_origin"], 2,
        op_type="spec.updated",
        ref={"collection": s["test_collection"], "doc_id": "spec_design_test"},
        payload={
            "spec_name": "design:test",
            "version": "1.1.0",
            "previous_version": "1.0.0",
            "owner": "peer",
            "spec_type": "design",
            "content": "v1.1 body",
            "tags": [],
            "json_schema": None,
            "project": s["test_project"],
            "updated_at": "2026-05-15T20:00:01+00:00",
            "embedding": [0.003] * 384,
        },
    )
    r = await _push(s["db"], s["chroma"], [op_upd])
    assert r["results"][0]["disposition"] == "conflict"
    # Receiver kept v2.0.0.
    after = await coll.get(ids=["spec_design_test"], include=["metadatas"])
    assert after["metadatas"][0]["spec_version"] == "2.0.0"

    # Backlog item filed in the test project's collection (auto-filed
    # backlog uses the spec's project for routing).
    proj_coll = await s["chroma"].get_or_create_collection(name=s["test_collection"])
    proj_rows = await proj_coll.get(include=["metadatas"])
    titles = [m.get("title") or "" for m in proj_rows["metadatas"]]
    assert any("Spec conflict" in t and "design:test" in t for t in titles), titles
