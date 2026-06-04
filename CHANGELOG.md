# Changelog

All notable changes to `junto-memory` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the server advertises its running
version via `memory_health` (`server_version`).

## [Unreleased]

### Added
- **Bearer / header authentication** (`design:header-auth-v0`). When `MCP_AUTH_ENABLED=true`,
  `memory_start_session` now accepts the API key from an `Authorization: Bearer <key>` HTTP
  header in addition to the explicit `api_key` tool argument. This lets adopters configure
  auth the standard MCP way — a `headers` block in `~/.mcp.json` — instead of injecting the
  key into the system prompt. Verified end-to-end against a live Claude Code client
  (CC forwards the static header untouched; the server resolves it to the keyed role).
  - Precedence: explicit `api_key` arg wins; header is the fallback; keyless stays
    `agent`-tier; a present-but-invalid header key hard-rejects (fails loud).
  - New surface: `auth.py` `parse_bearer_token()` + `_header_api_key` contextvar;
    `auth_header_middleware` ASGI wrapper in `__main__.py`; one-line fallback in
    `tools/sessions.py`.

## Earlier

This file was introduced 2026-06-04. For history before this point, see the git log and the
in-server architecture spec (`memory_get_spec(name="architecture:shared-memory-v1")`).
