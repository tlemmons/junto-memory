"""MCP-layer __intent_id extraction.

Clients (notably the junto-inbox channel plugin) may pass an `__intent_id`
keyword argument on any tool call. The MCP entry-point wrapper in app.py
pops the parameter from incoming arguments and stashes it on a contextvar
for the duration of the call. Phase 1 op-log writers will read it via
get_current_intent_id() and record it on the op-log payload so the central
op-log can be reconciled against a local journal (intent_id = UUID match).

Today no code reads the contextvar — the parameter is silently absorbed.
This is intentional: it lets the journaling side (inbox) ship intent-id
tagging right away without waiting for the server-side op-log to land.

See design:local-first-junto-v0-mvp §4 (op-log payload) and §8 (journal).
"""

import contextvars

INTENT_ID_KWARG = "__intent_id"

_intent_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "intent_id", default=None
)


def get_current_intent_id() -> str | None:
    """Return the __intent_id passed by the current MCP tool caller, if any."""
    return _intent_id_ctx.get()


def _set_intent_id(value: str | None) -> contextvars.Token:
    return _intent_id_ctx.set(value)


def _reset_intent_id(token: contextvars.Token) -> None:
    _intent_id_ctx.reset(token)


def build_call_tool_handler_with_intent(orig_handler):
    """Wrap an MCP CallToolRequest handler so it extracts __intent_id from
    incoming arguments into the intent_id contextvar before delegating.

    The wrapper mutates req.params.arguments in place (pop), so the inner
    handler never sees __intent_id — keeping it out of per-tool argument
    schemas. Non-string or empty values are dropped silently.
    """

    async def wrapped(req):
        intent_id: str | None = None
        args = getattr(req.params, "arguments", None)
        if isinstance(args, dict) and INTENT_ID_KWARG in args:
            candidate = args.pop(INTENT_ID_KWARG)
            if isinstance(candidate, str) and candidate:
                intent_id = candidate
        token = _set_intent_id(intent_id)
        try:
            return await orig_handler(req)
        finally:
            _reset_intent_id(token)

    return wrapped
