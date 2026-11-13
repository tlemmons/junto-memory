"""interface:knowledge-versioning-v0: sys:* labels, find_function
include_inactive, register_function tags, memory_update_tags, and the
archive/restore error + facets source_status additions."""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import functions as functions_mod
from shared_memory.tools import lifecycle
from shared_memory.versioning import version_labels


class _FakeCollection:
    def __init__(self, name, docs, contents=None, fail_get=False):
        self.name = name
        self.docs = docs  # id -> metadata
        self.contents = contents or {}
        self.fail_get = fail_get
        self.last_where = None

    async def get(self, ids=None, where=None, include=None):
        if self.fail_get:
            raise RuntimeError("boom")
        items = [(i, m) for i, m in self.docs.items()
                 if (ids is None or i in ids)
                 and (not where or all(m.get(k) == v for k, v in where.items()))]
        return {"ids": [i for i, _ in items], "metadatas": [dict(m) for _, m in items],
                "documents": [self.contents.get(i, "") for i, _ in items]}

    async def update(self, ids, metadatas):
        for i, m in zip(ids, metadatas):
            self.docs[i] = m

    async def upsert(self, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = m
            self.contents[i] = d

    async def query(self, query_texts=None, n_results=5, where=None):
        self.last_where = where
        status_ok = (lambda m: m.get("status") == "active") if "$and" in (where or {}) else (lambda m: True)
        rows = [(i, m) for i, m in self.docs.items()
                if m.get("type") == "function_ref" and status_ok(m)]
        return {
            "ids": [[i for i, _ in rows]],
            "documents": [[self.contents.get(i, "") for i, _ in rows]],
            "metadatas": [[dict(m) for _, m in rows]],
            "distances": [[m.get("_dist", 0.5) for _, m in rows]],
        }


class _FakeChroma:
    def __init__(self, cols):
        self.cols = cols

    async def list_collections(self):
        return self.cols


class _FakeMongoColl:
    def __init__(self):
        self.calls = []

    def update_many(self, f, u):
        self.calls.append((f, u))

        class _R:
            modified_count = len(f["_id"]["$in"]) if isinstance(f["_id"], dict) else 1
        return _R()


class _FakeDb:
    def __init__(self):
        self.learning_facets = _FakeMongoColl()
        self.learning_claims = _FakeMongoColl()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(lifecycle, "emit_op_log_from_context", lambda **kw: None)
    active_sessions.clear()
    active_sessions["sess_test"] = {
        "claude_instance": "tester", "project": "test", "role": "agent",
        "allowed_projects": ["test"], "started_at": "2026-11-13T00:00:00+00:00",
    }
    yield
    active_sessions.clear()


# ── labels ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tags,expected", [
    (None, {}),
    ("[]", {}),
    (json.dumps(["x", "y"]), {}),
    (json.dumps(["sys:old"]), {"applies_to": "old"}),
    (["sys:REBUILD"], {"applies_to": "rebuild"}),
    (["sys:both"], {"applies_to": "both"}),
    (["sys:old", "sys:rebuild"], {"applies_to": "both"}),
    (["sys:weird"], {}),
    (["sys:old", "valid-until:Lane3-Cutover", "valid-until:later"],
     {"applies_to": "old", "valid_until": "Lane3-Cutover"}),
    (["valid-until:"], {}),
    ("not json", {}),
])
def test_version_labels(tags, expected):
    assert version_labels(tags) == expected


# ── memory_update_tags ──────────────────────────────────────────────────────

def _patch_lifecycle(monkeypatch, col, db=None):
    async def _get_chroma():
        return _FakeChroma([col])

    async def _proj(c, p):
        return col

    async def _shared(c, n):
        return _FakeCollection("shared_" + n, {})

    monkeypatch.setattr(lifecycle, "get_chroma", _get_chroma)
    monkeypatch.setattr(lifecycle, "get_project_collection", _proj)
    monkeypatch.setattr(lifecycle, "get_shared_collection", _shared)
    monkeypatch.setattr(lifecycle, "get_mongo", lambda: db)


async def test_update_tags_adds_and_removes(monkeypatch):
    col = _FakeCollection("proj_test", {
        "learning_1": {"title": "t", "status": "active", "tags": json.dumps(["a", "sys:both"])},
    })
    _patch_lifecycle(monkeypatch, col)
    out = json.loads(await lifecycle.memory_update_tags(
        session_id="sess_test", doc_id="learning_1",
        add=["sys:old", "a", " "], remove=["sys:both", "missing"]))
    assert out["status"] == "updated"
    assert out["added"] == ["sys:old"]
    assert out["removed"] == ["sys:both"]
    assert json.loads(col.docs["learning_1"]["tags"]) == ["a", "sys:old"]
    assert col.docs["learning_1"]["status"] == "active"


async def test_update_tags_noop_and_validation(monkeypatch):
    col = _FakeCollection("proj_test", {"learning_1": {"title": "t", "tags": json.dumps(["a"])}})
    _patch_lifecycle(monkeypatch, col)
    out = json.loads(await lifecycle.memory_update_tags(session_id="sess_test", doc_id="learning_1", add=["a"]))
    assert out["status"] == "unchanged"
    err = json.loads(await lifecycle.memory_update_tags(session_id="sess_test", doc_id="learning_1"))
    assert "error" in err


async def test_update_tags_not_found(monkeypatch):
    col = _FakeCollection("proj_test", {})
    _patch_lifecycle(monkeypatch, col)
    out = json.loads(await lifecycle.memory_update_tags(session_id="sess_test", doc_id="nope", add=["x"]))
    assert "error" in out


