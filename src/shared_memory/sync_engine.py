"""Peer-side sync engine — daemon that drives pull-then-push between a
junto-memory peer and its primary.

Implements `design:local-first-junto-v0-mvp` v0.5.0 §5 + §8 Phase 2.

Architecture
------------

The engine is a standalone process co-located with a peer junto-memory.
It maintains two MCP HTTP sessions (one to the primary, one to the local
peer) and orchestrates replication purely via the server-side endpoints
`memory_sync_pull` + `memory_sync_push`. The engine has no DB access of
its own — both endpoints handle storage, dedupe, and validation.

Per loop iteration:
    1. Pull from primary: `primary.memory_sync_pull(since=cursors["pull"])`.
       Apply returned ops to local via `local.memory_sync_push(ops)`.
       Drain `has_more`.
    2. Push to primary: read local op_log for ops above the
       last-pushed-cursor via `local.memory_sync_pull(since={local_origin: X})`.
       Send via `primary.memory_sync_push(ops)`. Drain `has_more`.
    3. Sleep `pull_interval ± jitter` seconds.
    4. On transport error: SyncEngine catches `_TransportError` and applies
       per-iteration exponential backoff with the SAME client. This works
       for transient blips. For persistent failures (session terminated,
       DNS failure causing CancelledError) the supervisor in `_amain`
       discards the dead client and rebuilds.

v1 simplifications (per state:memory and §5.2):
    - Always runs the MQTT-offline cadence (10s pull, 5s push).
    - No MQTT-eager triggers — Phase 2.5.
    - Single shared interval for v1 (10s); separate push cadence is a
      v2 optimization.

Cursor persistence
------------------

Cursors live in a JSON file (default `~/.junto/sync-cursors.json`):

    {
      "pull": {origin: last_seen_seq, ...},
      "push": {local_origin: last_pushed_seq}
    }

Saved atomically after every successful batch. Loss of the file is
recoverable: re-delivery of already-applied ops is idempotent via the
`(origin, seq)` unique index and intent_id dedupe on the receiving side.

Auth
----

Both connections require admin- or owner-tier credentials (the `sync`
auth permission on the server). Set via env vars:

    JUNTO_SYNC_PRIMARY_URL   (default http://primary:8080/mcp)
    JUNTO_SYNC_PRIMARY_KEY
    JUNTO_SYNC_LOCAL_URL     (default http://localhost:8080/mcp)
    JUNTO_SYNC_LOCAL_KEY
    JUNTO_SYNC_CURSOR_PATH   (default ~/.junto/sync-cursors.json)
    JUNTO_SYNC_PULL_INTERVAL (default 10.0)
    JUNTO_SYNC_PUSH_INTERVAL (default 5.0)
    JUNTO_SYNC_PULL_JITTER   (default 3.0)
    JUNTO_SYNC_PUSH_JITTER   (default 2.0)
    JUNTO_SYNC_LIMIT         (default 500)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

logger = logging.getLogger("junto.sync_engine")


DEFAULT_PULL_INTERVAL = 10.0
DEFAULT_PUSH_INTERVAL = 5.0
DEFAULT_PULL_JITTER = 3.0
DEFAULT_PUSH_JITTER = 2.0
DEFAULT_LIMIT = 500
DEFAULT_BACKOFF_MIN = 5.0
DEFAULT_BACKOFF_MAX = 300.0
DEFAULT_CURSOR_PATH = "~/.junto/sync-cursors.json"


class MCPClient(Protocol):
    """Minimal interface the engine needs from an MCP client.

    Production class is `HTTPMCPClient`; tests provide fakes.
    """

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        ...


# ───────────────────────────────────────────────────────────────────
# HTTP MCP client (production)
# ───────────────────────────────────────────────────────────────────


class HTTPMCPClient:
    """Streamable-HTTP MCP client with session lifecycle + auth.

    Holds an open MCP session for the engine's lifetime. Does NOT
    reconnect on transport errors — once the underlying streamable_http
    cancel scope poisons (DNS failure, mid-stream connection loss),
    this instance must be discarded and a new one built. The supervisor
    in `_amain` handles that lifecycle; do not try to call `connect()`
    again on a poisoned instance. `call_tool` returns the parsed
    JSON-decoded body of the first content block (matches every
    junto-memory tool's `return json.dumps(...)` convention).

    `connect()` claims a server-side `session_id` via
    `memory_start_session`, which the engine then threads on every
    subsequent call.
    """

    def __init__(
        self,
        url: str,
        api_key: Optional[str] = None,
        agent_name: str = "sync-engine",
        project: str = "junto",
        role_description: str = "Peer-side sync engine — replication daemon",
    ):
        self.url = url
        self.api_key = api_key
        self.agent_name = agent_name
        self.project = project
        self.role_description = role_description
        self.session_id: Optional[str] = None
        self.server_origin: Optional[str] = None
        self._client_ctx = None
        self._session = None
        self._streams = None

    async def connect(self) -> None:
        """Open MCP session + register engine identity via start_session."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._client_ctx = streamablehttp_client(self.url)
        read, write, _gid = await self._client_ctx.__aenter__()
        self._streams = (read, write)
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

        args: Dict[str, Any] = {
            "project": self.project,
            "claude_instance": self.agent_name,
            "role_description": self.role_description,
        }
        if self.api_key is not None:
            args["api_key"] = self.api_key
        resp = await self._raw_call("memory_start_session", args)
        if "error" in resp:
            raise RuntimeError(
                f"memory_start_session failed against {self.url}: {resp['error']}"
            )
        self.session_id = resp.get("session_id")
        if not self.session_id:
            raise RuntimeError(
                f"memory_start_session returned no session_id from {self.url}: {resp}"
            )

    async def aclose(self) -> None:
        """Best-effort end-session + clean teardown of MCP streams."""
        if self._session is not None and self.session_id:
            try:
                await self._raw_call(
                    "memory_end_session",
                    {"session_id": self.session_id, "summary": "sync-engine shutdown"},
                )
            except Exception:
                logger.warning("memory_end_session failed during shutdown", exc_info=True)
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                logger.debug("ClientSession close failed", exc_info=True)
            self._session = None
        if self._client_ctx is not None:
            try:
                await self._client_ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("streamablehttp_client close failed", exc_info=True)
            self._client_ctx = None

    async def _raw_call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Underlying MCP call_tool wrapper, no session_id injection."""
        if self._session is None:
            raise RuntimeError("HTTPMCPClient.connect() not yet called")
        result = await self._session.call_tool(name, args)
        if not result.content:
            return {}
        text = result.content[0].text  # type: ignore[union-attr]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Tool call with session_id injected from start_session."""
        if not self.session_id:
            raise RuntimeError("HTTPMCPClient not connected; call connect() first")
        full_args = dict(args)
        full_args.setdefault("session_id", self.session_id)
        return await self._raw_call(name, full_args)


# ───────────────────────────────────────────────────────────────────
# Cursor persistence
# ───────────────────────────────────────────────────────────────────


def _empty_cursor_state() -> Dict[str, Dict[str, int]]:
    return {"pull": {}, "push": {}}


def load_cursors(path: Path) -> Dict[str, Dict[str, int]]:
    """Read cursor file or return empty state if missing/unreadable.

    Defensive: a corrupt cursor file does not crash the engine — it
    resets to empty (which is safe; the receiving side is idempotent).
    Logs loudly on malformed JSON so operators see the regression.
    """
    if not path.exists():
        return _empty_cursor_state()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.error(
            "cursor file %s is corrupt JSON; resetting to empty", path
        )
        return _empty_cursor_state()
    if not isinstance(data, dict):
        return _empty_cursor_state()
    pull = data.get("pull")
    push = data.get("push")
    return {
        "pull": dict(pull) if isinstance(pull, dict) else {},
        "push": dict(push) if isinstance(push, dict) else {},
    }


def save_cursors(path: Path, cursors: Dict[str, Dict[str, int]]) -> None:
    """Atomic write via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _merge_pull_cursor(
    existing: Dict[str, int], next_cursor: Dict[str, int]
) -> Dict[str, int]:
    """Cumulative merge: keep origins not returned this batch at prior seq."""
    merged = dict(existing)
    for origin, seq in next_cursor.items():
        merged[origin] = int(seq)
    return merged


# ───────────────────────────────────────────────────────────────────
# Sync engine core
# ───────────────────────────────────────────────────────────────────


class SyncEngine:
    """Pull-then-push replication loop driven over MCP."""

    def __init__(
        self,
        *,
        primary: MCPClient,
        local: MCPClient,
        cursor_path: Path,
        pull_interval: float = DEFAULT_PULL_INTERVAL,
        push_interval: float = DEFAULT_PUSH_INTERVAL,
        pull_jitter: float = DEFAULT_PULL_JITTER,
        push_jitter: float = DEFAULT_PUSH_JITTER,
        limit: int = DEFAULT_LIMIT,
        backoff_min: float = DEFAULT_BACKOFF_MIN,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        clock: callable = time.monotonic,
        rng: random.Random = None,
        sleep: callable = None,
    ):
        self.primary = primary
        self.local = local
        self.cursor_path = cursor_path
        self.pull_interval = float(pull_interval)
        self.push_interval = float(push_interval)
        self.pull_jitter = float(pull_jitter)
        self.push_jitter = float(push_jitter)
        self.limit = int(limit)
        self.backoff_min = float(backoff_min)
        self.backoff_max = float(backoff_max)
        self._clock = clock
        self._rng = rng or random.Random()
        self._sleep = sleep or asyncio.sleep
        self.cursors: Dict[str, Dict[str, int]] = load_cursors(cursor_path)
        self.local_origin: Optional[str] = None
        # Stats counters surface in logs and tests. `reconnects` lives on
        # the supervisor (in _amain), not here — an individual SyncEngine
        # instance is born after a reconnect and dies before the next one.
        self.stats = {
            "iterations": 0,
            "pulled": 0,
            "pushed": 0,
            "errors": 0,
        }

    # ─── public ───────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop. Returns when `stop_event` is set."""
        backoff = self.backoff_min
        while not stop_event.is_set():
            try:
                pulled, pushed = await self._pull_then_push()
                self.stats["iterations"] += 1
                self.stats["pulled"] += pulled
                self.stats["pushed"] += pushed
                backoff = self.backoff_min  # success resets backoff
                delay = self._jitter(self.pull_interval, self.pull_jitter)
            except _TransportError as e:
                self.stats["errors"] += 1
                logger.warning(
                    "transport error in pull-then-push: %s — sleeping %.1fs",
                    e, backoff,
                )
                delay = backoff
                backoff = min(self.backoff_max, backoff * 2)
            except Exception:
                self.stats["errors"] += 1
                logger.exception(
                    "unexpected error in pull-then-push — sleeping %.1fs",
                    backoff,
                )
                delay = backoff
                backoff = min(self.backoff_max, backoff * 2)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                # stop_event fired during sleep
                return
            except asyncio.TimeoutError:
                continue

    # ─── private ──────────────────────────────────────────────────

    async def _pull_then_push(self) -> Tuple[int, int]:
        pulled = await self._pull_loop()
        pushed = await self._push_loop()
        return pulled, pushed

    async def _pull_loop(self) -> int:
        """Drain primary → local until has_more is false everywhere."""
        total = 0
        for _ in range(MAX_DRAIN_PAGES):
            resp = await self._call(
                self.primary,
                "memory_sync_pull",
                {
                    "since_cursor_by_origin": dict(self.cursors["pull"]),
                    "limit": self.limit,
                },
            )
            ops = resp.get("ops") or []
            next_cursor = resp.get("next_cursor") or {}
            has_more = resp.get("has_more") or {}
            if not ops:
                break
            apply_resp = await self._call(
                self.local, "memory_sync_push", {"ops": ops}
            )
            self._log_dispositions("pull-apply", apply_resp)
            self.cursors["pull"] = _merge_pull_cursor(
                self.cursors["pull"], next_cursor
            )
            self._save()
            total += len(ops)
            if not any(has_more.values()):
                break
        return total

    async def _push_loop(self) -> int:
        """Drain local own-origin → primary until has_more is false."""
        if self.local_origin is None:
            self.local_origin = await self._discover_local_origin()
        local_origin = self.local_origin

        total = 0
        for _ in range(MAX_DRAIN_PAGES):
            cur = int(self.cursors["push"].get(local_origin, 0))
            resp = await self._call(
                self.local,
                "memory_sync_pull",
                {
                    "since_cursor_by_origin": {local_origin: cur},
                    "limit": self.limit,
                },
            )
            ops = resp.get("ops") or []
            next_cursor = resp.get("next_cursor") or {}
            has_more = resp.get("has_more") or {}
            # Local sync_pull will scan all known origins; filter to ours
            # so we never re-push primary's own ops or another peer's ops
            # back to primary.
            own_ops = [op for op in ops if op.get("origin") == local_origin]
            if not own_ops:
                # No ops for our origin this page — but other origins may
                # have advanced. Don't update our push cursor based on
                # other origins' cursors.
                if not has_more.get(local_origin):
                    break
                # has_more for our origin but page contained zero own ops
                # (other origins filled it). Use other origins' returned
                # cursors to make progress, but bump our cursor only by
                # what we actually saw. Defensive: avoid infinite loop.
                advance = next_cursor.get(local_origin)
                if advance is None:
                    break
                self.cursors["push"][local_origin] = int(advance)
                self._save()
                continue
            push_resp = await self._call(
                self.primary, "memory_sync_push", {"ops": own_ops}
            )
            self._log_dispositions("push-send", push_resp)
            max_seq = max(int(op["seq"]) for op in own_ops)
            self.cursors["push"][local_origin] = max_seq
            self._save()
            total += len(own_ops)
            if not has_more.get(local_origin):
                break
        return total

    async def _discover_local_origin(self) -> str:
        """Use memory_sync_pull's server_origin field to learn local id.

        Empty pull (cursor={}, limit=1) is cheap and definitive — the
        endpoint always returns `server_origin` in its envelope.
        """
        resp = await self._call(
            self.local,
            "memory_sync_pull",
            {"since_cursor_by_origin": {}, "limit": 1},
        )
        origin = resp.get("server_origin")
        if not origin:
            raise _TransportError(
                f"local memory_sync_pull returned no server_origin: {resp}"
            )
        return origin

    async def _call(
        self, client: MCPClient, name: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Single call wrapper that raises _TransportError on failure."""
        try:
            resp = await client.call_tool(name, args)
        except Exception as e:
            raise _TransportError(f"{name} call failed: {e}") from e
        if not isinstance(resp, dict):
            raise _TransportError(
                f"{name} returned non-dict: {type(resp).__name__}"
            )
        if "error" in resp:
            raise _TransportError(f"{name} error: {resp['error']}")
        return resp

    def _log_dispositions(self, label: str, push_resp: Dict[str, Any]) -> None:
        applied = push_resp.get("applied_count", 0)
        rejected = push_resp.get("rejected_count", 0)
        conflict = push_resp.get("conflict_count", 0)
        deduped = push_resp.get("deduped_count", 0)
        if rejected or conflict:
            logger.warning(
                "%s applied=%d rejected=%d conflict=%d deduped=%d",
                label, applied, rejected, conflict, deduped,
            )
        else:
            logger.info(
                "%s applied=%d deduped=%d", label, applied, deduped,
            )

    def _jitter(self, base: float, jitter: float) -> float:
        if jitter <= 0:
            return base
        return base + self._rng.uniform(0, jitter)

    def _save(self) -> None:
        save_cursors(self.cursor_path, self.cursors)


MAX_DRAIN_PAGES = 200  # safety: each page is `limit` ops; 200 × 500 = 100k


class _TransportError(RuntimeError):
    """Raised by _call to trigger backoff-retry in the main loop."""


# ───────────────────────────────────────────────────────────────────
# CLI entrypoint
# ───────────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("env %s=%r is not a float; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env %s=%r is not an int; using default %s", name, raw, default)
        return default


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="junto-sync-engine",
        description="Peer-side sync engine for junto-memory replication.",
    )
    p.add_argument("--primary-url", default=os.environ.get("JUNTO_SYNC_PRIMARY_URL"))
    p.add_argument("--local-url", default=os.environ.get("JUNTO_SYNC_LOCAL_URL", "http://localhost:8080/mcp"))
    p.add_argument("--primary-key", default=os.environ.get("JUNTO_SYNC_PRIMARY_KEY"))
    p.add_argument("--local-key", default=os.environ.get("JUNTO_SYNC_LOCAL_KEY"))
    p.add_argument(
        "--cursor-path",
        default=os.environ.get("JUNTO_SYNC_CURSOR_PATH", DEFAULT_CURSOR_PATH),
    )
    p.add_argument(
        "--pull-interval", type=float,
        default=_env_float("JUNTO_SYNC_PULL_INTERVAL", DEFAULT_PULL_INTERVAL),
    )
    p.add_argument(
        "--push-interval", type=float,
        default=_env_float("JUNTO_SYNC_PUSH_INTERVAL", DEFAULT_PUSH_INTERVAL),
    )
    p.add_argument(
        "--pull-jitter", type=float,
        default=_env_float("JUNTO_SYNC_PULL_JITTER", DEFAULT_PULL_JITTER),
    )
    p.add_argument(
        "--push-jitter", type=float,
        default=_env_float("JUNTO_SYNC_PUSH_JITTER", DEFAULT_PUSH_JITTER),
    )
    p.add_argument(
        "--limit", type=int,
        default=_env_int("JUNTO_SYNC_LIMIT", DEFAULT_LIMIT),
    )
    p.add_argument(
        "--agent-name",
        default=os.environ.get("JUNTO_SYNC_AGENT_NAME", "sync-engine"),
    )
    p.add_argument(
        "--project", default=os.environ.get("JUNTO_SYNC_PROJECT", "junto"),
    )
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


SUPERVISOR_BACKOFF_MIN = 5.0
SUPERVISOR_BACKOFF_MAX = 60.0


async def _supervisor_sleep(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, returning early if stop_event fires."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _run_supervisor(
    *,
    build_clients,  # Callable[[], Awaitable[Tuple[MCPClient, MCPClient]]]
    cursor_path: Path,
    pull_interval: float,
    push_interval: float,
    pull_jitter: float,
    push_jitter: float,
    limit: int,
    stop_event: asyncio.Event,
    backoff_min: float = SUPERVISOR_BACKOFF_MIN,
    backoff_max: float = SUPERVISOR_BACKOFF_MAX,
    sleep_fn=None,  # Callable[[float], Awaitable[None]]
) -> Dict[str, Any]:
    """Supervisor loop. Returns final supervisor stats dict.

    The MCP SDK's `streamable_http_client` wraps its httpx connection in
    an anyio task group. When the underlying transport fails (e.g., the
    tailnet hostname stops resolving because the link dropped, or the
    remote process gets killed), the task group cancels and surfaces an
    `asyncio.CancelledError` to whatever coroutine is awaiting on
    `call_tool`. CancelledError is a `BaseException` (not `Exception`),
    so SyncEngine's `except Exception` does not catch it; the engine's
    task dies. Worse, the cancel scope remains poisoned for the
    duration of the task that owns it — subsequent awaits in the same
    task get cancelled too.

    The fix: run the engine in a child task whose cancel-scope death is
    contained. This supervisor lives at the event loop root with its
    own (uncontaminated) cancel scope, so it can detect the inner
    task's death, sleep through a backoff, and rebuild fresh clients
    via `build_clients` + a new engine.

    Inner-exit modes:
      * Clean return (stop_event was set) → break and return.
      * `CancelledError` → assume transport loss; discard old clients
        (do NOT try to aclose, scope may be poisoned); rebuild.
      * `_TransportError` from engine.run propagating up → rebuild.
      * Any other exception → log, rebuild.

    Backoff between rebuilds: `backoff_min` → `backoff_max` capped,
    doubling on each failure. Production matches docker's cadence
    (5s → 60s) so the supervisor doesn't burn cycles in a tight loop
    against a still-unreachable peer.

    `build_clients` is async; it must return a tuple `(primary, local)`
    of already-connected MCP clients, or raise. If it raises, the
    supervisor counts the failure, sleeps, and retries.

    `sleep_fn(seconds)` is the interruptible sleep used between
    rebuilds. Defaults to wait_for(stop_event) with timeout; tests
    inject a faster version.
    """
    if sleep_fn is None:
        async def sleep_fn(seconds: float) -> None:
            await _supervisor_sleep(stop_event, seconds)

    sup_stats: Dict[str, Any] = {
        "reconnects": 0,
        "supervisor_errors": 0,
        "last_engine_stats": {},
    }
    backoff = backoff_min

    async def _inner_runner() -> Dict[str, int]:
        """Body of the inner task. Builds clients + engine inside this task
        so the streamable_http_client cancel scope is local to it (and
        cannot poison the supervisor's awaits when transport dies)."""
        primary, local = await build_clients()
        engine = SyncEngine(
            primary=primary,
            local=local,
            cursor_path=cursor_path,
            pull_interval=pull_interval,
            push_interval=push_interval,
            pull_jitter=pull_jitter,
            push_jitter=push_jitter,
            limit=limit,
        )
        # Expose stats to supervisor before run begins so we capture them
        # even if engine dies on the first iteration.
        sup_stats["last_engine_stats"] = engine.stats
        try:
            await engine.run(stop_event)
        finally:
            # Best-effort close. The cancel scope may be poisoned, in which
            # case aclose() itself may raise — that's fine; this task is
            # dying anyway, the supervisor will rebuild.
            for client, label in ((local, "local"), (primary, "primary")):
                try:
                    await client.aclose()
                except BaseException:
                    logger.debug(
                        "%s.aclose failed during inner teardown",
                        label, exc_info=True,
                    )
        return engine.stats

    while not stop_event.is_set():
        logger.info("supervisor: inner engine starting")
        inner = asyncio.create_task(_inner_runner(), name="sync-engine-inner")

        clean = False
        try:
            await inner
            # Inner returned of its own accord — only happens when
            # stop_event fired during a sleep. Clean shutdown.
            clean = True
        except asyncio.CancelledError:
            if stop_event.is_set():
                # Real shutdown initiated externally — propagate.
                raise
            sup_stats["supervisor_errors"] += 1
            logger.warning(
                "supervisor: inner task CancelledError (transport loss assumed); "
                "rebuilding in %.1fs", backoff,
            )
        except _TransportError as e:
            sup_stats["supervisor_errors"] += 1
            logger.warning(
                "supervisor: inner task TransportError: %s; rebuilding in %.1fs",
                e, backoff,
            )
        except Exception as e:
            sup_stats["supervisor_errors"] += 1
            # Connect failures inside _inner_runner land here. We log at
            # WARNING (with type name) rather than spamming a full
            # traceback because the typical case is "primary unreachable"
            # which is loud but expected during a transport outage.
            logger.warning(
                "supervisor: inner task %s: %s; rebuilding in %.1fs",
                type(e).__name__, e, backoff,
            )

        if clean:
            break

        # Failure path. Inner task already best-efforted client cleanup
        # in its finally block; the dead clients will be GC'd. Some
        # cosmetic "Task exception was never retrieved" / async-gen
        # cleanup errors may surface in the log.
        sup_stats["reconnects"] += 1
        await sleep_fn(backoff)
        backoff = min(backoff_max, backoff * 2)

    return sup_stats


# Patterns matched by the supervisor's asyncio exception handler. These
# come from MCP SDK / anyio's async-generator cleanup when an inner task
# dies inside a poisoned cancel scope — they are cosmetic (the supervisor
# already handled the underlying failure), not actionable.
_QUIET_EXCEPTION_MESSAGES = (
    "Task exception was never retrieved",
    "an error occurred during closing of asynchronous generator",
)
_QUIET_EXCEPTION_TYPES = (
    "Attempted to exit cancel scope in a different task than it was entered in",
)


def _supervisor_exception_handler(loop, context):
    """asyncio exception handler that suppresses cosmetic cancel-scope
    cleanup noise from dead inner tasks while letting everything else
    through to the default handler.

    The MCP SDK's `streamable_http_client` uses anyio task groups whose
    cancel scopes get garbage-collected after an inner task dies. The GC
    path complains about cross-task scope exits and never-retrieved task
    exceptions even though the supervisor has already caught and handled
    the originating failure.
    """
    msg = context.get("message", "")
    exc = context.get("exception")
    exc_msg = str(exc) if exc is not None else ""
    if any(p in msg for p in _QUIET_EXCEPTION_MESSAGES):
        return
    if any(p in exc_msg for p in _QUIET_EXCEPTION_TYPES):
        return
    if isinstance(exc, asyncio.CancelledError):
        # An inner task got cancelled and we already handled it on the
        # supervisor side. Default handler would print a stack trace
        # per dead task; this is just noise.
        return
    loop.default_exception_handler(context)


async def _amain(args: argparse.Namespace) -> int:
    """CLI entry: wire signal handlers and call _run_supervisor."""
    if not args.primary_url:
        print("ERROR: --primary-url (or JUNTO_SYNC_PRIMARY_URL) is required",
              file=sys.stderr)
        return 2

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_supervisor_exception_handler)
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(signal, sig_name), stop_event.set
            )
        except (NotImplementedError, RuntimeError):
            pass

    async def build_clients():
        primary = HTTPMCPClient(
            url=args.primary_url,
            api_key=args.primary_key,
            agent_name=args.agent_name,
            project=args.project,
            role_description="Peer-side sync engine — primary connection",
        )
        local = HTTPMCPClient(
            url=args.local_url,
            api_key=args.local_key,
            agent_name=args.agent_name,
            project=args.project,
            role_description="Peer-side sync engine — local connection",
        )
        await primary.connect()
        await local.connect()
        return primary, local

    logger.info(
        "sync engine supervisor starting: primary=%s local=%s cursors=%s",
        args.primary_url, args.local_url, args.cursor_path,
    )

    sup_stats = await _run_supervisor(
        build_clients=build_clients,
        cursor_path=Path(args.cursor_path).expanduser(),
        pull_interval=args.pull_interval,
        push_interval=args.push_interval,
        pull_jitter=args.pull_jitter,
        push_jitter=args.push_jitter,
        limit=args.limit,
        stop_event=stop_event,
    )

    logger.info(
        "sync engine supervisor stopping: supervisor=%s last_engine=%s",
        {k: v for k, v in sup_stats.items() if k != "last_engine_stats"},
        sup_stats.get("last_engine_stats"),
    )
    return 0


def cli_main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(cli_main())
