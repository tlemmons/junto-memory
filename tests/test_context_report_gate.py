"""Pane gate of the box reporter's idle-park auto-reset (contrib/context-report)."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("mcp")

_PATH = Path(__file__).resolve().parents[1] / "contrib" / "context-report" / "junto-context-report.py"
_spec = importlib.util.spec_from_file_location("junto_context_report", _PATH)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

P = "❯"
NUDGE = "← junto-inbox: [SYSTEM NOTICE] Idle 50 min: park now (memory_get_skill('pa…"


def test_banner_after_last_prompt_ok():
    lines = [P + " do the thing", "● done", "\U0001f17f️ I AM PARKED — /clear then go", P + " "]
    assert report.analyze(lines) == (True, "ok")


def test_nudge_anchors_park_without_banner():
    lines = [P + " earlier work", "● reply", NUDGE, "  Called junto 3 times",
             "● I've parked again on the idle reminder.", "✻ Churned for 10s", P + " "]
    assert report.analyze(lines) == (True, "ok")


def test_user_prompt_after_nudge_blocks():
    lines = [NUDGE, "● parked", P + " hey, one more thing", "● sure", P + " "]
    assert report.analyze(lines) == (False, "no park banner or nudge after last prompt")


def test_no_anchor_blocks():
    lines = [P + " work", "● done", P + " "]
    assert report.analyze(lines)[0] is False


def test_busy_after_nudge_blocks():
    lines = [NUDGE, "✽ Working… (esc to interrupt)", P + " "]
    assert report.analyze(lines) == (False, "busy")
