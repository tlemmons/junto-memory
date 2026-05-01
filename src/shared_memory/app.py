"""
MCP server instance and application setup.

Creates the FastMCP server instance that all tool modules register with.
"""

from mcp import types as _mcp_types
from mcp.server.fastmcp import FastMCP

from shared_memory.clients import app_lifespan

# Allow connections from any host (for remote access via IP or proxy)
# stateless_http=False (the default): persistent client sessions, required
# for resource subscriptions (Phase C2 inbox://) and progress notifications.
# Original stateless_http=True was a workaround for an older FastMCP -32602
# "request before initialization" race; verified resolved in current FastMCP
# via concurrent client harness (2026-04-26, see /tmp/mcp_stress_harness.py).
mcp = FastMCP("shared_memory", lifespan=app_lifespan, host="0.0.0.0", stateless_http=False)


# ── MCP resources.subscribe capability patch (backlog_4218136ef3ce) ──
# The lowlevel MCP server's get_capabilities() hard-codes
# resources.subscribe=False (mcp/server/lowlevel/server.py:211-212). We
# register a real subscribe_resource handler in tools/messaging.py for the
# inbox:// resource family, but without this patch the initialize handshake
# advertises subscribe=False, which causes well-behaved MCP clients (e.g.
# claudeControl) to fall back to polling instead of using the working
# subscribe path. The patch wraps the original method and overrides only
# the resources.subscribe flag, preserving everything else.
_orig_get_capabilities = mcp._mcp_server.get_capabilities

def _get_capabilities_with_subscribe(notification_options, experimental_capabilities):
    caps = _orig_get_capabilities(notification_options, experimental_capabilities)
    if caps.resources is not None and not caps.resources.subscribe:
        caps = caps.model_copy(update={
            "resources": _mcp_types.ResourcesCapability(
                subscribe=True,
                listChanged=caps.resources.listChanged,
            )
        })
    return caps

mcp._mcp_server.get_capabilities = _get_capabilities_with_subscribe


def create_app():
    """Import all tool modules to trigger @mcp.tool() registration, then return mcp."""
    # These imports trigger the @mcp.tool() decorators which register tools with mcp
    import shared_memory.tools  # noqa: F401
    return mcp
