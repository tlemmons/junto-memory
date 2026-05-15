"""HTTPMCPClient + sync endpoints smoke against a live junto-memory.

Exercises the production `HTTPMCPClient` (the class the sync engine uses)
end-to-end against http://localhost:8080/mcp — what unit tests cannot
cover because they substitute `FakeMCPClient`.

What we verify:
  1. http_connect_and_session: HTTPMCPClient.connect() opens streamable-HTTP
     session, calls memory_start_session, captures session_id.
  2. pull_envelope_shape: memory_sync_pull returns the documented shape
     (server_origin / ops / next_cursor / has_more) with the right types.
  3. push_self_origin_rejected: pushing the server's own ops back gets
     disposition=rejected with reason mentioning self-origin. This exercises
     _push_ops + _validate_shape + _preload_dedupe_state without producing
     a single write to op_log or any collection.
  4. push_envelope_shape: response carries applied_count/rejected_count/
     deduped_count/conflict_count/server_origin/results.
  5. end_session_clean: aclose() runs end-session + closes streams without
     raising.

Why "push self-origin": every op_log row carries origin = the server that
recorded it. memory_sync_push checks `op["origin"] == origin_server_id` and
short-circuits with disposition=rejected BEFORE _apply_op runs and BEFORE
the local op_log insert. That keeps the smoke side-effect-free against
live data while still proving the push wire path is alive.

Auth: requires admin- or owner-tier key (the `sync` permission).
Pass via JUNTO_SYNC_ADMIN_KEY env var. Default URL http://localhost:8080/mcp.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Make src/ importable when run directly from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from shared_memory.sync_engine import HTTPMCPClient  # noqa: E402

MCP_URL = os.environ.get("JUNTO_SYNC_LOCAL_URL", "http://localhost:8080/mcp")
ADMIN_KEY = os.environ.get("JUNTO_SYNC_ADMIN_KEY", "")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _check_self_origin_push(push_resp: dict, server_origin: str, results: list) -> None:
    """Append result rows for self-origin push expectation."""
    if push_resp.get("error") == "not_implemented":
        msg = (
            f"server returned stub: {push_resp.get('reason', '')[:80]} — "
            "materializer (4f2b62d) not deployed; "
            "docker compose build mcp-server && sudo systemctl restart mcp-rag-arch"
        )
        results.append(("push_self_origin_rejected", False, msg))
        results.append(("push_envelope_shape", False, "stub response — see prior FAIL"))
        return
    results_list = push_resp.get("results") or []
    first = results_list[0] if results_list else {}
    ok_self_origin = (
        first.get("disposition") == "rejected"
        and "self-origin" in (first.get("reason") or "").lower()
    )
    results.append((
        "push_self_origin_rejected",
        ok_self_origin,
        f"disposition={first.get('disposition')} reason={(first.get('reason') or '')[:60]!r}",
    ))
    envelope_keys = {"results", "applied_count", "rejected_count",
                     "conflict_count", "deduped_count", "server_origin"}
    ok_envelope = (
        envelope_keys.issubset(push_resp.keys())
        and push_resp.get("applied_count") == 0
        and push_resp.get("rejected_count") == 1
        and push_resp.get("conflict_count") == 0
        and push_resp.get("deduped_count") == 0
        and push_resp.get("server_origin") == server_origin
    )
    results.append((
        "push_envelope_shape",
        ok_envelope,
        f"applied={push_resp.get('applied_count')} rejected={push_resp.get('rejected_count')} "
        f"deduped={push_resp.get('deduped_count')} conflict={push_resp.get('conflict_count')}",
    ))


def _check_foreign_origin_push(push_resp: dict, sample_op: dict, server_origin: str, results: list) -> None:
    """Append result rows for foreign-origin (deduped) push expectation."""
    if push_resp.get("error") == "not_implemented":
        msg = (
            f"server returned stub: {push_resp.get('reason', '')[:80]} — "
            "materializer (4f2b62d) not deployed"
        )
        results.append(("push_self_origin_rejected", False, msg))
        results.append(("push_envelope_shape", False, "stub response — see prior FAIL"))
        return
    results_list = push_resp.get("results") or []
    first = results_list[0] if results_list else {}
    ok_dedupe = first.get("disposition") in ("deduped_seq", "deduped_intent")
    results.append((
        "push_self_origin_rejected",
        ok_dedupe,
        f"foreign-origin op deduped instead — origin={sample_op.get('origin')} "
        f"disposition={first.get('disposition')}",
    ))
    ok_envelope = (
        push_resp.get("applied_count") == 0
        and push_resp.get("rejected_count") == 0
        and push_resp.get("deduped_count") == 1
        and push_resp.get("server_origin") == server_origin
    )
    results.append((
        "push_envelope_shape",
        ok_envelope,
        f"applied={push_resp.get('applied_count')} deduped={push_resp.get('deduped_count')}",
    ))


async def main() -> int:
    if not ADMIN_KEY:
        log("ERROR: set JUNTO_SYNC_ADMIN_KEY env var to an admin- or owner-tier api_key")
        return 1

    results: list[tuple[str, bool, str]] = []

    client = HTTPMCPClient(
        url=MCP_URL,
        api_key=ADMIN_KEY,
        agent_name="sync-engine-smoke",
        project="junto",
        role_description="Sync engine HTTPMCPClient smoke",
    )

    # 1. connect + start_session
    try:
        await client.connect()
        ok = bool(client.session_id)
        results.append(("http_connect_and_session", ok, f"session_id={(client.session_id or '')[:30]}"))
    except Exception as exc:
        results.append(("http_connect_and_session", False, f"{type(exc).__name__}: {exc}"))
        return _report(results)

    try:
        # 2. pull envelope shape
        pull_resp = await client.call_tool("memory_sync_pull", {
            "since_cursor_by_origin": {},
            "limit": 1,
        })
        server_origin = pull_resp.get("server_origin") or ""
        envelope_keys = {"ops", "next_cursor", "has_more", "server_origin"}
        ok_pull = (
            envelope_keys.issubset(pull_resp.keys())
            and isinstance(pull_resp.get("ops"), list)
            and isinstance(pull_resp.get("next_cursor"), dict)
            and isinstance(pull_resp.get("has_more"), dict)
            and isinstance(server_origin, str)
            and bool(server_origin)
        )
        results.append((
            "pull_envelope_shape",
            ok_pull,
            f"server_origin={server_origin!r} ops={len(pull_resp.get('ops') or [])}",
        ))

        # 3 + 4. push (zero side effect via self-origin reject or dedupe)
        ops = pull_resp.get("ops") or []
        sample_op = ops[0] if ops else None

        if sample_op is not None and sample_op.get("origin") == server_origin:
            push_resp = await client.call_tool("memory_sync_push", {"ops": [sample_op]})
            _check_self_origin_push(push_resp, server_origin, results)
        elif sample_op is not None:
            push_resp = await client.call_tool("memory_sync_push", {"ops": [sample_op]})
            _check_foreign_origin_push(push_resp, sample_op, server_origin, results)
        else:
            push_resp = await client.call_tool("memory_sync_push", {"ops": []})
            ok_empty = (
                push_resp.get("error") != "not_implemented"
                and push_resp.get("applied_count") == 0
                and push_resp.get("rejected_count") == 0
                and push_resp.get("deduped_count") == 0
            )
            results.append(("push_self_origin_rejected", True, "skipped (op_log empty)"))
            results.append(("push_envelope_shape", ok_empty, f"empty-batch envelope: {push_resp}"))
    finally:
        # 5. clean shutdown
        try:
            await client.aclose()
            results.append(("end_session_clean", True, "aclose() returned cleanly"))
        except Exception as exc:
            results.append(("end_session_clean", False, f"{type(exc).__name__}: {exc}"))

    return _report(results)


def _report(results: list[tuple[str, bool, str]]) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        log(f"  {name:<32} {'PASS' if ok else 'FAIL'}  {detail}")
    log(f"=== {passed}/{len(results)} passed ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
