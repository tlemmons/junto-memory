"""Tests for the MCP-layer isError coercion (shared_memory.error_flag)."""

from mcp import types as _mcp_types

from shared_memory.error_flag import (
    BARE_ERROR_PREFIX,
    _coerce_error_flag,
    build_call_tool_handler_with_error_flag,
)


def _make_request():
    return _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(name="t", arguments={}),
    )


def _server_result(text: str, *, is_error: bool = False):
    return _mcp_types.ServerResult(
        _mcp_types.CallToolResult(
            content=[_mcp_types.TextContent(type="text", text=text)],
            isError=is_error,
        )
    )


def test_coerce_flips_on_bare_error_prefix():
    r = _server_result("ERROR: Session 'x' not found.")
    _coerce_error_flag(r)
    assert r.root.isError is True


def test_coerce_leaves_success_alone():
    r = _server_result('{"status": "ok"}')
    _coerce_error_flag(r)
    assert r.root.isError is False


def test_coerce_does_not_downgrade_existing_true():
    r = _server_result("ERROR: foo", is_error=True)
    _coerce_error_flag(r)
    assert r.root.isError is True


def test_coerce_leaves_json_error_alone():
    """Only bare-string ERROR: prefix is in scope. JSON {"error": ...}
    responses are handled by their existing callers; flipping their flag
    here would be a separate, broader change."""
    r = _server_result('{"error": "Permission denied"}')
    _coerce_error_flag(r)
    assert r.root.isError is False


def test_coerce_handles_empty_content():
    r = _mcp_types.ServerResult(
        _mcp_types.CallToolResult(content=[], isError=False)
    )
    _coerce_error_flag(r)
    assert r.root.isError is False


def test_coerce_handles_non_text_first_content():
    """Image or audio content as first element must not crash the wrapper."""
    r = _mcp_types.ServerResult(
        _mcp_types.CallToolResult(
            content=[_mcp_types.ImageContent(
                type="image", data="x", mimeType="image/png"
            )],
            isError=False,
        )
    )
    _coerce_error_flag(r)
    assert r.root.isError is False


def test_coerce_handles_non_server_result():
    """A bare object missing .root should no-op, not raise."""
    class Bare:
        pass

    _coerce_error_flag(Bare())  # no raise


async def test_wrapper_passes_through_and_flips():
    async def inner(_req):
        return _server_result("ERROR: nope")

    handler = build_call_tool_handler_with_error_flag(inner)
    result = await handler(_make_request())
    assert result.root.isError is True
    assert result.root.content[0].text == "ERROR: nope"  # text untouched


async def test_wrapper_passes_through_success_unchanged():
    async def inner(_req):
        return _server_result('{"status": "ok", "id": "abc"}')

    handler = build_call_tool_handler_with_error_flag(inner)
    result = await handler(_make_request())
    assert result.root.isError is False
    assert "abc" in result.root.content[0].text


def test_bare_error_prefix_constant():
    assert BARE_ERROR_PREFIX == "ERROR:"
