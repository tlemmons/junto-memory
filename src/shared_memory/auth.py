"""Authentication, RBAC, and tenant isolation.

Optional API key authentication for the MCP server. When enabled
(MCP_AUTH_ENABLED=true), all sessions require a valid API key.

Roles:
    owner    - Full access, can manage API keys and guidelines, all projects
    admin    - Can manage backlog, specs, functions, messaging, all projects
    user     - Human-driven dashboard/CLI access; broader read across projects
               and ability to send messages, but cannot manage keys or guidelines.
               Use for the future ClaudeTerminal phone/web dashboard and similar
               human-originated tooling.
    agent    - Standard agent access, scoped to assigned projects
    readonly - Query and search only, no writes

Tenant isolation:
    Each API key can be scoped to specific projects. An empty project list
    means access to all projects (for owner/admin/user roles).
"""

import hashlib
import os
import secrets
from typing import Dict, List, Optional, Tuple

from shared_memory.helpers import utc_now

# Whether auth is required (opt-in)
AUTH_ENABLED = os.getenv("MCP_AUTH_ENABLED", "").lower() in ("true", "1", "yes")

# Roles ordered by privilege level (user sits between agent and admin —
# broader cross-project read, can send messages, but no key/guideline management)
ROLES = ["readonly", "agent", "user", "admin", "owner"]

# Permission matrix: which roles can perform which operation categories
PERMISSIONS: Dict[str, List[str]] = {
    "session.start":    ["readonly", "agent", "user", "admin", "owner"],
    "session.end":      ["readonly", "agent", "user", "admin", "owner"],
    "query":            ["readonly", "agent", "user", "admin", "owner"],
    "store":            ["agent", "user", "admin", "owner"],
    "backlog":          ["agent", "user", "admin", "owner"],
    "messaging":        ["agent", "user", "admin", "owner"],
    "locking":          ["agent", "admin", "owner"],
    "functions":        ["agent", "admin", "owner"],
    "specs":            ["agent", "admin", "owner"],
    "lifecycle":        ["agent", "admin", "owner"],
    "checklists":       ["agent", "user", "admin", "owner"],
    "database":         ["agent", "user", "admin", "owner"],
    "autopilot":        ["user", "admin", "owner"],
    "guidelines":       ["admin", "owner"],
    "admin":            ["admin", "owner"],
    "admin.write":      ["owner"],
}


def _hash_key(api_key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a new random API key."""
    return f"smk_{secrets.token_urlsafe(32)}"


def validate_api_key(api_key: str) -> Optional[Dict]:
    """Validate an API key and return its record, or None if invalid.

    Returns dict with: name, role, projects, created, last_used
    """
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return None

    key_hash = _hash_key(api_key)
    record = db.api_keys.find_one({"key_hash": key_hash, "active": True})
    if not record:
        return None

    # Update last_used
    db.api_keys.update_one(
        {"key_hash": key_hash},
        {"$set": {"last_used": utc_now()}}
    )

    return {
        "name": record["name"],
        "role": record["role"],
        "projects": record.get("projects", []),
        "created": record.get("created"),
    }


def create_api_key(
    name: str,
    role: str = "agent",
    projects: Optional[List[str]] = None,
    created_by: str = "system",
) -> Tuple[str, Dict]:
    """Create a new API key. Returns (raw_key, record).

    The raw key is only returned once — store it securely.
    """
    from shared_memory.clients import get_mongo

    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {ROLES}")

    db = get_mongo()
    if db is None:
        raise RuntimeError("MongoDB not available")

    raw_key = generate_api_key()
    key_hash = _hash_key(raw_key)
    now = utc_now()

    record = {
        "key_hash": key_hash,
        "key_prefix": raw_key[:12],
        "name": name,
        "role": role,
        "projects": projects or [],
        "active": True,
        "created": now,
        "created_by": created_by,
        "last_used": None,
    }

    db.api_keys.insert_one(record)

    return raw_key, {
        "name": name,
        "role": role,
        "projects": projects or [],
        "key_prefix": raw_key[:12],
        "created": now.isoformat(),
    }


def revoke_api_key(name: str) -> bool:
    """Revoke an API key by name. Returns True if found and revoked."""
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return False

    result = db.api_keys.update_one(
        {"name": name, "active": True},
        {"$set": {"active": False, "revoked_at": utc_now()}}
    )
    return result.modified_count > 0


def list_api_keys() -> List[Dict]:
    """List all API keys (without hashes)."""
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return []

    keys = []
    for doc in db.api_keys.find({"active": True}).sort("created", 1):
        keys.append({
            "name": doc["name"],
            "role": doc["role"],
            "projects": doc.get("projects", []),
            "key_prefix": doc.get("key_prefix", ""),
            "created": doc.get("created", ""),
            "last_used": doc.get("last_used", ""),
        })
    return keys


def check_permission(role: str, permission: str) -> bool:
    """Check if a role has a specific permission."""
    allowed_roles = PERMISSIONS.get(permission, [])
    return role in allowed_roles


def check_project_access(allowed_projects: List[str], target_project: str) -> bool:
    """Check if a key has access to a specific project.

    Empty allowed_projects means access to all projects.
    """
    if not allowed_projects:
        return True  # No project restriction
    normalized = target_project.lower().replace("-", "_").replace(" ", "_")
    return normalized in [p.lower().replace("-", "_").replace(" ", "_") for p in allowed_projects]


def require_auth(session_info: Dict, permission: str, project: str = None) -> Optional[str]:
    """Check auth for a session. Returns error string or None if OK.

    When auth is disabled, always returns None (allowed).
    """
    if not AUTH_ENABLED:
        return None

    role = session_info.get("role", "agent")

    if not check_permission(role, permission):
        return (
            f"Permission denied: role '{role}' cannot perform '{permission}'. "
            f"Required roles: {PERMISSIONS.get(permission, [])}"
        )

    if project:
        allowed_projects = session_info.get("allowed_projects", [])
        if not check_project_access(allowed_projects, project):
            return (
                f"Tenant isolation: your API key does not have access to project '{project}'. "
                f"Allowed projects: {allowed_projects or 'none configured'}"
            )

    return None
