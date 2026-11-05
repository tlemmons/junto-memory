"""interface:backlog-ideas-comments-v0: `idea` status, comments, editable tags, /backlog routes."""

import json
import os
import uuid

import pytest

from shared_memory import backlog_comments as bc
from shared_memory.state import active_sessions
from shared_memory.tools import backlog as bt


class _FakeCollection:
    def __init__(self, name, docs=None):
        self.name = name
        self.docs = dict(docs or {})  # id -> (document, metadata)

    async def get(self, ids=None, where=None, include=None, **kw):
        keys = [i for i in ids if i in self.docs] if ids is not None else [
            i for i, (_, m) in self.docs.items() if not where or all(m.get(k) == v for k, v in where.items())]
        return {"ids": keys, "documents": [self.docs[i][0] for i in keys],
                "metadatas": [dict(self.docs[i][1]) for i in keys]}

    async def add(self, ids=None, documents=None, metadatas=None):
        for i, d, m in zip(ids, documents, metadatas):
            self.docs.setdefault(i, (d, m))

    async def update(self, ids=None, documents=None, metadatas=None):
        for idx, i in enumerate(ids or []):
            doc = documents[idx] if documents else self.docs[i][0]
            self.docs[i] = (doc, metadatas[idx])


@pytest.fixture
def db():
    """Throwaway database on the TEST mongo (JUNTO_TEST_MONGO_URL), never production."""
    url = os.environ.get("JUNTO_TEST_MONGO_URL")
    if not url:
        pytest.skip("JUNTO_TEST_MONGO_URL not set (use a throwaway mongo, not production)")
    from pymongo import MongoClient
    client = MongoClient(url, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("test mongo not reachable")
    name = f"bcmt_test_{uuid.uuid4().hex[:8]}"
    bc._indexed = False
    try:
        yield client[name]
    finally:
        client.drop_database(name)


@pytest.fixture
def env(monkeypatch, db):
    cols = {}
    ops = []
    active_sessions["s1"] = {"claude_instance": "pipeline", "project": "junto"}

    class _Chroma:
        async def list_collections(self):
            return list(cols.values())

    async def _chroma():
        return _Chroma()

    async def _proj(_c, project):
        return cols.setdefault(f"proj_{project}", _FakeCollection(f"proj_{project}"))

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(bt, "get_chroma", _chroma)
    monkeypatch.setattr(bt, "get_project_collection", _proj)
    monkeypatch.setattr(bt, "get_mongo", lambda: db)
    monkeypatch.setattr(bt, "emit_op_log_from_context", lambda **kw: ops.append(kw))
    monkeypatch.setattr(bt, "fetch_embedding_for_op_log", _none)
    yield {"cols": cols, "ops": ops, "db": db}
    active_sessions.pop("s1", None)


async def _add(title, status="open"):
    return json.loads(await bt.memory_add_backlog_item("s1", title, "body", project="junto", status=status))


async def _list(**kw):
    return json.loads(await bt.memory_list_backlog("s1", project="junto", **kw))


@pytest.mark.asyncio
async def test_idea_status_on_add_and_bad_status_refused(env):
    r = await _add("an idea", status="idea")
    assert r["status"] == "added" and r["backlog_status"] == "idea"
    bad = json.loads(await bt.memory_add_backlog_item("s1", "x", "y", project="junto", status="done"))
    assert "error" in bad


@pytest.mark.asyncio
async def test_list_hides_ideas_unless_asked(env):
    await _add("work")
    await _add("idea", status="idea")
    assert [i["title"] for i in (await _list())["items"]] == ["work"]
    assert sorted(i["title"] for i in (await _list(include_ideas=True))["items"]) == ["idea", "work"]
    assert [i["title"] for i in (await _list(status="idea"))["items"]] == ["idea"]


@pytest.mark.asyncio
async def test_comment_only_update_stores_comment_and_counts(env):
    item = await _add("t")
    r = json.loads(await bt.memory_update_backlog_item("s1", item["id"], comment="looks good"))
    assert r["comment_id"].startswith("bcmt_")
    rows = bc.list_comments(env["db"], item["id"])
    assert [(c["author"], c["author_kind"], c["body"]) for c in rows] == [("pipeline", "agent", "looks good")]
    listed = (await _list())["items"][0]
    assert listed["comment_count"] == 1


@pytest.mark.asyncio
async def test_no_comment_count_key_when_zero(env):
    await _add("t")
    assert "comment_count" not in (await _list())["items"][0]


@pytest.mark.asyncio
async def test_comment_is_redacted(env):
    item = await _add("t")
    secret = "sk-ant-" + "a" * 40
    await bt.memory_update_backlog_item("s1", item["id"], comment=f"key {secret}")
    body = bc.list_comments(env["db"], item["id"])[0]["body"]
    assert secret not in body and "[REDACTED:anthropic_key]" in body


@pytest.mark.asyncio
async def test_tags_replace_and_ride_the_op_log(env):
    item = await _add("t")
    r = json.loads(await bt.memory_update_backlog_item("s1", item["id"], tags=["a", "b", ""]))
    assert r["tags"] == ["a", "b"]
    meta = env["cols"]["proj_junto"].docs[item["id"]][1]
    assert json.loads(meta["tags"]) == ["a", "b"]
    upd = [o for o in env["ops"] if o["op_type"] == "backlog.updated"][-1]
    assert upd["payload"]["tags"] == ["a", "b"]


@pytest.mark.asyncio
async def test_human_session_comments_are_human(env):
    item = await _add("t")
    active_sessions["web"] = {"claude_instance": "tom", "project": "junto", "author_kind": "human"}
    try:
        await bt.memory_update_backlog_item("web", item["id"], comment="from the page")
    finally:
        active_sessions.pop("web", None)
    c = bc.list_comments(env["db"], item["id"])[0]
    assert (c["author"], c["author_kind"]) == ("tom", "human")


def test_add_comment_validates(db):
    with pytest.raises(ValueError):
        bc.add_comment(db, "backlog_x", "junto", "a", "agent", "   ")
    with pytest.raises(ValueError):
        bc.add_comment(db, "backlog_x", "junto", "a", "robot", "hi")
    assert bc.counts(db, []) == {}


# --- /backlog routes -----------------------------------------------------------

class _FakeMcp:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            self.routes[path] = fn
            return fn
        return deco


class _Req:
    def __init__(self, body=None, params=None):
        self._body = json.dumps(body or {}).encode()
        self.query_params = params or {}
        self.headers = type("H", (), {"raw": [(b"authorization", b"Bearer k")]})()

    async def body(self):
        return self._body


@pytest.fixture
def routes(monkeypatch):
    from shared_memory import backlog_ui as ui
    m = _FakeMcp()
    ui.register_backlog_routes(m)
    monkeypatch.setattr(ui, "_lan_only", lambda r: None)
    monkeypatch.setattr("shared_memory.auth.validate_api_key", lambda k: {"name": "tom", "role": "owner"})
    yield m.routes, ui
    for sid in [s for s in active_sessions if s.startswith("web_backlog")]:
        active_sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_write_routes_need_owner(routes, monkeypatch):
    r, ui = routes
    monkeypatch.setattr(ui, "_auth", lambda req: {"ok": False, "status": 403, "error": "owner role required"})
    resp = await r["/backlog/api/create"](_Req({"title": "x"}))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_comment_route_notifies_assignee(routes, monkeypatch, env):
    r, ui = routes
    monkeypatch.setattr(ui, "_auth", lambda req: {"ok": True, "name": "tom", "role": "owner"})
    item = await _add("needs eyes")
    await bt.memory_update_backlog_item("s1", item["id"], assigned_to="pipeline")
    sent = []

    async def fake_send(sid, **kw):
        sent.append((active_sessions[sid]["claude_instance"], kw))
        return json.dumps({"message_id": "msg_x"})
    from shared_memory.tools import messaging
    monkeypatch.setattr(messaging, "memory_send_message", fake_send)
    resp = await r["/backlog/api/comment"](_Req({"id": item["id"], "body": "what about X?", "notify": True}))
    out = json.loads(resp.body)
    assert resp.status_code == 200 and out["notified"]["to"] == "pipeline"
    who, kw = sent[0]
    assert who == "tom" and kw["to_instance"] == "pipeline" and kw["human_interacted"] is True
    assert item["id"] in kw["message"]
    assert bc.list_comments(env["db"], item["id"])[0]["author_kind"] == "human"


@pytest.mark.asyncio
async def test_create_route_makes_an_idea(routes, monkeypatch, env):
    r, ui = routes
    monkeypatch.setattr(ui, "_auth", lambda req: {"ok": True, "name": "tom", "role": "owner"})
    resp = await r["/backlog/api/create"](_Req({"title": "brainstorm", "project": "junto", "status": "idea"}))
    out = json.loads(resp.body)
    assert resp.status_code == 200 and out["backlog_status"] == "idea"
    meta = env["cols"]["proj_junto"].docs[out["id"]][1]
    assert meta["created_by"] == "tom"
