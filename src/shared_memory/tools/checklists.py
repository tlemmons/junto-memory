"""Checklist tools - shared checklists for coordination."""

import json
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import normalize_project, require_session, utc_now
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_checklist(
    session_id: str,
    action: str,
    name: str,
    project: str = None,
    items: List[str] = None,
    item_index: int = None,
    done: bool = None,
    notes: str = None,
    ctx: Context = None
) -> str:
    """
    Lightweight checklists for launch readiness, deploy steps, etc.

    One tool, multiple actions:

    action="create" - Create a new checklist
        Required: name, items (list of strings)
        Optional: project (defaults to session project)

    action="get" - View a checklist with status
        Required: name
        Optional: project

    action="add" - Append items to an existing checklist
        Required: name, items (list of strings)

    action="check" - Check/uncheck an item
        Required: name, item_index (0-based), done (true/false)
        Optional: notes (add context about completion)

    action="delete" - Delete a checklist
        Required: name

    action="list" - List all checklists for a project
        Optional: project (defaults to session project)

    Args:
        session_id: Your session ID
        action: One of: create, get, add, check, delete, list
        name: Checklist name (e.g., "meural-beta-launch")
        project: Project scope (defaults to session project)
        items: List of item strings (for create/add)
        item_index: 0-based index of item to check (for check)
        done: True/False to check/uncheck (for check)
        notes: Optional notes when checking an item
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    target_project = normalize_project(project or session_info.get("project", ""))
    who = session_info.get("claude_instance", "unknown")

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    checklists = db.checklists
    doc_id = f"{target_project}:{name}"

    # -- CREATE --
    if action == "create":
        if not items:
            return json.dumps({"error": "items required for create (list of strings)"})

        existing = checklists.find_one({"_id": doc_id})
        if existing:
            return json.dumps({"error": f"Checklist '{name}' already exists in {target_project}. Use action='add' to append items."})

        now = utc_now()
        checklist_items = []
        for text in items:
            checklist_items.append({
                "text": text,
                "done": False,
                "checked_by": None,
                "checked_at": None,
                "notes": ""
            })

        checklists.insert_one({
            "_id": doc_id,
            "name": name,
            "project": target_project,
            "created_by": who,
            "created_at": now,
            "updated_at": now,
            "items": checklist_items
        })

        return json.dumps({
            "status": "created",
            "name": name,
            "project": target_project,
            "item_count": len(checklist_items)
        })

    # -- GET --
    elif action == "get":
        doc = checklists.find_one({"_id": doc_id})
        if not doc:
            return json.dumps({"error": f"Checklist '{name}' not found in {target_project}"})

        total = len(doc["items"])
        checked = sum(1 for i in doc["items"] if i["done"])

        formatted_items = []
        for idx, item in enumerate(doc["items"]):
            entry = {
                "index": idx,
                "text": item["text"],
                "done": item["done"],
            }
            if item.get("checked_by"):
                entry["checked_by"] = item["checked_by"]
            if item.get("checked_at"):
                entry["checked_at"] = item["checked_at"].isoformat() if hasattr(item["checked_at"], "isoformat") else str(item["checked_at"])
            if item.get("notes"):
                entry["notes"] = item["notes"]
            formatted_items.append(entry)

        return json.dumps({
            "name": name,
            "project": target_project,
            "progress": f"{checked}/{total}",
            "created_by": doc.get("created_by"),
            "items": formatted_items
        }, indent=2)

    # -- ADD --
    elif action == "add":
        if not items:
            return json.dumps({"error": "items required for add (list of strings)"})

        doc = checklists.find_one({"_id": doc_id})
        if not doc:
            return json.dumps({"error": f"Checklist '{name}' not found in {target_project}. Use action='create' first."})

        new_items = []
        for text in items:
            new_items.append({
                "text": text,
                "done": False,
                "checked_by": None,
                "checked_at": None,
                "notes": ""
            })

        checklists.update_one(
            {"_id": doc_id},
            {
                "$push": {"items": {"$each": new_items}},
                "$set": {"updated_at": utc_now()}
            }
        )

        return json.dumps({
            "status": "added",
            "name": name,
            "added_count": len(new_items),
            "new_total": len(doc["items"]) + len(new_items)
        })

    # -- CHECK --
    elif action == "check":
        if item_index is None or done is None:
            return json.dumps({"error": "item_index and done required for check"})

        doc = checklists.find_one({"_id": doc_id})
        if not doc:
            return json.dumps({"error": f"Checklist '{name}' not found in {target_project}"})

        if item_index < 0 or item_index >= len(doc["items"]):
            return json.dumps({"error": f"item_index {item_index} out of range (0-{len(doc['items'])-1})"})

        update_fields = {
            f"items.{item_index}.done": done,
            f"items.{item_index}.checked_by": who if done else None,
            f"items.{item_index}.checked_at": utc_now() if done else None,
            "updated_at": utc_now()
        }
        if notes is not None:
            update_fields[f"items.{item_index}.notes"] = notes

        checklists.update_one({"_id": doc_id}, {"$set": update_fields})

        item_text = doc["items"][item_index]["text"]
        total = len(doc["items"])
        # Recalculate checked count with this change
        checked = sum(1 for i in doc["items"] if i["done"])
        checked = checked + (1 if done and not doc["items"][item_index]["done"] else 0)
        checked = checked - (1 if not done and doc["items"][item_index]["done"] else 0)

        return json.dumps({
            "status": "checked" if done else "unchecked",
            "item": item_text,
            "progress": f"{checked}/{total}",
            "checked_by": who if done else None
        })

    # -- DELETE --
    elif action == "delete":
        result = checklists.delete_one({"_id": doc_id})
        if result.deleted_count == 0:
            return json.dumps({"error": f"Checklist '{name}' not found in {target_project}"})
        return json.dumps({"status": "deleted", "name": name, "project": target_project})

    # -- LIST --
    elif action == "list":
        cursor = checklists.find({"project": target_project}).sort("updated_at", -1)
        results = []
        for doc in cursor:
            total = len(doc["items"])
            checked = sum(1 for i in doc["items"] if i["done"])
            results.append({
                "name": doc["name"],
                "progress": f"{checked}/{total}",
                "created_by": doc.get("created_by"),
                "updated_at": doc["updated_at"].isoformat() if doc.get("updated_at") else None
            })

        return json.dumps({
            "project": target_project,
            "count": len(results),
            "checklists": results
        }, indent=2)

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Must be one of: create, get, add, check, delete, list"})
