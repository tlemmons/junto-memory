"""
Per-route tool-surface profiles.

Two profiles:

- "full"       (default) — every registered tool is exposed. Reached at /mcp.
- "discussion" — the subset useful for discussion/planning clients (claude.ai
                 desktop, etc.) that pay the full schema cost upfront because
                 they lack Tool Search. Reached at /mcp/discussion.

The /mcp/discussion route is opt-in: an ASGI middleware in __main__.py rewrites
the scope path and sets `current_profile` to "discussion" for the duration of
the request. The list_tools handler in app.py reads this contextvar and
filters the returned tool list.

Background: backlog_0f6b4e4332a0. Primary driver is billable API-rate cost on
desktop-to-junto sessions — full ~50-tool schema burns ~15-25K tokens per
session for tools the client won't exercise.
"""

from contextvars import ContextVar

current_profile: ContextVar[str] = ContextVar("junto_tool_profile", default="full")


DISCUSSION_PROFILE_TOOLS: frozenset[str] = frozenset({
    "memory_start_session",
    "memory_query",
    "memory_get_by_id",
    "memory_get_spec",
    "memory_list_specs",
    "memory_search_global",
    "memory_list_projects",
    "memory_send_message",
    "memory_get_messages",
    "memory_acknowledge_message",
    "memory_store",
    "memory_define_spec",
    "memory_health",
})
