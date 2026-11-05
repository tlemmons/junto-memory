"""Backlog: a Jira-lite web page for humans (interface:backlog-ideas-comments-v0 §4).

GET  /backlog                  the page (backlog.html)
GET  /backlog/api/items        list (filters: project, status, priority, assigned_to, tag,
                               include_done, include_ideas); text search is client-side
GET  /backlog/api/item?id=     one item + its comments
POST /backlog/api/create       new item (status open|idea)              (owner key)
POST /backlog/api/update       edit fields                              (owner key)
POST /backlog/api/comment      add a comment, optionally notify assignee (owner key)

Gates match /admin: tunnel origins are refused; reads use _lan_only, writes need
an OWNER-role Bearer key or an owner's login cookie (ui_auth.py). Every read and write goes through the MCP tool
functions under a principal session (never on rosters), so redaction,
write-lint, op-log and tenant isolation are exactly what agents get. Writes are
attributed to the key's name; comments from here carry author_kind="human".
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from shared_memory.admin_ui import _auth, _lan_only, _tunnel_only

log = logging.getLogger(__name__)

BACKLOG_HTML = Path(__file__).parent / "backlog.html"
READER_SESSION = "web_backlog_reader"
FIELDS = ("title", "description", "priority", "assigned_to", "target_version", "deferred_reason", "tags")


def _ensure_session(session_id: str, name: str, role: str, projects, author_kind: str) -> str:
    """Register (or refresh) a principal session for the page's tool calls."""
    from shared_memory.helpers import utc_now_iso
    from shared_memory.state import active_sessions

    if session_id not in active_sessions:
        active_sessions[session_id] = {
            "project": "junto", "claude_instance": name, "task": "backlog web page",
            "started": utc_now_iso(), "last_activity": utc_now_iso(),
            "blocked_by": None, "blocked_reason": None, "waiting_for_signal": None,
            "tmux_target": None, "role": role, "allowed_projects": list(projects or []),
            "is_principal": True, "author_kind": author_kind,
        }
    else:
        # Role/scope can change under a live server (a user's role edited, a key re-scoped).
        active_sessions[session_id].update({"role": role, "allowed_projects": list(projects or []),
                                            "last_activity": utc_now_iso()})
    return session_id


def _reader() -> str:
    return _ensure_session(READER_SESSION, "backlog-web", "owner", [], "human")


def _writer(a: Dict[str, Any], request) -> str:
    """The caller's own session: name, role and project scope from _auth (key or login;
    a scoped key or user stays scoped)."""
    name = a.get("name") or "human"
    return _ensure_session(f"web_backlog_{a.get('via') or 'key'}_{name}", name, a.get("role") or "owner",
                           a.get("projects") or [], "human")


def _parse(out: str) -> Dict[str, Any]:
    """Tool output → dict. Tools return JSON, or a bare 'ERROR: ...' string."""
    if isinstance(out, str) and out.startswith("ERROR"):
        return {"error": out}
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return {"error": f"unexpected tool output: {str(out)[:200]}"}


