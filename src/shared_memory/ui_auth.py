"""Human web login for the junto pages (/admin, /backlog, /dashboard).

Humans log in with a username and password and get a session cookie, so they
don't need to paste an API key. Agents and scripts keep using
`Authorization: Bearer <key>`; both are accepted everywhere the pages call.

GET  /ui/account.js            the shared account widget (login form + account menu)
POST /ui/api/login             {username, password} -> sets the session cookie
POST /ui/api/logout            ends this browser's session
POST /ui/api/logout_all        ends every session of the logged-in user
POST /ui/api/password          {current, new} (session only)
GET  /ui/api/keys              active API keys, no secrets       (owner)
POST /ui/api/keys/create       {name, role, projects} -> raw key ONCE  (owner)
POST /ui/api/keys/revoke       {name}                             (owner)

Accounts live in Mongo `ui_users` and are created on the host only, never over
the web: `docker exec -it mcp-rag-arch python -m shared_memory.ui_user add <name>`.

Security model:
- Passwords: stdlib scrypt (n=2^14, r=8, p=1), random 16-byte salt,
  constant-time compare. A missing user still costs one scrypt (no username
  oracle by timing).
- Sessions: a random 32-byte token in an HttpOnly, SameSite=Strict cookie. The
  server stores only its sha256 (`ui_sessions`, TTL index on expires_at).
  30-day expiry, renewed on use. The user's role and scope are re-read from
  `ui_users` on every request, so disabling a user or changing a role is
  immediate.
- CSRF: a cookie-authenticated state-changing request must carry the custom
  header `X-Junto-UI: 1`. Another site cannot set it without a CORS preflight,
  and this server grants none (no CORS middleware anywhere). Origin is NOT
  compared with Host: proxies, port forwards and Tailscale names legitimately
  make them differ (the first live login was refused that way, 2026-11-05).
  A mismatch is only logged.
- Brute force: 5 failed logins for a username within 15 min locks that
  username for the rest of the window (in-process; resets on restart).
- Every /ui route refuses tunnel origins, like /admin.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared_memory.helpers import utc_now

log = logging.getLogger(__name__)

ACCOUNT_JS = Path(__file__).parent / "account.js"
COOKIE = "junto_ui"
SESSION_DAYS = 30
TOUCH_SECONDS = 600            # renew last_seen / expires_at at most this often
CSRF_HEADER = "x-junto-ui"
LOCK_FAILS, LOCK_WINDOW = 5, 15 * 60
MIN_PASSWORD = 10
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_DUMMY_HASH = None              # computed lazily; verified against when the user is unknown
_FAILS: Dict[str, List[float]] = {}
_indexes_done = False


# ── passwords ──

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${s}${h}".format(
        s=base64.b64encode(salt).decode(), h=base64.b64encode(dk).decode(), **_SCRYPT)


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, s, h = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(s), dklen=32,
                            n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(dk, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    return _DUMMY_HASH


# ── users (host CLI: shared_memory.ui_user) ──

def _norm(username: str) -> str:
    return (username or "").strip().lower()


def create_user(db, username: str, password: str, role: str = "owner",
                projects: Optional[List[str]] = None) -> Dict[str, Any]:
    from shared_memory.auth import ROLES
    u = _norm(username)
    if not u or not u.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return {"error": "username: letters, digits, - _ . only"}
    if role not in ROLES:
        return {"error": f"role must be one of {ROLES}"}
    if len(password or "") < MIN_PASSWORD:
        return {"error": f"password must be at least {MIN_PASSWORD} characters"}
    if db.ui_users.find_one({"_id": u}):
        return {"error": f"user {u} already exists"}
    now = utc_now()
    db.ui_users.insert_one({"_id": u, "password_hash": hash_password(password), "role": role,
                            "projects": list(projects or []), "disabled": False,
                            "created": now, "password_changed": now})
    return {"username": u, "role": role, "projects": list(projects or [])}


def set_password(db, username: str, password: str) -> Dict[str, Any]:
    u = _norm(username)
    if len(password or "") < MIN_PASSWORD:
        return {"error": f"password must be at least {MIN_PASSWORD} characters"}
    r = db.ui_users.update_one({"_id": u}, {"$set": {"password_hash": hash_password(password),
                                                     "password_changed": utc_now()}})
    if not r.matched_count:
        return {"error": f"no user {u}"}
    return {"username": u, "password_changed": True}


def set_disabled(db, username: str, disabled: bool) -> Dict[str, Any]:
    u = _norm(username)
    r = db.ui_users.update_one({"_id": u}, {"$set": {"disabled": bool(disabled)}})
    if not r.matched_count:
        return {"error": f"no user {u}"}
    if disabled:
        db.ui_sessions.delete_many({"username": u})
    return {"username": u, "disabled": bool(disabled)}


def list_users(db) -> List[Dict[str, Any]]:
    return [{"username": d["_id"], "role": d.get("role"), "projects": d.get("projects") or [],
             "disabled": bool(d.get("disabled")), "created": d.get("created")}
            for d in db.ui_users.find({}).sort("_id", 1)]


# ── login throttle ──

def _locked(u: str, now: float) -> bool:
    fails = [t for t in _FAILS.get(u, []) if now - t < LOCK_WINDOW]
    _FAILS[u] = fails
    return len(fails) >= LOCK_FAILS


def authenticate(db, username: str, password: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(user doc, "") on success, else (None, reason). Reason is generic on purpose."""
    u, now = _norm(username), time.time()
    if _locked(u, now):
        return None, "too many failed logins; try again in a few minutes"
    doc = db.ui_users.find_one({"_id": u}) if u else None
    ok = verify_password(password or "", doc["password_hash"] if doc else _dummy_hash())
    if not doc or not ok or doc.get("disabled"):
        _FAILS.setdefault(u, []).append(now)
        return None, "wrong username or password"
    _FAILS.pop(u, None)
    return doc, ""


