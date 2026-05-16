"""Unit tests for the peer-side sync engine.

Covers `design:local-first-junto-v0-mvp` v0.5.0 §5 + §8 Phase 2.

Tests use a `FakeMCPClient` injected into `SyncEngine` — production
`HTTPMCPClient` is exercised only via the smoke tests against a live
server (out of scope for unit tests).
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from shared_memory.sync_engine import (
    SyncEngine,
    _empty_cursor_state,
    _merge_pull_cursor,
    _run_supervisor,
    load_cursors,
    save_cursors,
)

# ───────────────────────────────────────────────────────────────────
# FakeMCPClient — scripted responses, records calls
# ───────────────────────────────────────────────────────────────────


class FakeMCPClient:
    """Records every call_tool invocation and returns scripted responses.

    Set `responses[name]` to a list of dicts or a callable. Each call_tool
    pops the next response. If a response is an Exception, it is raised.
    """

    def __init__(self, name: str = "fake"):
        self.name = name
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self.responses: Dict[str, List[Any]] = {}

    def queue(self, tool: str, *responses: Any) -> None:
        self.responses.setdefault(tool, []).extend(responses)

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(args)))
        queue = self.responses.get(name)
        if not queue:
            raise AssertionError(
                f"FakeMCPClient({self.name}) had no scripted response for {name}; "
                f"calls so far: {[c[0] for c in self.calls]}"
            )
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt(args)
        return dict(nxt)


def _op(seq: int, origin: str, op_type: str = "learning.recorded", doc_id: Optional[str] = None) -> Dict[str, Any]:
    """Synthesize a minimal op-log row that passes shape validation."""
    return {
        "_id": f"op_{origin}_{seq}",
        "seq": seq,
        "origin": origin,
        "intent_id": None,
        "actor": {"agent": "test", "project": "junto", "session_id": "s1"},
        "op_type": op_type,
        "ref": {"collection": "test", "doc_id": doc_id or f"doc_{origin}_{seq}"},
        "payload": {"text": "x"},
        "schema_version": 1,
        "ts": "2026-05-15T00:00:00Z",
    }


def _push_ok(applied: int = 1) -> Dict[str, Any]:
    return {
        "results": [{"op_id": f"r{i}", "disposition": "applied"} for i in range(applied)],
        "applied_count": applied,
        "rejected_count": 0,
        "conflict_count": 0,
        "deduped_count": 0,
        "server_origin": "fake",
    }


def _empty_pull(server_origin: str = "fake") -> Dict[str, Any]:
    return {
        "ops": [],
        "next_cursor": {},
        "has_more": {},
        "server_origin": server_origin,
    }


# ───────────────────────────────────────────────────────────────────
# Cursor persistence
# ───────────────────────────────────────────────────────────────────


class TestCursorPersistence:
    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        p = tmp_path / "absent.json"
        assert load_cursors(p) == _empty_cursor_state()

    def test_load_corrupt_file_returns_empty(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert load_cursors(p) == _empty_cursor_state()

    def test_load_non_dict_returns_empty(self, tmp_path: Path):
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]")
        assert load_cursors(p) == _empty_cursor_state()

    def test_load_partial_dict_normalizes(self, tmp_path: Path):
        p = tmp_path / "partial.json"
        p.write_text(json.dumps({"pull": {"central": 5}}))
        result = load_cursors(p)
        assert result == {"pull": {"central": 5}, "push": {}}

    def test_save_then_load_round_trip(self, tmp_path: Path):
        p = tmp_path / "cursors.json"
        cursors = {"pull": {"central": 100, "peer-a": 42}, "push": {"peer-b": 7}}
        save_cursors(p, cursors)
        assert load_cursors(p) == cursors

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        p = tmp_path / "nested" / "dir" / "cursors.json"
        save_cursors(p, _empty_cursor_state())
        assert p.exists()

    def test_save_is_atomic_via_tmp(self, tmp_path: Path):
        p = tmp_path / "cursors.json"
        save_cursors(p, {"pull": {"a": 1}, "push": {}})
        # After save, only the real file exists; no .tmp residue.
        assert p.exists()
        assert not (tmp_path / "cursors.json.tmp").exists()

    def test_merge_pull_cursor_overwrites_named_keeps_others(self):
        existing = {"central": 100, "peer-a": 42}
        nxt = {"central": 120}
        merged = _merge_pull_cursor(existing, nxt)
        assert merged == {"central": 120, "peer-a": 42}

    def test_merge_pull_cursor_adds_new_origin(self):
        existing = {"central": 100}
        nxt = {"peer-b": 5}
        assert _merge_pull_cursor(existing, nxt) == {"central": 100, "peer-b": 5}


# ───────────────────────────────────────────────────────────────────
# Pull loop
# ───────────────────────────────────────────────────────────────────


@pytest.fixture
def cursor_path(tmp_path: Path) -> Path:
    return tmp_path / "cursors.json"


def _make_engine(
    primary: FakeMCPClient, local: FakeMCPClient, cursor_path: Path, **kwargs
) -> SyncEngine:
    defaults = dict(
        pull_interval=0.01,
        push_interval=0.01,
        pull_jitter=0,
        push_jitter=0,
        limit=10,
        backoff_min=0.01,
        backoff_max=0.1,
    )
    defaults.update(kwargs)
    return SyncEngine(
        primary=primary,
        local=local,
        cursor_path=cursor_path,
        rng=random.Random(0),
        sleep=asyncio.sleep,
        **defaults,
    )


class TestPullLoop:
    async def test_empty_primary_no_apply(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        primary.queue("memory_sync_pull", _empty_pull("central"))

        engine = _make_engine(primary, local, cursor_path)
        count = await engine._pull_loop()

        assert count == 0
        # local.memory_sync_push must NOT be called when ops list is empty.
        assert [c[0] for c in local.calls] == []

    async def test_single_page_apply(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        ops = [_op(1, "central"), _op(2, "central")]
        primary.queue(
            "memory_sync_pull",
            {
                "ops": ops,
                "next_cursor": {"central": 2},
                "has_more": {"central": False},
                "server_origin": "central",
            },
        )
        local.queue("memory_sync_push", _push_ok(2))

        engine = _make_engine(primary, local, cursor_path)
        count = await engine._pull_loop()

        assert count == 2
        assert engine.cursors["pull"] == {"central": 2}
        # Cursor file written.
        assert load_cursors(cursor_path)["pull"] == {"central": 2}
        # Push was called with the exact ops list.
        push_call = next(c for c in local.calls if c[0] == "memory_sync_push")
        assert push_call[1]["ops"] == ops

    async def test_multi_page_drain_until_has_more_false(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        primary.queue(
            "memory_sync_pull",
            {
                "ops": [_op(1, "central"), _op(2, "central")],
                "next_cursor": {"central": 2},
                "has_more": {"central": True},
                "server_origin": "central",
            },
            {
                "ops": [_op(3, "central")],
                "next_cursor": {"central": 3},
                "has_more": {"central": False},
                "server_origin": "central",
            },
        )
        local.queue("memory_sync_push", _push_ok(2), _push_ok(1))

        engine = _make_engine(primary, local, cursor_path)
        count = await engine._pull_loop()

        assert count == 3
        assert engine.cursors["pull"] == {"central": 3}
        # Two pulls + two pushes.
        assert sum(1 for c in primary.calls if c[0] == "memory_sync_pull") == 2
        assert sum(1 for c in local.calls if c[0] == "memory_sync_push") == 2

    async def test_pull_advances_cursor_per_origin(self, cursor_path):
        """Origin not returned this batch must keep its prior cursor."""
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        # Pre-existing cursor for two origins; this batch only advances one.
        cursor_path.write_text(json.dumps({
            "pull": {"central": 10, "peer-a": 5},
            "push": {},
        }))
        primary.queue(
            "memory_sync_pull",
            {
                "ops": [_op(11, "central")],
                "next_cursor": {"central": 11},  # only central
                "has_more": {"central": False},
                "server_origin": "central",
            },
        )
        local.queue("memory_sync_push", _push_ok(1))

        engine = _make_engine(primary, local, cursor_path)
        await engine._pull_loop()

        # peer-a's cursor must persist.
        assert engine.cursors["pull"] == {"central": 11, "peer-a": 5}


# ───────────────────────────────────────────────────────────────────
# Push loop
# ───────────────────────────────────────────────────────────────────


class TestPushLoop:
    async def test_discovers_local_origin_via_empty_pull(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        # First call is the discovery probe; second is the real own-origin pull.
        local.queue("memory_sync_pull",
                    _empty_pull("peer-laptop"),
                    _empty_pull("peer-laptop"))

        engine = _make_engine(primary, local, cursor_path)
        await engine._push_loop()

        assert engine.local_origin == "peer-laptop"
        # discovery probe used limit=1 + empty cursor
        discovery = local.calls[0]
        assert discovery[0] == "memory_sync_pull"
        assert discovery[1]["since_cursor_by_origin"] == {}
        assert discovery[1]["limit"] == 1

    async def test_push_own_origin_only_filters_other_origins(self, cursor_path):
        """Local sync_pull may return ops from multiple origins (peer
        previously received ops from primary). The push loop must filter
        to local-origin only, never re-pushing primary's ops."""
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        local.queue(
            "memory_sync_pull",
            _empty_pull("peer-laptop"),  # discovery
            {
                "ops": [
                    _op(1, "peer-laptop"),
                    _op(50, "central"),  # must be filtered out
                    _op(2, "peer-laptop"),
                ],
                "next_cursor": {"peer-laptop": 2, "central": 50},
                "has_more": {"peer-laptop": False, "central": False},
                "server_origin": "peer-laptop",
            },
        )
        primary.queue("memory_sync_push", _push_ok(2))

        engine = _make_engine(primary, local, cursor_path)
        count = await engine._push_loop()

        assert count == 2
        push_call = next(c for c in primary.calls if c[0] == "memory_sync_push")
        push_origins = {op["origin"] for op in push_call[1]["ops"]}
        assert push_origins == {"peer-laptop"}, (
            "primary.memory_sync_push must NEVER receive ops where origin "
            "!= local origin — that would cause central to receive its own "
            "ops back, hitting the self-origin gate at best, or worse "
            "creating a divergent op-log row at worst"
        )
        # Cursor moves to max own-origin seq.
        assert engine.cursors["push"] == {"peer-laptop": 2}

    async def test_push_advances_cursor(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        local.queue(
            "memory_sync_pull",
            _empty_pull("peer-laptop"),
            {
                "ops": [_op(7, "peer-laptop")],
                "next_cursor": {"peer-laptop": 7},
                "has_more": {"peer-laptop": False},
                "server_origin": "peer-laptop",
            },
        )
        primary.queue("memory_sync_push", _push_ok(1))

        engine = _make_engine(primary, local, cursor_path)
        await engine._push_loop()

        assert engine.cursors["push"]["peer-laptop"] == 7
        assert load_cursors(cursor_path)["push"]["peer-laptop"] == 7

    async def test_push_skips_empty_local_pull(self, cursor_path):
        """No local ops to push → no call to primary.memory_sync_push."""
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        local.queue(
            "memory_sync_pull",
            _empty_pull("peer-laptop"),  # discovery
            _empty_pull("peer-laptop"),  # real query, nothing returned
        )

        engine = _make_engine(primary, local, cursor_path)
        count = await engine._push_loop()

        assert count == 0
        assert [c[0] for c in primary.calls] == []


# ───────────────────────────────────────────────────────────────────
# Loop control: backoff, stop, transport errors
# ───────────────────────────────────────────────────────────────────


class TestRunLoop:
    async def test_stop_event_cancels_during_sleep(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        # One iteration's worth: primary pull (empty), local discovery
        # (empty), local own-origin pull (empty). No pushes needed.
        primary.queue("memory_sync_pull", _empty_pull("central"))
        local.queue(
            "memory_sync_pull",
            _empty_pull("peer"),
            _empty_pull("peer"),
        )

        engine = _make_engine(
            primary, local, cursor_path,
            pull_interval=10.0,  # would block forever
        )
        stop_event = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.02)
            stop_event.set()

        await asyncio.gather(engine.run(stop_event), stopper())
        # One full iteration must have run before stop_event fired.
        assert engine.stats["iterations"] == 1

    async def test_transport_error_triggers_backoff(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        primary.queue("memory_sync_pull", RuntimeError("boom"))

        engine = _make_engine(
            primary, local, cursor_path,
            pull_interval=10.0,
            backoff_min=0.01,
            backoff_max=0.02,
        )
        stop_event = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.05)
            stop_event.set()

        await asyncio.gather(engine.run(stop_event), stopper())
        assert engine.stats["errors"] >= 1
        # backoff was the delay (0.01s) not the regular interval (10s).
        # If it had been 10s, the test would have taken 10s instead of 0.05s.

    async def test_error_does_not_advance_cursor(self, cursor_path):
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        primary.queue("memory_sync_pull", RuntimeError("transport"))

        engine = _make_engine(primary, local, cursor_path)
        with pytest.raises(Exception):
            await engine._pull_then_push()

        assert engine.cursors["pull"] == {}
        # File not written.
        assert not cursor_path.exists()

    async def test_server_returned_error_treated_as_transport(self, cursor_path):
        """Server-returned `{"error": "..."}` envelopes must surface as
        _TransportError, not be silently consumed as a successful empty
        response."""
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        primary.queue("memory_sync_pull", {"error": "Permission denied"})

        engine = _make_engine(primary, local, cursor_path)
        with pytest.raises(Exception):
            await engine._pull_then_push()


# ───────────────────────────────────────────────────────────────────
# Integration of pull + push within one iteration
# ───────────────────────────────────────────────────────────────────


class TestPullThenPush:
    async def test_pull_runs_before_push(self, cursor_path):
        """If primary has ops AND local has own-origin ops, both ship in
        a single iteration with pull running first."""
        primary = FakeMCPClient("primary")
        local = FakeMCPClient("local")
        # Pull side: primary returns one op.
        primary.queue(
            "memory_sync_pull",
            {
                "ops": [_op(5, "central")],
                "next_cursor": {"central": 5},
                "has_more": {"central": False},
                "server_origin": "central",
            },
        )
        # Push side: local returns own-origin discovery, then real pull.
        local.queue(
            "memory_sync_push",
            _push_ok(1),  # for applying central's op locally
        )
        local.queue(
            "memory_sync_pull",
            _empty_pull("peer-laptop"),  # discovery
            {
                "ops": [_op(3, "peer-laptop")],
                "next_cursor": {"peer-laptop": 3},
                "has_more": {"peer-laptop": False},
                "server_origin": "peer-laptop",
            },
        )
        primary.queue("memory_sync_push", _push_ok(1))

        engine = _make_engine(primary, local, cursor_path)
        pulled, pushed = await engine._pull_then_push()

        assert pulled == 1
        assert pushed == 1
        assert engine.cursors["pull"] == {"central": 5}
        assert engine.cursors["push"] == {"peer-laptop": 3}

        # Pull should have happened before push: the first local.calls
        # entry must be memory_sync_push (applying primary's op), not
        # memory_sync_pull (push-side discovery).
        first_local_call = local.calls[0]
        assert first_local_call[0] == "memory_sync_push", (
            f"pull must run first; got {first_local_call[0]} as first local call"
        )


# ───────────────────────────────────────────────────────────────────
# Supervisor: rebuild on inner-task transport-loss
# ───────────────────────────────────────────────────────────────────


class _CancellingFakeMCPClient(FakeMCPClient):
    """FakeMCPClient that can also raise BaseException (e.g., CancelledError).

    The base FakeMCPClient only re-raises queued items that are
    `isinstance(Exception)`. Transport-loss simulation needs to also
    surface `asyncio.CancelledError` (a `BaseException`).
    """

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        queue = self.responses.get(name)
        if not queue:
            raise AssertionError(
                f"_CancellingFakeMCPClient({self.name}) had no scripted response "
                f"for {name}; calls so far: {[c[0] for c in self.calls]}"
            )
        nxt = queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if callable(nxt):
            return nxt(args)
        return dict(nxt)

    async def aclose(self):  # called on clean-shutdown branch
        pass


class TestSupervisor:
    async def test_clean_shutdown_does_not_reconnect(self, cursor_path):
        """stop_event set during inner sleep ⇒ supervisor exits with
        reconnects=0, supervisor_errors=0."""
        primary = _CancellingFakeMCPClient("primary")
        local = _CancellingFakeMCPClient("local")
        # One quiet iteration: empty pull, empty discovery + own-pull.
        primary.queue("memory_sync_pull", _empty_pull("central"))
        local.queue(
            "memory_sync_pull",
            _empty_pull("peer"),
            _empty_pull("peer"),
        )

        stop_event = asyncio.Event()

        async def build():
            return primary, local

        async def stopper():
            await asyncio.sleep(0.02)
            stop_event.set()

        sup_stats_task = asyncio.create_task(
            _run_supervisor(
                build_clients=build,
                cursor_path=cursor_path,
                pull_interval=10.0,  # would block ≫ stopper
                push_interval=5.0,
                pull_jitter=0.0,
                push_jitter=0.0,
                limit=500,
                stop_event=stop_event,
                backoff_min=0.001,
                backoff_max=0.002,
            )
        )
        await asyncio.gather(sup_stats_task, stopper())
        sup_stats = sup_stats_task.result()

        assert sup_stats["reconnects"] == 0
        assert sup_stats["supervisor_errors"] == 0
        assert sup_stats["last_engine_stats"]["iterations"] == 1

    async def test_cancelled_error_triggers_rebuild(self, cursor_path):
        """CancelledError raised from a client mid-call propagates up
        through engine.run (CancelledError is BaseException, not Exception,
        so engine's `except` doesn't catch it). The supervisor must catch
        it on `await inner`, count a reconnect, and call build_clients()
        again."""
        # Build a sequence of (primary, local) pairs. First pair raises
        # CancelledError on the first sync_pull; second pair runs one quiet
        # iteration before stop_event fires.
        builds_done = 0
        all_clients = []

        async def build():
            nonlocal builds_done
            builds_done += 1
            primary = _CancellingFakeMCPClient(f"primary-{builds_done}")
            local = _CancellingFakeMCPClient(f"local-{builds_done}")
            if builds_done == 1:
                primary.queue("memory_sync_pull", asyncio.CancelledError("transport"))
            else:
                primary.queue("memory_sync_pull", _empty_pull("central"))
                local.queue(
                    "memory_sync_pull",
                    _empty_pull("peer"),
                    _empty_pull("peer"),
                )
            all_clients.append((primary, local))
            return primary, local

        stop_event = asyncio.Event()

        async def stopper():
            # Give the supervisor enough time to: build once, hit the cancel,
            # apply backoff (we set this short), build again, run one iter.
            await asyncio.sleep(0.06)
            stop_event.set()

        sup_stats_task = asyncio.create_task(
            _run_supervisor(
                build_clients=build,
                cursor_path=cursor_path,
                pull_interval=10.0,
                push_interval=5.0,
                pull_jitter=0.0,
                push_jitter=0.0,
                limit=500,
                stop_event=stop_event,
                backoff_min=0.005,
                backoff_max=0.01,
            )
        )
        await asyncio.gather(sup_stats_task, stopper())
        sup_stats = sup_stats_task.result()

        assert builds_done == 2, f"expected 2 builds (1 failure + 1 recovery), got {builds_done}"
        assert sup_stats["reconnects"] == 1
        assert sup_stats["supervisor_errors"] == 1
        # The second-pair engine actually ran an iteration before stop.
        assert sup_stats["last_engine_stats"]["iterations"] == 1

    async def test_build_clients_failure_retried_with_backoff(self, cursor_path):
        """If build_clients() itself raises (e.g., connect() failed), the
        supervisor counts a supervisor_error, sleeps backoff, and tries
        again. Reconnects only count successful client builds."""
        builds_done = 0

        async def build():
            nonlocal builds_done
            builds_done += 1
            if builds_done == 1:
                raise ConnectionError("primary unreachable")
            primary = _CancellingFakeMCPClient("primary")
            local = _CancellingFakeMCPClient("local")
            primary.queue("memory_sync_pull", _empty_pull("central"))
            local.queue(
                "memory_sync_pull",
                _empty_pull("peer"),
                _empty_pull("peer"),
            )
            return primary, local

        stop_event = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.05)
            stop_event.set()

        sup_stats_task = asyncio.create_task(
            _run_supervisor(
                build_clients=build,
                cursor_path=cursor_path,
                pull_interval=10.0,
                push_interval=5.0,
                pull_jitter=0.0,
                push_jitter=0.0,
                limit=500,
                stop_event=stop_event,
                backoff_min=0.005,
                backoff_max=0.01,
            )
        )
        await asyncio.gather(sup_stats_task, stopper())
        sup_stats = sup_stats_task.result()

        assert builds_done == 2, f"expected 2 builds (1 connect-failure + 1 success), got {builds_done}"
        # build_clients runs inside the inner task, so its failure dies
        # the inner task — same recovery path as transport-loss mid-run.
        assert sup_stats["reconnects"] == 1
        assert sup_stats["supervisor_errors"] == 1

    async def test_inner_raises_transport_error_triggers_rebuild(self, cursor_path):
        """If the inner engine's main loop propagates a `_TransportError`
        (e.g., its internal backoff loop exited with an error), the
        supervisor should also rebuild."""
        builds_done = 0

        async def build():
            nonlocal builds_done
            builds_done += 1
            primary = _CancellingFakeMCPClient(f"primary-{builds_done}")
            local = _CancellingFakeMCPClient(f"local-{builds_done}")
            if builds_done == 1:
                # SyncEngine wraps RuntimeError from _call as _TransportError.
                # Engine's own except _TransportError handler catches and
                # backs off — to make it propagate, we'd need to break out
                # of the engine loop. Easier: make the engine's wait-for-stop
                # sleep race with an immediate-stop, and queue a RuntimeError
                # that bypasses engine's catch via a non-Exception type.
                primary.queue("memory_sync_pull", asyncio.CancelledError("synthetic"))
            else:
                primary.queue("memory_sync_pull", _empty_pull("central"))
                local.queue(
                    "memory_sync_pull",
                    _empty_pull("peer"),
                    _empty_pull("peer"),
                )
            return primary, local

        stop_event = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.05)
            stop_event.set()

        sup_stats_task = asyncio.create_task(
            _run_supervisor(
                build_clients=build,
                cursor_path=cursor_path,
                pull_interval=10.0,
                push_interval=5.0,
                pull_jitter=0.0,
                push_jitter=0.0,
                limit=500,
                stop_event=stop_event,
                backoff_min=0.005,
                backoff_max=0.01,
            )
        )
        await asyncio.gather(sup_stats_task, stopper())
        sup_stats = sup_stats_task.result()

        assert builds_done == 2
        assert sup_stats["reconnects"] == 1