def _bool(v: Optional[str]) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def register_backlog_routes(mcp) -> None:
    from starlette.responses import HTMLResponse, JSONResponse

    from shared_memory.tools import backlog as bt
    from shared_memory.tools import messaging as mt
    from shared_memory.tools import query as qt

    def _resp(d: Dict[str, Any]):
        return JSONResponse(d, status_code=400 if d.get("error") else 200)

    async def _body(request) -> Optional[Dict[str, Any]]:
        try:
            b = json.loads(await request.body() or b"{}")
            return b if isinstance(b, dict) else None
        except ValueError:
            return None

    def _owner(request):
        a = _auth(request)
        if not a["ok"]:
            return None, JSONResponse({"error": a["error"]}, status_code=a["status"])
        return a, None

    @mcp.custom_route("/backlog", methods=["GET"])
    async def backlog_page(request):
        blocked = _tunnel_only(request)
        if blocked:
            return blocked
        try:
            return HTMLResponse(BACKLOG_HTML.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return HTMLResponse("<h1>backlog.html missing</h1>", status_code=500)

    @mcp.custom_route("/backlog/api/items", methods=["GET"])
    async def backlog_items(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        p = request.query_params
        tag = p.get("tag") or None
        out = _parse(await bt.memory_list_backlog(
            _reader(), project=p.get("project") or None, status=p.get("status") or None,
            priority=p.get("priority") or None, assigned_to=p.get("assigned_to") or None,
            tags=[tag] if tag else None, include_done=_bool(p.get("include_done")),
            include_ideas=_bool(p.get("include_ideas")), limit=0))
        return _resp(out)

    @mcp.custom_route("/backlog/api/item", methods=["GET"])
    async def backlog_item(request):
        blocked = _lan_only(request)
        if blocked:
            return blocked
        item_id = request.query_params.get("id") or ""
        if not item_id.startswith("backlog_"):
            return JSONResponse({"error": "id must be a backlog_* id"}, status_code=400)
        out = _parse(await qt.memory_get_by_id(_reader(), item_id))
        if not out.get("found") and not out.get("error"):
            out["error"] = f"not found: {item_id}"
        return _resp(out)

    @mcp.custom_route("/backlog/api/create", methods=["POST"])
    async def backlog_create(request):
        a, err = _owner(request)
        if err:
            return err
        b = await _body(request)
        if b is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        if not (b.get("title") or "").strip():
            return JSONResponse({"error": "title is required"}, status_code=400)
        out = _parse(await bt.memory_add_backlog_item(
            _writer(a, request), title=b["title"].strip(), description=b.get("description") or "",
            priority=b.get("priority") or "medium", project=b.get("project") or None,
            assigned_to=b.get("assigned_to") or None, tags=b.get("tags") or [],
            target_version=b.get("target_version") or None, status=b.get("status") or "open"))
        return _resp(out)

    @mcp.custom_route("/backlog/api/update", methods=["POST"])
    async def backlog_update(request):
        a, err = _owner(request)
        if err:
            return err
        b = await _body(request)
        if b is None or not b.get("id"):
            return JSONResponse({"error": "body needs id"}, status_code=400)
        kw = {k: b[k] for k in FIELDS if k in b}
        if "status" in b:
            kw["status"] = b["status"]
        if "project" in b and b["project"]:
            kw["project"] = b["project"]
        if not kw:
            return JSONResponse({"error": "nothing to update"}, status_code=400)
        out = _parse(await bt.memory_update_backlog_item(_writer(a, request), b["id"], **kw))
        return _resp(out)

    @mcp.custom_route("/backlog/api/comment", methods=["POST"])
    async def backlog_comment(request):
        a, err = _owner(request)
        if err:
            return err
        b = await _body(request)
        if b is None or not b.get("id") or not (b.get("body") or "").strip():
            return JSONResponse({"error": "body needs id and body"}, status_code=400)
        sid = _writer(a, request)
        out = _parse(await bt.memory_update_backlog_item(sid, b["id"], comment=b["body"]))
        if out.get("error") or out.get("comment_error"):
            return JSONResponse({"error": out.get("error") or out.get("comment_error")}, status_code=400)
        if b.get("notify") and out.get("assigned_to"):
            snippet = b["body"].strip().replace("\n", " ")
            snippet = snippet[:300] + ("…" if len(snippet) > 300 else "")
            msg = (f"{a.get('name')} commented on {b['id']} \"{out.get('title')}\": {snippet}\n"
                   f"Full thread: memory_get_by_id(\"{b['id']}\"). Reply with "
                   f"memory_update_backlog_item(item_id=\"{b['id']}\", comment=...).")
            sent = _parse(await mt.memory_send_message(
                sid, to_instance=out["assigned_to"], message=msg, category="info",
                to_project=out.get("project") or None, human_interacted=True,
                subject=f"Comment on backlog: {out.get('title')}"))
            out["notified"] = {"to": out["assigned_to"], "message_id": sent.get("message_id"),
                               "error": sent.get("error")}
        return _resp(out)
