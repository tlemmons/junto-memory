"""Tests for the MCP-layer __intent_id extraction (shared_memory.intent)."""

from mcp import types as _mcp_types

from shared_memory.intent import (
    INTENT_ID_KWARG,
    build_call_tool_handler_with_intent,
    get_current_intent_id,
)


def _make_request(arguments):
    return _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(name="t", arguments=arguments),
    )


async def _capture(observed):
    """Return a fake inner handler that records what it saw."""

    async def inner(req):
        observed["intent_id_in_call"] = get_current_intent_id()
        observed["args_in_call"] = dict(req.params.arguments or {})
        return "ok"

    return inner


async def test_intent_id_absent_is_none():
    observed: dict = {}
    inner = await _capture(observed)
    handler = build_call_tool_handler_with_intent(inner)
    await handler(_make_request({"foo": "bar"}))
    assert observed["intent_id_in_call"] is None
    assert observed["args_in_call"] == {"foo": "bar"}


async def test_intent_id_visible_and_stripped():
    observed: dict = {}
    inner = await _capture(observed)
    handler = build_call_tool_handler_with_intent(inner)
    await handler(_make_request({"foo": "bar", INTENT_ID_KWARG: "uuid-xyz"}))
    assert observed["intent_id_in_call"] == "uuid-xyz"
    assert INTENT_ID_KWARG not in observed["args_in_call"]
    assert observed["args_in_call"] == {"foo": "bar"}


async def test_intent_id_reset_after_call():
    observed: dict = {}
    inner = await _capture(observed)
    handler = build_call_tool_handler_with_intent(inner)
    await handler(_make_request({INTENT_ID_KWARG: "uuid-abc"}))
    # Leaking the contextvar would contaminate later calls — must be reset.
    assert get_current_intent_id() is None


async def test_intent_id_non_string_dropped():
    observed: dict = {}
    inner = await _capture(observed)
    handler = build_call_tool_handler_with_intent(inner)
    await handler(_make_request({"foo": "bar", INTENT_ID_KWARG: 12345}))
    assert observed["intent_id_in_call"] is None
    assert INTENT_ID_KWARG not in observed["args_in_call"]
    assert observed["args_in_call"] == {"foo": "bar"}


async def test_intent_id_empty_string_dropped():
    observed: dict = {}
    inner = await _capture(observed)
    handler = build_call_tool_handler_with_intent(inner)
    await handler(_make_request({INTENT_ID_KWARG: ""}))
    assert observed["intent_id_in_call"] is None


async def test_intent_id_reset_after_inner_raises():
    """If the inner handler raises, the contextvar must still be reset."""
    async def boom(_req):
        raise RuntimeError("inner failure")

    handler = build_call_tool_handler_with_intent(boom)
    raised = False
    try:
        await handler(_make_request({INTENT_ID_KWARG: "uuid-fail"}))
    except RuntimeError:
        raised = True
    assert raised
    assert get_current_intent_id() is None


async def test_intent_id_none_arguments():
    """Tools called with arguments=None must not crash the wrapper."""
    observed: dict = {}
    inner = await _capture(observed)
    handler = build_call_tool_handler_with_intent(inner)
    await handler(_make_request(None))
    assert observed["intent_id_in_call"] is None
    assert observed["args_in_call"] == {}