# ── sessions ──

def _ensure_indexes(db) -> None:
    global _indexes_done
    if _indexes_done:
        return
    try:
        db.ui_sessions.create_index("expires_at", expireAfterSeconds=0)
        db.ui_sessions.create_index("username")
        _indexes_done = True
    except Exception as e:  # best-effort; the expiry check below still holds
        log.warning("ui_sessions index: %s", e)


def _sid(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db, username: str, user_agent: str = "") -> str:
    _ensure_indexes(db)
    token = secrets.token_urlsafe(32)
    now = utc_now()
    db.ui_sessions.insert_one({"_id": _sid(token), "username": _norm(username), "created": now,
                               "last_seen": now, "expires_at": now + timedelta(days=SESSION_DAYS),
                               "user_agent": (user_agent or "")[:200]})
    return token


def _headers(request) -> List[Tuple[bytes, bytes]]:
    return [(k.lower(), v) for k, v in request.headers.raw]


def _header(headers, name: str) -> Optional[str]:
    for k, v in headers:
        if k == name.encode():
            return v.decode("latin-1")
    return None


def _cookie_token(headers) -> Optional[str]:
    raw = _header(headers, "cookie")
    if not raw:
        return None
    try:
        c = SimpleCookie()
        c.load(raw)
    except Exception:
        return None
    m = c.get(COOKIE)
    return m.value if m and m.value else None


def _csrf_ok(headers) -> bool:
    """The X-Junto-UI header is the CSRF defence. Origin vs Host is logged, not enforced."""
    if _header(headers, CSRF_HEADER) != "1":
        return False
    origin, host = _header(headers, "origin"), _header(headers, "host")
    if origin and host and origin.split("://", 1)[-1].rstrip("/").lower() != host.lower():
        log.info("ui_auth: Origin %r differs from Host %r (allowed; header present)", origin, host)
    return True


