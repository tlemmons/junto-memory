"""sync_observer.py — cursor-lag observer for §13 acceptance.

Polls both primary and peer via memory_sync_pull(since={}, head_only=True),
reads their `next_cursor` maps (highest op_log seq per origin), and computes:

  pull_lag[primary_origin] = primary.cursor[primary_origin] - peer.cursor[primary_origin]
      = how far the peer is behind in receiving primary's writes
  push_lag[peer_origin]    = peer.cursor[peer_origin] - primary.cursor[peer_origin]
      = how far the peer is behind in pushing its own writes to primary

Pass criterion during steady-state: both lags ≈ 0 (small spikes during
push-interval cycles are normal). During Tailscale-drop window: pull_lag
and push_lag grow monotonically. On reconnect: both should return to 0
within ~5 min per §13.

Usage:

    JUNTO_PRIMARY_URL=http://<primary-ip-or-tailnet>:8080/mcp \\
    JUNTO_PRIMARY_KEY=<admin-key-on-primary> \\
    JUNTO_PEER_URL=http://<peer-ip-or-tailnet>:8080/mcp \\
    JUNTO_PEER_KEY=<admin-key-on-peer> \\
    python contrib/test/sync_observer.py --interval 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from typing import Dict, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from shared_memory.sync_engine import HTTPMCPClient  # noqa: E402

PRIMARY_URL = os.environ.get("JUNTO_PRIMARY_URL", "")
PRIMARY_KEY = os.environ.get("JUNTO_PRIMARY_KEY", "")
PEER_URL = os.environ.get("JUNTO_PEER_URL", "")
PEER_KEY = os.environ.get("JUNTO_PEER_KEY", "")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def _fetch_cursor_view(client: HTTPMCPClient) -> Tuple[Optional[str], Dict[str, int]]:
    """Returns (server_origin, next_cursor_by_origin). Empty dict on error."""
    try:
        resp = await client.call_tool(
            "memory_sync_pull",
            {"since_cursor_by_origin": {}, "head_only": True},
        )
    except Exception as exc:
        log(f"  pull failed: {type(exc).__name__}: {str(exc)[:120]}")
        return (None, {})
    return (resp.get("server_origin") or None, dict(resp.get("next_cursor") or {}))


def _compute_lag(primary_view: Dict[str, int], peer_view: Dict[str, int]) -> Dict[str, int]:
    """primary - peer per origin; positive = peer is behind."""
    origins = set(primary_view) | set(peer_view)
    return {o: primary_view.get(o, 0) - peer_view.get(o, 0) for o in origins}


async def main(args: argparse.Namespace) -> int:
    if not PRIMARY_URL or not PRIMARY_KEY:
        log("ERROR: set JUNTO_PRIMARY_URL and JUNTO_PRIMARY_KEY"); return 2
    if not PEER_URL or not PEER_KEY:
        log("ERROR: set JUNTO_PEER_URL and JUNTO_PEER_KEY"); return 2

    log(f"sync_observer: primary={PRIMARY_URL}")
    log(f"               peer={PEER_URL}")
    log(f"               interval={args.interval}s alert_threshold={args.alert_threshold}")

    stop = asyncio.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    primary = HTTPMCPClient(
        url=PRIMARY_URL, api_key=PRIMARY_KEY,
        agent_name="sync-observer-primary",
        project="junto",
        role_description="§13 acceptance — primary-side cursor probe",
    )
    peer = HTTPMCPClient(
        url=PEER_URL, api_key=PEER_KEY,
        agent_name="sync-observer-peer",
        project="junto",
        role_description="§13 acceptance — peer-side cursor probe",
    )

    try:
        await primary.connect()
    except Exception as exc:
        log(f"FATAL: primary connect failed: {type(exc).__name__}: {exc}"); return 3
    try:
        await peer.connect()
    except Exception as exc:
        log(f"FATAL: peer connect failed: {type(exc).__name__}: {exc}"); return 3
    log("  both connected")

    primary_origin: Optional[str] = None
    peer_origin: Optional[str] = None
    tick = 0
    alerts_active = False

    try:
        while not stop.is_set():
            tick += 1
            p_origin, p_view = await _fetch_cursor_view(primary)
            q_origin, q_view = await _fetch_cursor_view(peer)
            primary_origin = primary_origin or p_origin
            peer_origin = peer_origin or q_origin

            lag = _compute_lag(p_view, q_view)
            pull_lag = lag.get(primary_origin or "", 0)
            push_lag = -lag.get(peer_origin or "", 0)  # peer ahead = primary behind = positive push_lag

            origins_str = ",".join(sorted(set(p_view) | set(q_view))) or "(none)"
            log(
                f"#{tick:04d} primary_origin={primary_origin!r} peer_origin={peer_origin!r} "
                f"pull_lag={pull_lag} push_lag={push_lag} origins=[{origins_str}]"
            )
            # Detail line — full cursor views, useful when debugging.
            log(f"        primary.next_cursor={p_view}")
            log(f"        peer.next_cursor={q_view}")

            over_threshold = pull_lag > args.alert_threshold or push_lag > args.alert_threshold
            if over_threshold and not alerts_active:
                log(f"  ALERT: lag exceeded threshold {args.alert_threshold}")
                alerts_active = True
            elif not over_threshold and alerts_active:
                log(f"  RECOVERY: lag back under threshold")
                alerts_active = False

            try:
                await asyncio.wait_for(stop.wait(), timeout=args.interval)
            except asyncio.TimeoutError:
                pass
    finally:
        for c in (primary, peer):
            try:
                await c.aclose()
            except Exception:
                pass

    log("DONE.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--interval", type=float, default=10.0,
                   help="poll interval seconds (default 10)")
    p.add_argument("--alert-threshold", type=int, default=50,
                   help="lag (ops) above which to log ALERT (default 50)")
    sys.exit(asyncio.run(main(p.parse_args())))