# ── archive / restore additions ─────────────────────────────────────────────

async def test_archive_restore_stamp_facets_and_report_errors(monkeypatch):
    good = _FakeCollection("proj_test", {
        "learning_1": {"title": "t", "status": "active", "tags": json.dumps(["sys:old"])},
        "learning_2": {"title": "u", "status": "active", "tags": json.dumps(["sys:both"])},
    })
    bad = _FakeCollection("proj_other", {}, fail_get=True)
    db = _FakeDb()

    async def _get_chroma():
        return _FakeChroma([good, bad])

    monkeypatch.setattr(lifecycle, "get_chroma", _get_chroma)
    monkeypatch.setattr(lifecycle, "get_mongo", lambda: db)
    monkeypatch.setattr(lifecycle, "collection_allowed", lambda s, n: True)

    out = json.loads(await lifecycle.memory_archive_by_tag(session_id="sess_test", tag="sys:old"))
    assert out["archived_count"] == 1
    assert out["status"] == "partial"
    assert out["errors"] and out["errors"][0].startswith("proj_other:")
    assert good.docs["learning_2"]["status"] == "active"  # sys:both untouched
    f, u = db.learning_facets.calls[-1]
    assert f == {"_id": {"$in": ["learning_1"]}}
    assert u["$set"]["source_status"] == "archived"

    out = json.loads(await lifecycle.memory_restore_by_tag(session_id="sess_test", tag="sys:old"))
    assert out["restored_count"] == 1
    assert good.docs["learning_1"]["status"] == "active"
    assert db.learning_claims.calls[-1][1]["$set"]["source_status"] == "active"


# ── find_function + register_function ──────────────────────────────────────

def _patch_functions(monkeypatch, proj, shared):
    async def _get_chroma():
        return None

    async def _proj(c, p):
        return proj

    async def _shared(c, n):
        return shared

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(functions_mod, "get_chroma", _get_chroma)
    monkeypatch.setattr(functions_mod, "get_project_collection", _proj)
    monkeypatch.setattr(functions_mod, "get_shared_collection", _shared)
    monkeypatch.setattr(functions_mod, "update_access_stats", _noop)
    monkeypatch.setattr(functions_mod, "calculate_relevance", lambda d: 1 - d)
    monkeypatch.setattr(functions_mod, "MIN_RELEVANCE_THRESHOLD", 0.0)


async def test_find_function_active_only_by_default(monkeypatch):
    proj = _FakeCollection("proj_test", {
        "func_old": {"type": "function_ref", "status": "archived", "func_name": "old",
                     "tags": json.dumps(["sys:old"]), "_dist": 0.1},
        "func_new": {"type": "function_ref", "status": "active", "func_name": "new",
                     "tags": json.dumps(["sys:rebuild"]), "_dist": 0.3},
    })
    _patch_functions(monkeypatch, proj, _FakeCollection("shared_patterns", {}))
    out = json.loads(await functions_mod.memory_find_function(session_id="sess_test", query="x"))
    assert [r["name"] for r in out["results"]] == ["new"]
    assert out["results"][0]["applies_to"] == "rebuild"
    assert out["results"][0]["status"] == "active"
    assert "warning" not in out["results"][0]


async def test_find_function_include_inactive_flags_and_sorts_numerically(monkeypatch):
    proj = _FakeCollection("proj_test", {
        "func_old": {"type": "function_ref", "status": "archived", "func_name": "old",
                     "tags": json.dumps(["sys:old", "valid-until:lane3"]), "_dist": 0.15},
        "func_low": {"type": "function_ref", "status": "active", "func_name": "low", "_dist": 0.91},
    })
    _patch_functions(monkeypatch, proj, _FakeCollection("shared_patterns", {}))
    out = json.loads(await functions_mod.memory_find_function(
        session_id="sess_test", query="x", include_inactive=True, include_shared=False))
    # 85% must outrank 9%; the old string sort put "9%" first.
    assert [r["name"] for r in out["results"]] == ["old", "low"]
    old = out["results"][0]
    assert old["status"] == "archived" and old["warning"]
    assert old["applies_to"] == "old" and old["valid_until"] == "lane3"
    assert "_rel" not in old
    assert "applies_to" not in out["results"][1]


async def test_register_function_tags_set_preserve_replace(monkeypatch):
    proj = _FakeCollection("proj_test", {})
    _patch_functions(monkeypatch, proj, _FakeCollection("shared_patterns", {}))
    monkeypatch.setattr(functions_mod, "emit_op_log_from_context", lambda **kw: None)

    async def _emb(*a, **k):
        return None

    async def _notify(*a, **k):
        return None

    monkeypatch.setattr(functions_mod, "fetch_embedding_for_op_log", _emb)
    monkeypatch.setattr(functions_mod, "notify_librarian", _notify)
    monkeypatch.setattr(functions_mod, "get_mongo", lambda: None)

    async def reg(**kw):
        return json.loads(await functions_mod.memory_register_function(
            session_id="sess_test", name="f", file="a.py:1", purpose="p", project="test", **kw))

    fid = (await reg(tags=["sys:old"]))["id"]
    assert json.loads(proj.docs[fid]["tags"]) == ["sys:old"]
    await reg()  # omitted -> preserved
    assert json.loads(proj.docs[fid]["tags"]) == ["sys:old"]
    await reg(tags=["sys:both"])  # list -> replaced
    assert json.loads(proj.docs[fid]["tags"]) == ["sys:both"]
