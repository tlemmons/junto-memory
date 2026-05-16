"""federated_writer.py — steady-state load generator for §13 acceptance.

Drives writes against a peer's MCP endpoint to simulate a federated agent.
Used to keep activity going through a Tailscale-drop window so we can verify
on reconnect that (a) nothing was lost, (b) push cursor catches up, (c) no
duplicates.

Every write is tagged `test:phase2-acceptance` so the lot can be archived
after the run via `memory_archive_by_tag(tag="test:phase2-acceptance")`.

Usage (inside or outside the peer VM, anything with network reach to the
peer's MCP):

    JUNTO_PEER_URL=http://<peer-ip>:8080/mcp \\
    JUNTO_PEER_KEY=<admin-or-agent-key-on-peer> \\
    python contrib/test/federated_writer.py --rate-per-min 12 --duration-min 90

Defaults: 12 writes/min (1 every 5s), runs until SIGINT.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import signal
import string
import sys
import time
from typing import Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from shared_memory.sync_engine import HTTPMCPClient  # noqa: E402

PEER_URL = os.environ.get("JUNTO_PEER_URL", "http://localhost:8080/mcp")
PEER_KEY = os.environ.get("JUNTO_PEER_KEY", "")

TEST_TAG = "test:phase2-acceptance"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _rand_token(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _pick_action() -> str:
    # Weighted mix: learnings dominate (they exercise the embedding path);
    # store + send_message provide tool-surface coverage.
    return random.choices(
        ["learning", "store", "message"],
        weights=[0.5, 0.3, 0.2],
        k=1,
    )[0]


async def _do_learning(client: HTTPMCPClient, seq: int) -> Tuple[str, bool, str]:
    token = _rand_token()
    resp = await client.call_tool(
        "memory_record_learning",
        {
            "title": f"§13 acceptance learning #{seq} ({token})",
            "details": f"Synthetic learning from federated_writer.py, seq={seq}, token={token}.",
            "project": "junto",
            "tags": [TEST_TAG, "synthetic"],
        },
    )
    doc_id = resp.get("id") or resp.get("doc_id") or ""
    return ("memory_record_learning", bool(doc_id), f"id={doc_id!r}")


async def _do_store(client: HTTPMCPClient, seq: int) -> Tuple[str, bool, str]:
    token = _rand_token()
    resp = await client.call_tool(
        "memory_store",
        {
            "title": f"§13 acceptance store #{seq} ({token})",
            "content": f"Synthetic store from federated_writer.py, seq={seq}.",
            "project": "junto",
            "tags": [TEST_TAG, "synthetic"],
        },
    )
    doc_id = resp.get("id") or resp.get("doc_id") or ""
    return ("memory_store", bool(doc_id), f"id={doc_id!r}")


async def _do_message(client: HTTPMCPClient, seq: int) -> Tuple[str, bool, str]:
    token = _rand_token()
    # Self-addressed so we don't pollute real inboxes even if test-primary
    # gets messages-mirrored anywhere. Receiver is the agent we're posing as.
    resp = await client.call_tool(
        "memory_send_message",
        {
            "to_instance": "federated-writer",
            "to_project": "junto",
            "subject": f"§13 acceptance msg #{seq} ({token})",
            "body": f"Synthetic message from federated_writer.py, seq={seq}.",
            "category": "info",
            "priority": "low",
        },
    )
    mid = resp.get("message_id") or resp.get("id") or ""
    return ("memory_send_message", bool(mid), f"id={mid!r}")


async def main(args: argparse.Namespace) -> int:
    if not PEER_KEY:
        log("ERROR: set JUNTO_PEER_KEY env var to an agent- or admin-tier key on the peer")
        return 2

    interval = 60.0 / max(args.rate_per_min, 0.001)
    log(f"federated_writer: peer={PEER_URL} rate={args.rate_per_min}/min interval={interval:.2f}s")
    log(f"  tag={TEST_TAG} (archive after run: memory_archive_by_tag(tag='{TEST_TAG}'))")

    stop = asyncio.Event()

    def _sigint(_signum: int, _frame: object) -> None:
        log("SIGINT — stopping after current write")
        stop.set()
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    client = HTTPMCPClient(
        url=PEER_URL,
        api_key=PEER_KEY,
        agent_name="federated-writer",
        project="junto",
        role_description="§13 acceptance test load generator",
    )

    try:
        await client.connect()
    except Exception as exc:
        log(f"FATAL: connect failed: {type(exc).__name__}: {exc}")
        return 3
    log(f"  connected (session_id={(client.session_id or '')[:30]})")

    deadline = time.monotonic() + args.duration_min * 60 if args.duration_min > 0 else None
    seq = 0
    successes = 0
    failures = 0

    try:
        while not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                log(f"duration reached ({args.duration_min} min)")
                break

            seq += 1
            action = _pick_action()
            t0 = time.monotonic()
            try:
                if action == "learning":
                    tool, ok, info = await _do_learning(client, seq)
                elif action == "store":
                    tool, ok, info = await _do_store(client, seq)
                else:
                    tool, ok, info = await _do_message(client, seq)
                dt_ms = (time.monotonic() - t0) * 1000
                marker = "OK " if ok else "ERR"
                log(f"  #{seq:04d} {marker} {tool:<24s} {info} ({dt_ms:.0f}ms)")
                if ok:
                    successes += 1
                else:
                    failures += 1
            except Exception as exc:
                failures += 1
                log(f"  #{seq:04d} ERR {action} {type(exc).__name__}: {str(exc)[:120]}")

            # Sleep, but be interruptible by stop signal.
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    log(f"DONE. writes={seq} success={successes} failure={failures}")
    log(f"  clean up via memory_archive_by_tag(tag='{TEST_TAG}')")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--rate-per-min", type=float, default=12.0,
                   help="writes per minute (default 12 = one every 5s)")
    p.add_argument("--duration-min", type=float, default=0.0,
                   help="run for this many minutes then exit; 0 = until SIGINT")
    sys.exit(asyncio.run(main(p.parse_args())))