def session_user(request, db=None) -> Optional[Dict[str, Any]]:
    """The logged-in user for this request's cookie, or None.

    Returns {name, role, projects, via: "session", csrf_ok}. csrf_ok is False on
    a state-changing request that lacks X-Junto-UI or comes from another origin;
    callers must refuse those."""
    headers = _headers(request)
    token = _cookie_token(headers)
    if not token:
        return None
    if db is None:
        from shared_memory.clients import get_mongo
        db = get_mongo()
    if db is None:
        return None
    now = utc_now()
    s = db.ui_sessions.find_one({"_id": _sid(token)})
    if not s or _aware(s.get("expires_at")) <= now:
        return None
    user = db.ui_users.find_one({"_id": s.get("username")})
    if not user or user.get("disabled"):
        return None
    if (now - _aware(s.get("last_seen") or now)).total_seconds() > TOUCH_SECONDS:
        db.ui_sessions.update_one({"_id": s["_id"]}, {"$set": {
            "last_seen": now, "expires_at": now + timedelta(days=SESSION_DAYS)}})
    method = (getattr(request, "method", "GET") or "GET").upper()
    csrf_ok = method in ("GET", "HEAD") or _csrf_ok(headers)
    return {"name": user["_id"], "role": user.get("role") or "user",
            "projects": user.get("projects") or [], "via": "session", "csrf_ok": csrf_ok,
            "_sid": s["_id"]}


