"""Transcript-extraction keep conditions (Tom 2026-11-10, review:extract-week-junto-v0).

Pins:
- de-identify scrubs emails, IPs, phone numbers, uuids and long numeric ids,
  and leaves doc ids, ports and short numbers alone;
- status sentences are stripped from details and a status title drops the
  record, while lasting mechanism ("requires a restart", "is applied first")
  survives;
- extract() drops point_in_time records and de-identifies the rest;
- a memory_register_function call counts as ALREADY SAVED;
- unreviewed extractor rows are flagged for demotion; verified ones are not.
- a transcript deleted before it is read is skipped, not fatal.
- names the model reports (its "names" field, plus a focused second pass)
  are scrubbed to [name], and names found by earlier runs stay scrubbed.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

from shared_memory.decay import is_unreviewed_extract

_PATH = Path(__file__).resolve().parents[1] / "contrib" / "transcript-extract" / "junto-transcript-extract.py"
_spec = importlib.util.spec_from_file_location("junto_transcript_extract", _PATH)
tx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tx)


def test_deidentify_scrubs_contact_and_ids():
    out = tx.deidentify("Mail jane.doe@example.com from 192.168.15.98:8080, call 555-123-4567 "
                        "or +1 (555) 123-4567; user 4815162342, uuid 123e4567-e89b-12d3-a456-426614174000.")
    for raw in ("jane.doe@example.com", "192.168.15.98", "555-123-4567", "4815162342", "123e4567"):
        assert raw not in out
    assert "[email]" in out and "[ip]" in out and "[phone]" in out and "[id]" in out


def test_deidentify_keeps_technical_tokens():
    text = "learning_bc08b2c0df60f6cf on port 27019, v1.45.0, 3547 rows, commit 70cd370."
    assert tx.deidentify(text) == text


def test_strip_status_removes_status_sentences():
    r = tx.strip_status({"title": "Facets selection filter uses $nin", "details":
                         "The librarian filters on source_status. The fix was deployed on 11-09. "
                         "Rows with no source_status are kept."})
    assert "deployed" not in r["details"]
    assert r["details"].startswith("The librarian filters") and r["details"].endswith("are kept.")


def test_strip_status_drops_status_title_and_empty_details():
    assert tx.strip_status({"title": "Fix is now merged to main", "details": "Lasting fact."}) is None
    assert tx.strip_status({"title": "Login fix", "details": "It is still broken on prod."}) is None


def test_strip_status_keeps_lasting_mechanism():
    r = {"title": "DB env is read once at boot", "details":
         "DB_<NAME>_* is read at boot, so changing it requires a restart. The filter is applied before limit."}
    assert tx.strip_status(r) == r


def test_extract_filters_point_in_time_and_deidentifies(monkeypatch):
    records = [
        {"kind": "decision", "title": "Use $nin for source_status", "details":
         "Most rows have no source_status, so filter superseded rows out. Asked bob@example.com.",
         "lines": "L1-L9", "point_in_time": False},
        {"kind": "explanation", "title": "Extractor backlog", "details": "286 rows are unreviewed.",
         "lines": "L10", "point_in_time": True},
        {"kind": "opinion", "title": "x", "details": "y"},
    ]

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps({"records": records})}), "")

    monkeypatch.setattr(tx.subprocess, "run", fake_run)
    out = tx.extract("L1 USER: hi", "sonnet", 5)
    assert len(out) == 1
    assert "bob@example.com" not in out[0]["details"] and "[email]" in out[0]["details"]
    assert "point_in_time" not in out[0]
    raw = tx.extract("L1 USER: hi", "sonnet", 5, deid=False)
    assert "bob@example.com" in raw[0]["details"]


def test_register_function_counts_as_already_saved():
    rows = tx._text_of([{"type": "tool_use", "name": "mcp__junto__memory_register_function",
                         "input": {"name": "session_user", "file": "src/x.py:1"}}])
    assert rows == [("tool", "[tool mcp__junto__memory_register_function] ALREADY SAVED: session_user")]


def test_unreviewed_extract_flag():
    assert is_unreviewed_extract("extractor", "unreviewed")
    assert is_unreviewed_extract("extractor", None)
    assert not is_unreviewed_extract("extractor", "verified")
    assert not is_unreviewed_extract("memory", "unreviewed")


def test_sanitize_skips_vanished_transcript(tmp_path):
    # CC can prune a transcript between the glob and the read; one vanished
    # file must be skipped, not crash the whole nightly run.
    assert tx.sanitize(tmp_path / "gone.jsonl", 0) == ("", None, 0, "transcript vanished")


def test_extract_scrubs_reported_names(monkeypatch):
    records = [{"kind": "resolution", "title": "Four black Shimmick screens on 'Single Wall' traced to hardware",
                "details": "The Shimmick frames went black; Single Wall goes to the bench. Wallboard is unaffected.",
                "lines": "L1-L9", "names": ["Shimmick", "Single Wall", ""], "point_in_time": False}]

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps({"records": records})}), "")

    monkeypatch.setattr(tx.subprocess, "run", fake_run)
    out = tx.extract("L1 USER: hi", "sonnet", 5)[0]
    assert out["title"] == "Four black [name] screens on '[name]' traced to hardware"
    assert out["details"] == "The [name] frames went black; [name] goes to the bench. Wallboard is unaffected."
    assert "names" not in out
    assert tx.extract("L1 USER: hi", "sonnet", 5, deid=False)[0]["title"].startswith("Four black Shimmick")


def test_extract_names_pass_catches_unreported_names(monkeypatch):
    records = [{"kind": "resolution", "title": "Shimmick's black screens traced to hardware",
                "details": "Bring back Single Wall first.", "lines": "L1", "names": [], "point_in_time": False}]
    calls = []

    def fake_run(cmd, input=None, **kw):
        calls.append(input)
        body = {"records": records} if len(calls) == 1 else {"names": ["Shimmick", "Single Wall"]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(body)}), "")

    monkeypatch.setattr(tx.subprocess, "run", fake_run)
    out = tx.extract("L1 USER: hi", "sonnet", 5)[0]
    assert out["title"] == "[name]'s black screens traced to hardware"
    assert out["details"] == "Bring back [name] first."
    assert len(calls) == 2 and "Shimmick" in calls[1]
    calls.clear()
    tx.extract("L1 USER: hi", "sonnet", 5, deid=False)
    assert len(calls) == 1   # no names pass when de-identification is off


def test_extract_known_names_scrub_and_grow(monkeypatch):
    records = [{"kind": "resolution", "title": "Shimmick frames went black",
                "details": "GoDaddy mail and the Kitchen frame were unrelated.", "lines": "L1",
                "names": ["GoDaddy", "Kitchen"], "point_in_time": False}]

    def fake_run(cmd, input=None, **kw):
        body = {"names": []} if "List the names" in cmd[2] else {"records": records}
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(body)}), "")

    monkeypatch.setattr(tx.subprocess, "run", fake_run)
    known = {"Shimmick"}   # found by an earlier run; neither pass reports it this time
    out = tx.extract("L1 USER: hi", "sonnet", 5, known=known, allow=["godaddy"])[0]
    assert out["title"] == "[name] frames went black"
    assert out["details"] == "GoDaddy mail and the [name] frame were unrelated."
    assert known == {"Shimmick", "Kitchen"}


def test_scrub_names_case_rules():
    assert tx.scrub_names("Bill asked us to bill him monthly.", ["Bill"]) == "[name] asked us to bill him monthly."
    assert tx.scrub_names("Mail from Weizhang and weizhang@", ["weizhang"]) == "Mail from [name] and [name]@"
