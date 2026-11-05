"""Human web login (ui_auth.py): passwords, sessions, CSRF, the /ui routes, and the
admin_ui gates accepting the login cookie."""

import os
import uuid
from datetime import timedelta

import pytest

import shared_memory.admin_ui as admin_ui
import shared_memory.auth as auth
import shared_memory.clients as clients
from shared_memory import ui_auth
from shared_memory.helpers import utc_now

PW = "correct horse battery"


@pytest.fixture
def db(monkeypatch):
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
    d = client[f"uiauth_test_{uuid.uuid4().hex[:8]}"]
    monkeypatch.setattr(clients, "get_mongo", lambda: d)
    monkeypatch.setattr(ui_auth, "_indexes_done", False)
    ui_auth._FAILS.clear()
    try:
        yield d
    finally:
        client.drop_database(d.name)


class _Req:
    def __init__(self, headers, method="GET"):
        self.headers = type("H", (), {"raw": [(k.encode(), v.encode()) for k, v in headers.items()]})()
        self.method = method


def _cookie_req(db, username, method="GET", **extra):
    token = ui_auth.create_session(db, username)
    return _Req({"Cookie": f"{ui_auth.COOKIE}={token}", "Host": "sage:8080", **extra}, method), token


# ── passwords + users ──

def test_password_hash_roundtrip_and_rejects():
    h = ui_auth.hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert ui_auth.verify_password(PW, h)
    assert not ui_auth.verify_password(PW + "x", h)
    assert not ui_auth.verify_password(PW, "garbage")
    assert ui_auth.hash_password(PW) != h          # salted


def test_create_user_validates(db):
    assert "at least" in ui_auth.create_user(db, "tom", "short")["error"]
    assert "role" in ui_auth.create_user(db, "tom", PW, role="god")["error"]
    assert "letters" in ui_auth.create_user(db, "to m", PW)["error"]
    assert ui_auth.create_user(db, "Tom", PW)["username"] == "tom"
    assert "exists" in ui_auth.create_user(db, "tom", PW)["error"]


def test_authenticate_lockout_and_disable(db):
    ui_auth.create_user(db, "tom", PW)
    assert ui_auth.authenticate(db, "TOM", PW)[0]["_id"] == "tom"
    for _ in range(ui_auth.LOCK_FAILS):
        assert ui_auth.authenticate(db, "tom", "nope")[0] is None
    user, why = ui_auth.authenticate(db, "tom", PW)      # right password, but locked
    assert user is None and "too many" in why
    ui_auth._FAILS.clear()
    ui_auth.set_disabled(db, "tom", True)
    assert ui_auth.authenticate(db, "tom", PW)[0] is None
    # unknown users get the same generic answer
    assert ui_auth.authenticate(db, "nobody", PW)[1] == "wrong username or password"


# ── sessions + CSRF ──

def test_session_user_and_csrf(db):
    ui_auth.create_user(db, "tom", PW, role="owner", projects=[])
    req, token = _cookie_req(db, "tom")
    s = ui_auth.session_user(req, db)
    assert s["name"] == "tom" and s["role"] == "owner" and s["csrf_ok"]
    assert db.ui_sessions.find_one({"_id": ui_auth._sid(token)}) and not db.ui_sessions.find_one({"_id": token})
    post = _Req({"Cookie": f"{ui_auth.COOKIE}={token}", "Host": "sage:8080"}, "POST")
    assert not ui_auth.session_user(post, db)["csrf_ok"]
    ok = _Req({"Cookie": f"{ui_auth.COOKIE}={token}", "Host": "sage:8080", "X-Junto-UI": "1",
               "Origin": "http://sage:8080"}, "POST")
    assert ui_auth.session_user(ok, db)["csrf_ok"]
    evil = _Req({"Cookie": f"{ui_auth.COOKIE}={token}", "Host": "sage:8080", "X-Junto-UI": "1",
                 "Origin": "http://evil.example"}, "POST")
    assert not ui_auth.session_user(evil, db)["csrf_ok"]
    assert ui_auth.session_user(_Req({"Cookie": f"{ui_auth.COOKIE}=forged"}), db) is None


def test_session_expiry_disable_and_role_reread(db):
    ui_auth.create_user(db, "tom", PW, role="owner")
    req, token = _cookie_req(db, "tom")
    db.ui_users.update_one({"_id": "tom"}, {"$set": {"role": "user"}})
    assert ui_auth.session_user(req, db)["role"] == "user"       # role change is immediate
    db.ui_sessions.update_one({"_id": ui_auth._sid(token)}, {"$set": {"expires_at": utc_now() - timedelta(seconds=1)}})
    assert ui_auth.session_user(req, db) is None
    req2, _ = _cookie_req(db, "tom")
    ui_auth.set_disabled(db, "tom", True)
    assert ui_auth.session_user(req2, db) is None
    assert db.ui_sessions.count_documents({"username": "tom"}) == 0


# ── admin_ui gates accept the cookie ──

