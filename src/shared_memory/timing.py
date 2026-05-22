"""Per-tool latency logging for the MCP CallToolRequest handler.

Gated by env var JUNTO_TIMING_LOG. When enabled (1/true/yes), each tool call
emits one INFO log line in greppable format:

    TIMING tool=<name> session=<session_id|none> duration_ms=<float> ok=<bool>

When disabled (default), the wrapper short-circuits to the original handler
at import time — zero overhead in the hot path.
"""

import logging
import os
import time

_TIMING_ENABLED = os.getenv("JUNTO_TIMING_LOG", "").lower() in ("1", "true", "yes")
_log = logging.getLogger("junto.timing")


def build_call_tool_handler_with_timing(orig_handler):
    """Wrap an MCP CallToolRequest handler with per-tool latency logging."""
    if not _TIMING_ENABLED:
        return orig_handler

    async def wrapped(req):
        tool_name = getattr(req.params, "name", "unknown")
        args = getattr(req.params, "arguments", None)
        session_id = args.get("session_id", "none") if isinstance(args, dict) else "none"
        start = time.monotonic()
        ok = True
        try:
            return await orig_handler(req)
        except Exception:
            ok = False
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            _log.info(
                "TIMING tool=%s session=%s duration_ms=%.1f ok=%s",
                tool_name, session_id, duration_ms, ok,
            )

    return wrapped
