"""Client setup for Chroma and MongoDB connections."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import chromadb
from chromadb.config import Settings
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from shared_memory.config import (
    CHROMA_HOST,
    CHROMA_PORT,
    MONGO_DB,
    MONGO_HOST,
    MONGO_PASSWORD,
    MONGO_PORT,
    MONGO_USER,
)

# =============================================================================
# Chroma Client Setup - Uses AsyncHttpClient for proper connection management
# =============================================================================

# Global client reference (lazy initialized)
_chroma_client = None
_chroma_lock = None  # Will be created when needed

async def _get_or_create_lock():
    """Get or create the asyncio lock for client initialization."""
    global _chroma_lock
    if _chroma_lock is None:
        _chroma_lock = asyncio.Lock()
    return _chroma_lock


async def get_chroma():
    """Get the shared async Chroma client (lazy initialization).

    CRITICAL: We use AsyncHttpClient instead of HttpClient because:
    1. HttpClient creates new TCP connections per request that don't get released
    2. This causes port exhaustion and server hangs under load
    3. AsyncHttpClient properly manages connections and supports async/await

    See: https://github.com/chroma-core/chroma/issues/4296

    This uses lazy initialization to work with stateless_http mode where
    the lifespan context may not be available.
    """
    global _chroma_client

    if _chroma_client is not None:
        return _chroma_client

    lock = await _get_or_create_lock()
    async with lock:
        # Double-check after acquiring lock
        if _chroma_client is not None:
            return _chroma_client

        # Create a single AsyncHttpClient instance for the entire application lifetime
        client = await chromadb.AsyncHttpClient(
            host=CHROMA_HOST,
            port=CHROMA_PORT,
            settings=Settings(anonymized_telemetry=False)
        )
        print(f"Connected to Chroma (async) at {CHROMA_HOST}:{CHROMA_PORT}")

        # Ensure shared collections exist
        for name in ["shared_patterns", "shared_context", "shared_work"]:
            await client.get_or_create_collection(
                name=name,
                metadata={"type": "shared", "created": datetime.now(timezone.utc).isoformat()}
            )

        _chroma_client = client
        return _chroma_client


@asynccontextmanager
async def app_lifespan(app):
    """Lifespan manager for cleanup (lazy init means startup is handled by get_chroma)."""
    global _chroma_client

    # We don't initialize here anymore - get_chroma() does lazy initialization
    # This avoids issues with stateless_http mode where lifespan might not run properly
    yield {}

    # Cleanup on shutdown
    _chroma_client = None


# =============================================================================
# MongoDB Client Setup - For message queue and agent status persistence
# =============================================================================

_mongo_client = None
_mongo_db = None

def get_mongo():
    """Get the MongoDB client and database (lazy initialization).

    MongoDB is used for:
    - Message queue (persistent, supports change streams)
    - Agent status (heartbeats, current task)
    - Task lifecycle tracking

    Chroma remains the source of truth for:
    - Memories, learnings, patterns
    - Function references
    - Backlog items
    """
    global _mongo_client, _mongo_db

    if _mongo_db is not None:
        return _mongo_db

    try:
        # Build connection string with auth. authSource=admin because mongo's
        # root user (created via MONGO_INITDB_ROOT_USERNAME) lives in the admin
        # database regardless of MONGO_INITDB_DATABASE; without this, pymongo
        # defaults authSource to MONGO_DB and authentication fails.
        mongo_uri = f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}?authSource=admin"
        _mongo_client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=5000
        )
        # Verify connection
        _mongo_client.admin.command('ping')
        _mongo_db = _mongo_client[MONGO_DB]

        # Ensure indexes for messages collection
        messages = _mongo_db.messages
        messages.create_index("to_instance")
        messages.create_index("to_project")
        messages.create_index("status")
        messages.create_index("priority")
        messages.create_index([("to_instance", 1), ("to_project", 1), ("status", 1)])
        messages.create_index("created_at", expireAfterSeconds=86400 * 7)  # TTL: 7 days

        # Ensure indexes for agent_status collection
        agent_status = _mongo_db.agent_status
        agent_status.create_index("instance", unique=True)
        agent_status.create_index("last_heartbeat", expireAfterSeconds=3600)  # TTL: 1 hour stale

        # Ensure indexes for checklists collection
        checklists_col = _mongo_db.checklists
        checklists_col.create_index("project")

        # Ensure indexes for agent_directory collection (auto-populated activity tracking)
        agent_dir = _mongo_db.agent_directory
        agent_dir.create_index([("project", 1), ("instance", 1)], unique=True)
        agent_dir.create_index("project")
        agent_dir.create_index("last_seen")

        # Ensure indexes for project registry (admin-controlled)
        projects_col = _mongo_db.projects
        projects_col.create_index("name", unique=True)

        # Ensure indexes for registered agents (admin-controlled, per-project)
        reg_agents = _mongo_db.registered_agents
        reg_agents.create_index([("project", 1), ("name", 1)], unique=True)
        reg_agents.create_index("project")
        reg_agents.create_index("tier")

        # Ensure indexes for guidelines collection (server-managed agent instructions)
        guidelines_col = _mongo_db.guidelines
        guidelines_col.create_index("scope")
        guidelines_col.create_index("name", unique=True)

        # Ensure indexes for audit_log collection
        audit_col = _mongo_db.audit_log
        audit_col.create_index("event_type")
        audit_col.create_index("actor")
        audit_col.create_index("timestamp", expireAfterSeconds=86400 * 90)  # 90-day retention

        # Ensure indexes for api_keys collection (auth system)
        api_keys_col = _mongo_db.api_keys
        api_keys_col.create_index("key_hash", unique=True)
        api_keys_col.create_index("name", unique=True)

        # Compaction events collection (per ADR 822c260ccfda — v3 measurement)
        compact_col = _mongo_db.compaction_events
        compact_col.create_index([("agent", 1), ("logged_at", -1)])
        compact_col.create_index("logged_at")

        # Autopilot config — one doc per (project, agent), introduced in Phase C1
        # for ClaudeTerminal channel-plugin integration.
        autopilot_col = _mongo_db.agent_autopilot
        autopilot_col.create_index([("project", 1), ("agent", 1)], unique=True)

        # Autopilot events — Phase C2 budget enforcement. One doc per
        # auto-processed message. TTL of 1 hour means rolling-window count
        # is just a count_documents call. Mongo's TTL monitor reaps expired
        # docs every ~60s so the count is approximate at the second-by-second
        # level; that's fine for budget gating.
        autopilot_events_col = _mongo_db.autopilot_events
        autopilot_events_col.create_index(
            "logged_at", expireAfterSeconds=3600
        )
        autopilot_events_col.create_index(
            [("project", 1), ("agent", 1), ("logged_at", -1)]
        )

        # New message indexes for chain semantics — chain_depth lets us enforce
        # the depth-5 hard cap efficiently; in_response_to lets us walk threads.
        messages_col = _mongo_db.messages
        messages_col.create_index("chain_depth")
        messages_col.create_index("in_response_to")

        print(f"Connected to MongoDB at {MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}")
        return _mongo_db

    except ConnectionFailure as e:
        print(f"[MCP] MongoDB connection failed (messaging will use in-memory fallback): {e}")
        return None
