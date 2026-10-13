"""Junto Admin: a small web UI for system settings, served by the junto server.

GET  /admin                    the page (admin.html)
GET  /admin/api/whoami         who the Bearer key is (login check)
GET  /admin/api/projects       projects known to the server
GET  /admin/api/settings       every setting: default / override / effective
POST /admin/api/settings       set or reset one setting   (owner key)
GET  /admin/api/agents         agents with context gauge + idle-park membership
GET  /admin/api/alerts         push-control alerts
POST /admin/api/alerts/ack     acknowledge an alert        (owner key)
GET  /admin/api/changes        change log of settings writes

Reads are LAN-open like /dashboard. Writes need an OWNER-role API key
(Authorization: Bearer). Every /admin request arriving through the public
tunnel is refused, so the page stays LAN-only even if a tunnel route is added.

ADDING A SETTING: put it in the owning module's CONFIG_KEYS (push_control or
query_config) and add a line to DESCRIPTIONS below; the page renders it.
ADDING A SECTION: add an entry to SECTIONS with that module's
get_effective_config / set_config_value / reset_config / _default_config_dict.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

ADMIN_HTML = Path(__file__).parent / "admin.html"
OWNER_ROLES = {"owner"}

DESCRIPTIONS = {
    # push_control
    "depth_cap": "Max reply-chain depth before pushes are suppressed (messages still persist).",
    "push_budget": "Pushes per sender per rolling hour before suppression.",
    "hard_ceiling": "Absolute sends per sender per hour; above it the sender is suspended.",
    "recovery_behavior": "What happens to suppressed messages on recovery: annotated | quarantine | leave_it.",
    "incident_pad_messages": "Recovery pad: messages.",
    "incident_pad_seconds": "Recovery pad: seconds.",
    "webhook_url": "Where push-control alerts are POSTed (empty = none).",
    "webhook_token": "Shared token sent with alert webhooks.",
    "warn_fraction": "budget_warn alert fires at this fraction of push_budget.",
    "idle_park_minutes": "Nudge an idle agent to park after N minutes (0 = off). Prompt cache expires ~60 min.",
    "idle_park_optout": "Comma list of agents never nudged.",
    "idle_park_min_context": "Only nudge agents whose reported context is at least this many tokens.",
    "idle_park_autoreset": "Comma list of agents auto-/clear + go'd after a nudged park (tmux boxes).",
    "action_lapse_days": "Unanswered action messages lapse (leave the action lane, not deleted) after N days. 0 = off.",
    "action_lapse_responded_days": "Answered-but-never-closed tasks/blockers lapse N days after the reply. 0 = use action_lapse_days.",
    # query_config
    "default_expand": "memory_query returns full bodies by default.",
    "default_expand_top": "memory_query: top N results get full bodies.",
    "default_snippet_length": "memory_query snippet length when not expanded.",
    "recall_floor": "Minimum similarity (0-1) for the per-turn recall hook to inject a memory.",
}

SECRET_KEYS = {"webhook_token"}


def _sections():
    from shared_memory import push_control, query_config
    return {
        "push_control": {
            "title": "Messaging & agent lifecycle (push_control)",
            "module": push_control,
            "keys": push_control.CONFIG_KEYS,
            "collection": "push_control_config",
            "default_scope": "__default__",
        },
        "query_config": {
            "title": "Search & recall (query_config)",
            "module": query_config,
            "keys": query_config.CONFIG_KEYS,
            "collection": "query_config",
            "default_scope": query_config.DEFAULT_SCOPE,
        },
    }


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _mask(key, value):
    if key in SECRET_KEYS and value:
        return "••••" + str(value)[-4:]
    return value


def _auth(request) -> Dict[str, Any]:
    """{'ok': bool, 'name', 'role', 'error', 'status'} for the request's Bearer key."""
    from shared_memory.auth import detect_tunnel_origin, parse_bearer_token, validate_api_key

    headers = [(k.lower(), v) for k, v in request.headers.raw]
    if detect_tunnel_origin(headers):
        return {"ok": False, "status": 403, "error": "admin is LAN-only"}
    key = parse_bearer_token(headers)
    if not key:
        return {"ok": False, "status": 401, "error": "no API key"}
    rec = validate_api_key(key)
    if not rec:
        return {"ok": False, "status": 401, "error": "invalid or revoked key"}
    return {"ok": rec.get("role") in OWNER_ROLES, "status": 403,
            "error": "owner role required", "name": rec.get("name"), "role": rec.get("role")}


def _lan_only(request) -> Optional[Any]:
    from starlette.responses import JSONResponse

    from shared_memory.auth import detect_tunnel_origin
    headers = [(k.lower(), v) for k, v in request.headers.raw]
    if detect_tunnel_origin(headers):
        return JSONResponse({"error": "admin is LAN-only"}, status_code=403)
    return None