def _aware(dt):
    """Mongo returns naive UTC datetimes; utc_now() is aware."""
    if dt is not None and getattr(dt, "tzinfo", None) is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _cookie_header(request, token: str, max_age: int) -> str:
    headers = _headers(request)
    secure = (_header(headers, "x-forwarded-proto") == "https"
              or getattr(getattr(request, "url", None), "scheme", "") == "https")
    return (f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


# ── routes ──

def register_ui_auth_routes(mcp, get_mongo) -> None:
    from starlette.responses import JSONResponse, Response

    from shared_memory import auth
    from shared_memory.admin_ui import _auth, _tunnel_only
    from shared_memory.audit import log_audit

    async def _body(request) -> Optional[Dict[str, Any]]:
        try:
            b = json.loads(await request.body() or b"{}")
            return b if isinstance(b, dict) else None
        except ValueError:
            return None

    def _gate(request):
        """Tunnel refused; JSON POSTs must carry X-Junto-UI."""
        blocked = _tunnel_only(request)
        if blocked:
            return blocked
        if not _csrf_ok(_headers(request)):
            return JSONResponse({"error": "missing X-Junto-UI header"}, status_code=403)
        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable"}, status_code=503)
        return None

    def _owner(request):
        a = _auth(request)
        if not a["ok"]:
            return None, JSONResponse({"error": a["error"]}, status_code=a["status"])
        return a, None

    @mcp.custom_route("/ui/account.js", methods=["GET"])
    async def ui_account_js(request):
        blocked = _tunnel_only(request)
        if blocked:
            return blocked
        try:
            return Response(ACCOUNT_JS.read_text(encoding="utf-8"), media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})
        except OSError:
            return Response("console.error('account.js missing')", media_type="application/javascript",
                            status_code=500)

    @mcp.custom_route("/ui/api/login", methods=["POST"])
    async def ui_login(request):
        err = _gate(request)
        if err:
            return err
        b = await _body(request) or {}
        db = get_mongo()
        user, why = authenticate(db, b.get("username") or "", b.get("password") or "")
        if not user:
            log_audit("ui.login_failed", _norm(b.get("username") or "")[:64], details={"reason": why})
            return JSONResponse({"error": why}, status_code=429 if "too many" in why else 401)
        token = create_session(db, user["_id"], _header(_headers(request), "user-agent") or "")
        log_audit("ui.login", user["_id"])
        resp = JSONResponse({"name": user["_id"], "role": user.get("role"), "via": "session",
                             "can_write": user.get("role") in auth_owner_roles()})
        resp.headers["Set-Cookie"] = _cookie_header(request, token, SESSION_DAYS * 86400)
        return resp

    @mcp.custom_route("/ui/api/logout", methods=["POST"])
    async def ui_logout(request):
        err = _gate(request)
        if err:
            return err
        token = _cookie_token(_headers(request))
        if token:
            get_mongo().ui_sessions.delete_one({"_id": _sid(token)})
        resp = JSONResponse({"logged_out": True})
        resp.headers["Set-Cookie"] = _cookie_header(request, "", 0)
        return resp

    @mcp.custom_route("/ui/api/logout_all", methods=["POST"])
    async def ui_logout_all(request):
        err = _gate(request)
        if err:
            return err
        s = session_user(request, get_mongo())
        if not s:
            return JSONResponse({"error": "not logged in"}, status_code=401)
        n = get_mongo().ui_sessions.delete_many({"username": s["name"]}).deleted_count
        log_audit("ui.logout_all", s["name"], details={"sessions": n})
        resp = JSONResponse({"logged_out": True, "sessions_ended": n})
        resp.headers["Set-Cookie"] = _cookie_header(request, "", 0)
        return resp

    @mcp.custom_route("/ui/api/password", methods=["POST"])
    async def ui_password(request):
        err = _gate(request)
        if err:
            return err
        db = get_mongo()
        s = session_user(request, db)
        if not s:
            return JSONResponse({"error": "log in with your password first"}, status_code=401)
        b = await _body(request) or {}
        user, why = authenticate(db, s["name"], b.get("current") or "")
        if not user:
            return JSONResponse({"error": "current password is wrong" if "wrong" in why else why}, status_code=403)
        out = set_password(db, s["name"], b.get("new") or "")
        if out.get("error"):
            return JSONResponse(out, status_code=400)
        # Other browsers lose their session; this one keeps it.
        db.ui_sessions.delete_many({"username": s["name"], "_id": {"$ne": s["_sid"]}})
        log_audit("ui.password_changed", s["name"])
        return JSONResponse(out)

    @mcp.custom_route("/ui/api/keys", methods=["GET"])
    async def ui_keys(request):
        a, err = _owner(request)
        if err:
            return err
        rows = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                for r in auth.list_api_keys()]
        return JSONResponse({"keys": rows, "roles": auth.ROLES})

    @mcp.custom_route("/ui/api/keys/create", methods=["POST"])
    async def ui_key_create(request):
        a, err = _owner(request)
        if err:
            return err
        b = await _body(request) or {}
        name = (b.get("name") or "").strip()
        role = b.get("role") or "agent"
        projects = [p.strip() for p in (b.get("projects") or []) if str(p).strip()]
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if any(k["name"] == name for k in auth.list_api_keys()):
            return JSONResponse({"error": f"an active key named {name} already exists"}, status_code=400)
        try:
            raw, rec = auth.create_api_key(name, role=role, projects=projects, created_by=f"ui:{a['name']}")
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        log_audit("admin.key_created", f"ui:{a['name']}", details={"name": name, "role": role, "projects": projects})
        return JSONResponse({"key": raw, **rec})

    @mcp.custom_route("/ui/api/keys/revoke", methods=["POST"])
    async def ui_key_revoke(request):
        a, err = _owner(request)
        if err:
            return err
        b = await _body(request) or {}
        name = (b.get("name") or "").strip()
        if not name or not auth.revoke_api_key(name):
            return JSONResponse({"error": f"no active key named {name}"}, status_code=400)
        log_audit("admin.key_revoked", f"ui:{a['name']}", details={"name": name})
        return JSONResponse({"revoked": name})


def auth_owner_roles():
    from shared_memory.admin_ui import OWNER_ROLES
    return OWNER_ROLES