def test_auth_accepts_owner_cookie_and_enforces_csrf(db):
    ui_auth.create_user(db, "tom", PW, role="owner")
    ui_auth.create_user(db, "viewer", PW, role="user")
    req, token = _cookie_req(db, "tom")
    a = admin_ui._auth(req)
    assert a["ok"] and a["via"] == "session" and a["name"] == "tom"
    post = _Req({"Cookie": f"{ui_auth.COOKIE}={token}", "Host": "h"}, "POST")
    assert admin_ui._auth(post)["status"] == 403
    post_ok = _Req({"Cookie": f"{ui_auth.COOKIE}={token}", "Host": "h", "X-Junto-UI": "1"}, "POST")
    assert admin_ui._auth(post_ok)["ok"]
    vreq, _ = _cookie_req(db, "viewer")
    v = admin_ui._auth(vreq)
    assert not v["ok"] and v["status"] == 403 and v["name"] == "viewer"
    tun, _ = _cookie_req(db, "tom", **{"CF-Connecting-IP": "1.2.3.4"})
    assert admin_ui._auth(tun)["error"] == "admin is LAN-only"
    assert admin_ui._auth(_Req({}))["status"] == 401


def test_lan_only_under_require_key_accepts_unscoped_login(db, monkeypatch):
    monkeypatch.setattr(auth, "REQUIRE_KEY", True)
    ui_auth.create_user(db, "tom", PW)
    ui_auth.create_user(db, "scoped", PW, role="owner", projects=["nimbus"])
    req, _ = _cookie_req(db, "tom")
    assert admin_ui._lan_only(req) is None
    sreq, _ = _cookie_req(db, "scoped")
    assert admin_ui._lan_only(sreq).status_code == 403
    assert admin_ui._lan_only(_Req({})).status_code == 401


# ── routes ──

class _App:
    def __init__(self):
        from starlette.routing import Route
        self.routes, self._Route = [], Route

    def custom_route(self, path, methods):
        def deco(fn):
            self.routes.append(self._Route(path, fn, methods=methods))
            return fn
        return deco


@pytest.fixture
def client(db):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    app = _App()
    ui_auth.register_ui_auth_routes(app, lambda: db)
    admin_ui.register_admin_routes(app, lambda: db)
    return TestClient(Starlette(routes=app.routes), base_url="http://sage:8080")


H = {"X-Junto-UI": "1"}


def test_login_cookie_flags_and_whoami(client, db):
    ui_auth.create_user(db, "tom", PW)
    assert client.post("/ui/api/login", json={"username": "tom", "password": PW}).status_code == 403  # no CSRF header
    bad = client.post("/ui/api/login", json={"username": "tom", "password": "nope"}, headers=H)
    assert bad.status_code == 401 and "set-cookie" not in bad.headers
    r = client.post("/ui/api/login", json={"username": "tom", "password": PW}, headers=H)
    assert r.status_code == 200 and r.json()["can_write"]
    sc = r.headers["set-cookie"]
    assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Max-Age=2592000" in sc
    me = client.get("/admin/api/whoami").json()
    assert me["name"] == "tom" and me["via"] == "session" and me["can_write"]
    client.post("/ui/api/logout", headers=H)
    assert db.ui_sessions.count_documents({}) == 0
    assert client.get("/admin/api/whoami").status_code == 401


def test_keys_create_list_revoke(client, db):
    ui_auth.create_user(db, "tom", PW)
    assert client.get("/ui/api/keys").status_code == 401
    client.post("/ui/api/login", json={"username": "tom", "password": PW}, headers=H)
    assert client.post("/ui/api/keys/create", json={"name": "k1"}).status_code == 403   # cookie POST needs header
    r = client.post("/ui/api/keys/create", json={"name": "k1", "role": "agent", "projects": ["nimbus"]}, headers=H)
    raw = r.json()["key"]
    assert raw.startswith("smk_") and auth.validate_api_key(raw)["projects"] == ["nimbus"]
    assert "already exists" in client.post("/ui/api/keys/create", json={"name": "k1"}, headers=H).json()["error"]
    listed = client.get("/ui/api/keys").json()["keys"]
    assert [k["name"] for k in listed] == ["k1"] and raw not in str(listed)
    assert client.post("/ui/api/keys/revoke", json={"name": "k1"}, headers=H).json()["revoked"] == "k1"
    assert auth.validate_api_key(raw) is None
    assert db.audit_log.count_documents({"event_type": {"$in": ["admin.key_created", "admin.key_revoked"]}}) == 2


def test_non_owner_cannot_manage_keys(client, db):
    ui_auth.create_user(db, "viewer", PW, role="user")
    client.post("/ui/api/login", json={"username": "viewer", "password": PW}, headers=H)
    assert client.get("/ui/api/keys").status_code == 403
    assert client.post("/ui/api/keys/create", json={"name": "x"}, headers=H).status_code == 403


def test_password_change_ends_other_sessions(client, db):
    ui_auth.create_user(db, "tom", PW)
    ui_auth.create_session(db, "tom")                       # another browser
    client.post("/ui/api/login", json={"username": "tom", "password": PW}, headers=H)
    assert client.post("/ui/api/password", json={"current": "wrong", "new": "x" * 12}, headers=H).status_code == 403
    r = client.post("/ui/api/password", json={"current": PW, "new": "a brand new secret"}, headers=H)
    assert r.status_code == 200
    assert db.ui_sessions.count_documents({"username": "tom"}) == 1          # only this one survives
    assert client.get("/admin/api/whoami").json()["name"] == "tom"
    assert ui_auth.authenticate(db, "tom", "a brand new secret")[0]


def test_tunnel_refused_on_login(client, db):
    ui_auth.create_user(db, "tom", PW)
    r = client.post("/ui/api/login", json={"username": "tom", "password": PW},
                    headers={**H, "CF-Connecting-IP": "1.2.3.4"})
    assert r.status_code == 403
