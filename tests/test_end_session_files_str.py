"""memory_end_session accepts files_modified as a string (madrox 11-15).

A park whose end_session fails argument validation never reaches the server,
so an auto-reset agent is stranded. The schema must accept a plain string.
"""

from shared_memory.app import mcp
import shared_memory.tools.sessions  # noqa: F401  (registers the tool)


def _args_model():
    return mcp._tool_manager.get_tool("memory_end_session").fn_metadata.arg_model


def test_string_files_modified_passes_validation():
    m = _args_model().model_validate(
        {"session_id": "s", "summary": "x", "files_modified": "CT217:/root/a.service (new)"})
    assert m.files_modified == "CT217:/root/a.service (new)"


def test_list_files_modified_still_passes():
    m = _args_model().model_validate({"session_id": "s", "summary": "x", "files_modified": ["a", "b"]})
    assert m.files_modified == ["a", "b"]