def settings_payload(db, project: Optional[str]) -> Dict[str, Any]:
    out = []
    for sid, sec in _sections().items():
        mod = sec["module"]
        defaults = mod._default_config_dict()
        server_doc = mod._read_config_doc(db, sec["default_scope"]) if db is not None else {}
        proj_doc = {}
        if project:
            from shared_memory.helpers import normalize_project
            proj_doc = mod._read_config_doc(db, normalize_project(project)) if db is not None else {}
        effective = mod.get_effective_config(db, project)
        rows = []
        for key, typ in sec["keys"].items():
            rows.append({
                "key": key,
                "type": typ.__name__,
                "description": DESCRIPTIONS.get(key, ""),
                "code_default": _mask(key, defaults.get(key)),
                "server_value": _mask(key, server_doc.get(key)) if key in server_doc else None,
                "project_value": _mask(key, proj_doc.get(key)) if key in proj_doc else None,
                "effective": _mask(key, effective.get(key)),
                "secret": key in SECRET_KEYS,
            })
        out.append({"id": sid, "title": sec["title"], "settings": rows})
    return {"project": project, "sections": out}


def apply_setting(db, actor: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Set or reset one setting and record it in config_changes."""
    sections = _sections()
    sid = body.get("section")
    if sid not in sections:
        return {"error": f"unknown section {sid!r}"}
    sec = sections[sid]
    mod = sec["module"]
    key = body.get("key")
    if key not in sec["keys"]:
        return {"error": f"unknown key {key!r} for {sid}"}
    project = body.get("project") or None
    before = mod.get_effective_config(db, project).get(key)
    if body.get("reset"):
        if project is None:
            return {"error": "the server default cannot be reset; set a value instead"}
        result = mod.reset_config(db, project, key, actor)
        new_value = None
    else:
        value = body.get("value")
        typ = sec["keys"][key]
        if typ is bool and isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        result = mod.set_config_value(db, project, key, value, actor)
        new_value = value
    if isinstance(result, dict) and result.get("error"):
        return result
    after = mod.get_effective_config(db, project).get(key)
    try:
        from shared_memory.helpers import utc_now
        db.config_changes.insert_one({
            "ts": utc_now(), "actor": actor, "section": sid,
            "scope": project or "server-default", "key": key,
            "action": "reset" if body.get("reset") else "set",
            "requested": None if key in SECRET_KEYS else new_value,
            "effective_before": None if key in SECRET_KEYS else before,
            "effective_after": None if key in SECRET_KEYS else after,
        })
    except Exception as e:  # noqa: BLE001
        log.warning("admin: change log write failed (%s)", e)
    return {"ok": True, "section": sid, "key": key, "project": project,
            "effective_before": _mask(key, before), "effective_after": _mask(key, after)}


def agents_payload(db) -> Dict[str, Any]:
    from shared_memory import push_control

    status = {d.get("instance"): d for d in db.agent_status.find({})}
    rows = []
    seen = set()
    for d in db.agent_directory.find({}).sort("last_seen", -1).limit(200):
        inst, proj = d.get("instance"), d.get("project")
        if not inst or (inst, proj) in seen:
            continue
        seen.add((inst, proj))
        st = status.get(inst, {})
        cfg = push_control.get_effective_config(db, proj)
        optout = {a.strip() for a in str(cfg.get("idle_park_optout") or "").split(",")}
        auto = {a.strip() for a in str(cfg.get("idle_park_autoreset") or "").split(",")}
        rows.append({
            "instance": inst, "project": proj,
            "role": d.get("role_description", ""),
            "last_seen": _jsonable(d.get("last_seen")),
            "context_tokens": st.get("context_tokens"),
            "context_pct": st.get("context_pct"),
            "context_reported_at": _jsonable(st.get("context_reported_at")),
            "idle_park": "opt-out" if inst in optout else ("auto-reset" if inst in auto else "nudge"),
        })
    return {"agents": rows}


def register_admin_routes(mcp, get_mongo) -> None:
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

    def _db_or_error():
        db = get_mongo()
        return db, (None if db is not None else JSONResponse({"error": "MongoDB unavailable"}, status_code=503))

    @mcp.custom_route("/admin", methods=["GET"])
    async def admin_page(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        try:
            return HTMLResponse(ADMIN_HTML.read_text(encoding="utf-8"))
        except Exception as e:
            return HTMLResponse(f"<h1>Admin page error</h1><p>{e}</p>", status_code=500)

    @mcp.custom_route("/admin/", methods=["GET"])
    async def admin_slash(request):
        return RedirectResponse("/admin")

    @mcp.custom_route("/admin/api/whoami", methods=["GET"])
    async def admin_whoami(request):
        a = _auth(request)
        if a.get("name") is None:
            return JSONResponse({"error": a["error"]}, status_code=a["status"])
        return JSONResponse({"name": a["name"], "role": a["role"], "can_write": a["ok"]})

    @mcp.custom_route("/admin/api/projects", methods=["GET"])
    async def admin_projects(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        db, err = _db_or_error()
        if err:
            return err
        names = set(p for p in db.agent_directory.distinct("project") if p)
        names |= set(p for p in db.push_control_config.distinct("scope") if p and not p.startswith("__"))
        return JSONResponse({"projects": sorted(names)})

    @mcp.custom_route("/admin/api/settings", methods=["GET", "POST"])
    async def admin_settings(request):
        db, err = _db_or_error()
        if err:
            return err
        if request.method == "GET":
            blocked = _lan_only(request)
            if blocked:
                return blocked
            return JSONResponse(settings_payload(db, request.query_params.get("project") or None))
        a = _auth(request)
        if not a["ok"]:
            return JSONResponse({"error": a["error"]}, status_code=a["status"])
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        result = apply_setting(db, f"admin-ui:{a['name']}", body)
        return JSONResponse(result, status_code=400 if result.get("error") else 200)

    @mcp.custom_route("/admin/api/agents", methods=["GET"])
    async def admin_agents(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        db, err = _db_or_error()
        if err:
            return err
        return JSONResponse(agents_payload(db))

    @mcp.custom_route("/admin/api/alerts", methods=["GET"])
    async def admin_alerts(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        db, err = _db_or_error()
        if err:
            return err
        from shared_memory.push_control import list_alerts
        only_open = request.query_params.get("all") != "1"
        rows = list_alerts(db, unacknowledged_only=only_open, limit=100)
        return JSONResponse({"alerts": json.loads(json.dumps(rows, default=_jsonable))})

    @mcp.custom_route("/admin/api/alerts/ack", methods=["POST"])
    async def admin_alert_ack(request):
        db, err = _db_or_error()
        if err:
            return err
        a = _auth(request)
        if not a["ok"]:
            return JSONResponse({"error": a["error"]}, status_code=a["status"])
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        from shared_memory.push_control import acknowledge_alert
        result = acknowledge_alert(db, body.get("alert_id"), f"admin-ui:{a['name']}")
        return JSONResponse(json.loads(json.dumps(result, default=_jsonable)),
                            status_code=400 if result.get("error") else 200)

    @mcp.custom_route("/admin/api/changes", methods=["GET"])
    async def admin_changes(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        db, err = _db_or_error()
        if err:
            return err
        rows = []
        for d in db.config_changes.find({}, {"_id": 0}).sort("ts", -1).limit(200):
            rows.append({k: _jsonable(v) for k, v in d.items()})
        return JSONResponse({"changes": rows})


def register_connection_routes(mcp, get_mongo) -> None:
    """Connections tab (interface:delegated-inference-v0 1.5.0 §3).

    GET  /admin/api/connections            connections (no secrets) + audit
    POST /admin/api/connections/activate   {name, secret?, widen?}   (owner key)
    POST /admin/api/connections/revoke     {name}                    (owner key)

    Secrets go in through POST only and are never echoed back.
    """
    from starlette.responses import JSONResponse

    from shared_memory import delegation as di
    from shared_memory import vault

    @mcp.custom_route("/admin/api/connections", methods=["GET"])
    async def admin_connections(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable"}, status_code=503)
        return JSONResponse({"connections": di.connections_admin(db), "audit": di.connection_audit(db, 100),
                             "vault_ready": vault.available()})

    async def _write(request, fn):
        a = _auth(request)
        if not a["ok"]:
            return JSONResponse({"error": a["error"]}, status_code=a["status"])
        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable"}, status_code=503)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        result = fn(db, f"admin-ui:{a['name']}", body)
        if result.get("status") in ("active", "revoked"):
            try:
                from shared_memory.delegation import send_notices
                db_conn = db.di_scopes.find_one({"_id": body.get("name")}) or {}
                owner = db_conn.get("requested_by")
                if owner:
                    await send_notices(db, [{"to": [owner], "category": "info", "kind": "connection_status",
                                             "subject": f"Connection {body.get('name')} is {result['status']}",
                                             "body": f"Tom set connection '{body.get('name')}' to {result['status']}. "
                                                     f"Tasks started: {result.get('tasks_started', 0)}."}])
            except Exception as e:  # notice is best-effort
                log.warning("connections: owner notice failed: %s", e)
        return JSONResponse(result, status_code=400 if result.get("error") else 200)

    @mcp.custom_route("/admin/api/connections/activate", methods=["POST"])
    async def admin_connection_activate(request):
        return await _write(request, lambda db, actor, b: di.activate_connection(
            db, actor, b.get("name") or "", b.get("secret") or None, b.get("widen") or None))

    @mcp.custom_route("/admin/api/connections/revoke", methods=["POST"])
    async def admin_connection_revoke(request):
        return await _write(request, lambda db, actor, b: di.revoke_connection(db, actor, b.get("name") or ""))
