"""MCP-layer isError flag coercion for bare-string error returns.

Tools across this server return rejections as plain strings starting with
"ERROR: ..." (see helpers.require_session). FastMCP wraps that string in
a CallToolResult with isError=False — the rejection text is present but
no protocol-level flag distinguishes it from a successful response.

junto-inbox's heartbeat path (and any other MCP client following
convention) reads isError to decide whether to heal a stale session. The
prior shape silently passed forever, causing the v0.0.20→v0.0.21 stuck-
session class of bugs (learning_52937f613fa09891, msg_46da77c26efd).

This wrapper flips isError=True whenever a tool returns a bare-string
response whose first text content starts with "ERROR:". It does NOT
touch the response text itself — the "ERROR: ..." prefix stays for
human readability and forward-compat with clients that already detect
both signals (junto-inbox v0.0.21 unwrapToolError, sync_engine smoke).
It does NOT inspect JSON-shaped errors (`{"error": "..."}`) — those
travel through tools whose existing callers parse the body and would
see a behavior change if isError flipped. Scope-bounded on purpose.
"""

from __future__ import annotations

BARE_ERROR_PREFIX = "ERROR:"


def _coerce_error_flag(result) -> None:
    """If result is a CallToolResult whose first text content begins with
    "ERROR:", set isError=True. No-op on any structural surprise — this
    is a defensive post-processor, never a hard failure.
    """
    root = getattr(result, "root", None)
    if root is None:
        return
    if getattr(root, "isError", None):
        return  # already flagged; do not downgrade
    content = getattr(root, "content", None)
    if not content:
        return
    first = content[0]
    text = getattr(first, "text", None)
    if not isinstance(text, str):
        return
    if text.startswith(BARE_ERROR_PREFIX):
        root.isError = True


def build_call_tool_handler_with_error_flag(orig_handler):
    """Wrap an MCP CallToolRequest handler so it post-processes the result
    to set isError=True on bare-string "ERROR:" returns.

    Mirrors the structure of build_call_tool_handler_with_intent so the
    two wrappers compose predictably in app.py.
    """

    async def wrapped(req):
        result = await orig_handler(req)
        _coerce_error_flag(result)
        return result

    return wrapped
