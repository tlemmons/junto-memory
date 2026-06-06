"""Tests for header-based (Authorization: Bearer) auth — design:header-auth-v0.

Covers the pure header parser and the contextvar set/get/reset roundtrip.
The end-to-end fallback (memory_start_session pulling from the contextvar) is
exercised against the live server; here we lock down the parsing + plumbing
that the middleware depends on.
"""

import shared_memory.auth as auth_mod


# ── parse_bearer_token ──

def test_parse_bearer_extracts_token():
    headers = [(b"authorization", b"Bearer smk_abc123")]
    assert auth_mod.parse_bearer_token(headers) == "smk_abc123"


def test_parse_bearer_case_insensitive_name_and_scheme():
    # ASGI lowercases header names, but be defensive; scheme casing varies.
    assert auth_mod.parse_bearer_token([(b"Authorization", b"bearer tok")]) == "tok"
    assert auth_mod.parse_bearer_token([(b"authorization", b"BEARER tok")]) == "tok"


def test_parse_bearer_strips_whitespace():
    assert auth_mod.parse_bearer_token([(b"authorization", b"Bearer   tok  ")]) == "tok"


def test_parse_bearer_absent_returns_none():
    assert auth_mod.parse_bearer_token([(b"content-type", b"application/json")]) is None
    assert auth_mod.parse_bearer_token([]) is None
    assert auth_mod.parse_bearer_token(None) is None


def test_parse_bearer_non_bearer_scheme_returns_none():
    # A non-Bearer Authorization (e.g. Basic) must not be treated as a key.
    assert auth_mod.parse_bearer_token([(b"authorization", b"Basic dXNlcjpwdw==")]) is None


def test_parse_bearer_empty_token_returns_none():
    assert auth_mod.parse_bearer_token([(b"authorization", b"Bearer ")]) is None
    assert auth_mod.parse_bearer_token([(b"authorization", b"Bearer")]) is None


# ── contextvar roundtrip ──

def test_header_api_key_default_is_none():
    assert auth_mod.get_header_api_key() is None


def test_header_api_key_set_get_reset():
    token = auth_mod.set_header_api_key("smk_xyz")
    try:
        assert auth_mod.get_header_api_key() == "smk_xyz"
    finally:
        auth_mod.reset_header_api_key(token)
    # After reset, back to the default — no leakage across requests.
    assert auth_mod.get_header_api_key() is None


# ── tunnel-origin detection (design:auth-origin-trust-v0) ──

def test_detect_tunnel_origin_true_on_cf_header():
    # cloudflared sets CF-Connecting-IP on every proxied request.
    assert auth_mod.detect_tunnel_origin([(b"cf-connecting-ip", b"203.0.113.7")]) is True


def test_detect_tunnel_origin_case_insensitive():
    assert auth_mod.detect_tunnel_origin([(b"CF-Connecting-IP", b"203.0.113.7")]) is True


def test_detect_tunnel_origin_false_for_lan_local():
    # A LAN/local direct hit carries no CF header → keyless allowed.
    assert auth_mod.detect_tunnel_origin([(b"host", b"192.168.15.240:8080")]) is False
    assert auth_mod.detect_tunnel_origin([]) is False
    assert auth_mod.detect_tunnel_origin(None) is False


def test_via_tunnel_set_get_reset():
    assert auth_mod.get_via_tunnel() is False  # default
    token = auth_mod.set_via_tunnel(True)
    try:
        assert auth_mod.get_via_tunnel() is True
    finally:
        auth_mod.reset_via_tunnel(token)
    # No leakage across requests.
    assert auth_mod.get_via_tunnel() is False
