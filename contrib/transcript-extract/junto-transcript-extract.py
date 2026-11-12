#!/usr/bin/env python3
"""Nightly transcript extraction (Memento #4, design:transcript-extraction-v0).

Reads new Claude Code session transcripts (~/.claude/projects/<dir>/*.jsonl)
for OPTED-IN project dirs only, sanitizes and redacts them, asks `claude -p`
(no tools, JSON only) for decision / explanation / resolution records, and
writes each record as a junto learning with provenance. Every record lands
UNREVIEWED (interface:verification-state-v0); the server's redaction, write-
time gate and facets pipeline all apply because writes go through MCP.

Opt-in config (JSON, default ~/.config/junto/transcript-extract.json):
    {"projects": {"-home-you-myapp": "myapp", "-home-you-src-junto-memory": "junto"},
     "model": "sonnet", "max_records_per_chunk": 5, "deidentify": true}
No config, or an empty "projects" map, means a no-op run. Opt-in is per
transcript dir, so per agent: list only the dirs of agents that record little
by hand (the 11-08 week review: value tracks how little the agent saves).
"deidentify" (default true) scrubs emails, phone numbers, IPs and long numeric
ids from the transcript before the model sees it and from every record.

Records that assert point-in-time status (deployed / merged / pending / needs
a restart) are not written: the week review found status claims were the main
source of STALE rows (review:extract-week-junto-v0 finding 4).

State (watermarks): ~/.local/state/junto-transcript-extract/state.json
    {"<transcript path>": <lines already processed>}

Usage:
    junto-transcript-extract.py               # extract + write
    junto-transcript-extract.py --dry-run     # extract + print, no writes, no watermark move
    junto-transcript-extract.py --limit 3     # at most 3 transcripts this run
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Cron's PATH has no ~/.local/bin, where the claude CLI installs (the 10-31
# extractor run failed on every transcript with "No such file: 'claude'").
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or str(Path("~/.local/bin/claude").expanduser()))

URL = os.environ.get("JUNTO_MCP_URL", "http://localhost:8080/mcp")
API_KEY = os.environ.get("JUNTO_API_KEY")
IDENTITY = "extractor"
CONFIG = Path(os.environ.get("JUNTO_EXTRACT_CONFIG", "~/.config/junto/transcript-extract.json")).expanduser()
STATE = Path(os.environ.get("JUNTO_EXTRACT_STATE",
                            "~/.local/state/junto-transcript-extract/state.json")).expanduser()
PROJECTS_DIR = Path("~/.claude/projects").expanduser()
PROMPT_FILE = Path(__file__).with_name("extract_prompt.txt")
# Customer names found by any run, per junto project, scrubbed from every later
# record. Holds customer names: local only, mode 0600, never sent anywhere.
NAMES_FILE = Path(os.environ.get("JUNTO_EXTRACT_NAMES", str(STATE.parent / "names.json"))).expanduser()

IDLE_SECONDS = 2 * 3600      # only sessions untouched for 2h (a live session is still deciding)
CHUNK_CHARS = 60_000         # per claude -p call
MAX_CHUNKS_PER_SESSION = 3   # per-session input cap = 180K chars
MAX_TOOL_INPUT_CHARS = 200   # tool_use rows keep a short hint, not the payload
DUPLICATE_RELEVANCE = 85     # skip a record when memory_query's top hit is >= this %
RUN_DEADLINE_SECONDS = int(os.environ.get("JUNTO_EXTRACT_DEADLINE", "3000"))
KINDS = ("decision", "explanation", "resolution")
DUPLICATE_PROBE_LIMIT = 3    # memory_query sorts within a relevance band, so the top row isn't always the closest

# Tools whose call means the session already saved this knowledge. Register_function
# was missing: most "known" junto rows re-described a fix registered at park.
_SAVE_TOOLS = ("memory_record_learning", "memory_store", "memory_define_spec",
               "memory_register_function")

# De-identification (condition 1 of the 11-08 keep ruling). Names can't be
# caught by regex; the prompt asks for roles instead of names.
_PII_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[ip]"),
    (re.compile(r"(?<![\w-])(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b"), "[phone]"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "[id]"),
    (re.compile(r"(?<![\w.])\d{6,}(?![\w.])"), "[id]"),
)

# Point-in-time status assertions (condition 2). A matching title drops the
# record; a matching sentence in details is removed. Deliberately narrow:
# "X requires a restart" or "the filter is applied first" can be lasting
# mechanism, so those are left to the prompt's point_in_time flag.
_STATUS_RE = re.compile(
    r"\b(?:was|were|has been|have been|got)\s+(?:now\s+)?"
    r"(?:deployed|applied|merged|shipped|released|restarted|rolled out)\b"
    r"|\b(?:is|are)\s+(?:now\s+)?(?:deployed|merged|shipped|released|pending|awaiting)\b"
    r"|\bstill\s+(?:broken|pending|open|failing|unmerged|undeployed)\b"
    r"|\bnot\s+yet\s+(?:deployed|merged|fixed|shipped|released|applied)\b",
    re.I)

# The server's redactor is the source of truth; import it from the repo when
# present so the prompt never even sees a secret. Fall back to server-side
# redaction only (writes are redacted there regardless).
_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))
try:
    from shared_memory.redact import redact_secrets
except Exception:  # pragma: no cover - box without the repo
    def redact_secrets(text):
        return text, {}


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _text_of(content):
    """User/assistant message content -> list of (kind, text)."""
    if isinstance(content, str):
        return [("text", content)]
    out = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text" and block.get("text"):
            out.append(("text", block["text"]))
        elif t == "tool_use":
            inp = block.get("input") or {}
            name = block.get("name") or ""
            if any(t in name for t in _SAVE_TOOLS):
                # What the session ALREADY saved: the extractor must not re-record it.
                hint = "ALREADY SAVED: " + str(inp.get("title") or inp.get("name") or "")
            else:
                hint = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("query") or ""
            out.append(("tool", f"[tool {name}] {str(hint)[:MAX_TOOL_INPUT_CHARS]}"))
        # tool_result and thinking blocks are dropped: results are bulk output,
        # thinking is not the agent's stated position.
    return out


def deidentify(text):
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def strip_status(record):
    """Drop point-in-time status from a record: None when the title asserts
    status or nothing lasting is left, else the record with status sentences
    removed from details."""
    if _STATUS_RE.search(record["title"]):
        return None
    sentences = re.split(r"(?<=[.!?])\s+", record["details"].strip())
    kept = [x for x in sentences if not _STATUS_RE.search(x)]
    if not kept:
        return None
    return {**record, "details": " ".join(kept)}


def sanitize(path, start_line):
    """Transcript lines after start_line -> (rendered text with L<n> markers,
    session_id, total_lines, skip_reason)."""
    rows, session_id, n = [], None, 0
    try:
        fh = open(path, errors="replace")
    except FileNotFoundError:
        # Deleted between the directory listing and now (CC prunes old
        # transcripts); skip it rather than kill the whole run.
        return "", None, 0, "transcript vanished"
    with fh:
        for n, line in enumerate(fh, 1):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            session_id = session_id or d.get("sessionId")
            if d.get("entrypoint") == "sdk-cli":
                return "", session_id, n, "headless claude -p session (librarian/extractor runs)"
            if n <= start_line or d.get("type") not in ("user", "assistant"):
                continue
            if d.get("isMeta") or d.get("isSidechain"):
                continue
            who = "USER" if d["type"] == "user" else "CLAUDE"
            for kind, text in _text_of((d.get("message") or {}).get("content")):
                text = text.strip()
                if not text or text.startswith("<command-") or text.startswith("<local-command"):
                    continue
                if kind == "text" and who == "USER" and "<system-reminder>" in text:
                    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
                    if not text:
                        continue
                rows.append(f"L{n} {who}: {text}")
    rendered, _ = redact_secrets("\n".join(rows))
    return rendered, session_id, n, None


def scrub_names(text, names):
    """Replace each name the model reported (the prompt's "names" field) with
    [name]. The model drops names from its prose only about half the time but
    lists them reliably, so code does the removal (nimbus dry-runs 2026-11-11)."""
    for name in sorted({str(n).strip() for n in names if str(n).strip()}, key=len, reverse=True):
        if len(name) < 2:
            continue
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[name]", text, flags=re.IGNORECASE)
    return text


def chunks(text):
    out, cur = [], []
    size = 0
    for line in text.splitlines():
        if size + len(line) > CHUNK_CHARS and cur:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line[:CHUNK_CHARS])
        size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out[-MAX_CHUNKS_PER_SESSION:]   # newest content wins when over the cap


NAMES_PROMPT = (
    "You are given JSON records on stdin. List every proper name in them that belongs to a person, "
    "customer, company, organization, place, or a label someone gave their own device or site "
    "(like a frame called \"Kitchen\" or \"Single Wall\"), spelled exactly as it appears. Do not list "
    "product, software or service names (Nimbus, Outlook, MongoDB, GoDaddy). "
    'Output ONLY this JSON: {"names": ["..."]}'
)


def _claude_json(system_prompt, user_prompt, stdin, model):
    """One tool-less claude -p turn -> the first JSON object in its reply, or {}."""
    # No tools, no MCP servers, no saved transcript (so the extractor never
    # extracts itself), instructions as the system prompt, and a neutral cwd so
    # no project CLAUDE.md is discovered. --bare would be cleaner but needs an
    # API key; this keeps the run on the subscription.
    cmd = [CLAUDE_BIN, "-p", user_prompt,
           "--system-prompt", system_prompt, "--output-format", "json", "--max-turns", "1",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=600,
                          cwd=tempfile.gettempdir())
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exit {proc.returncode}: {proc.stderr[:300]}")
    body = json.loads(proc.stdout).get("result", "")
    m = re.search(r"\{.*\}", body, re.DOTALL)
    return json.loads(m.group(0)) if m else {}


def extract(chunk, model, max_records, deid=True, known=None, allow=()):
    """known: this project's set of already-found names. It is read for the
    scrub and updated in place with new finds. allow: names never scrubbed."""
    prompt = PROMPT_FILE.read_text().replace("{MAX_RECORDS}", str(max_records))
    records = _claude_json(prompt, "Extract records from the transcript excerpt on stdin.",
                           chunk, model).get("records") or []
    extra_names = []
    if deid and records:
        # A focused second pass: the extract prompt alone leaves a customer name
        # in ~1 run in 4 even with its own "names" field (dry-runs 2026-11-11).
        # Raises like the first call, so the watermark stays and the run retries.
        found = _claude_json(NAMES_PROMPT, "List the names in the records on stdin.",
                             json.dumps(records), model).get("names")
        extra_names = found if isinstance(found, list) else []
    clean = []
    for r in records[:max_records]:
        if r.get("kind") not in KINDS or not r.get("title") or not r.get("details"):
            continue
        if r.get("point_in_time") is True:
            continue
        names = (r.get("names") if isinstance(r.get("names"), list) else []) + extra_names
        if deid and known is not None:
            allowed = {str(a).lower() for a in allow}
            known.update(n.strip() for n in map(str, names) if n.strip() and n.strip().lower() not in allowed)
            names = sorted(known)
        r = strip_status({k: v for k, v in r.items() if k not in ("point_in_time", "names")})
        if r is None:
            continue
        if deid:
            r["title"], r["details"] = deidentify(r["title"]), deidentify(r["details"])
            r["title"], r["details"] = scrub_names(r["title"], names), scrub_names(r["details"], names)
        clean.append(r)
    return clean


def _text(result):
    try:
        return result.content[0].text
    except Exception:
        return ""


async def server_features():
    """The server's feature switches (memory_health `features`), read before any
    model call so a disabled server costs nothing. None = the server predates
    the switch (< 1.43.1)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else None
    async with streamablehttp_client(URL, headers=headers) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            health = json.loads(_text(await s.call_tool("memory_health", {"include_storage": False})) or "{}")
    return health.get("features")


async def write_records(batch, dry_run):
    """batch: list of (junto_project, transcript_path, session_id, record)."""
    if dry_run or not batch:
        for project, path, sid, r in batch:
            print(json.dumps({"project": project, "session": sid, **r}, indent=2))
        return {"written": 0, "duplicates": 0}
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else None
    written = dupes = disabled = 0
    off_projects = set()
    async with streamablehttp_client(URL, headers=headers) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            start = json.loads(_text(await s.call_tool("memory_start_session", {
                "project": "junto", "claude_instance": IDENTITY,
                "role_description": "Nightly transcript extractor (Memento #4). Writes unreviewed learnings "
                                    "with provenance from opted-in Claude Code transcripts.",
                **({"api_key": API_KEY} if API_KEY else {}),
            })))
            sid = start.get("session_id")
            if not sid:
                raise RuntimeError(f"start_session failed: {start}")
            for project, path, cc_sid, r in batch:
                if project in off_projects:
                    disabled += 1
                    continue
                probe = json.loads(_text(await s.call_tool("memory_query", {
                    "session_id": sid, "query": f"{r['title']}\n{r['details'][:300]}",
                    "project": project, "limit": DUPLICATE_PROBE_LIMIT, "include_shared": False,
                })) or "{}")
                best = max((int(str(h.get("relevance", "0")).rstrip("%") or 0)
                            for h in probe.get("results") or []), default=0)
                if best >= DUPLICATE_RELEVANCE:
                    dupes += 1
                    continue
                details = (f"{r['details']}\n\n---\nProvenance: transcript-extract · kind={r['kind']} · "
                           f"cc_session={cc_sid} · lines {r.get('lines', '?')} · {path}")
                res = json.loads(_text(await s.call_tool("memory_record_learning", {
                    "session_id": sid, "project": project, "title": r["title"], "details": details,
                    "tags": ["transcript-extract", f"kind:{r['kind']}", f"cc-session:{cc_sid}"],
                })) or "{}")
                if res.get("code") == "transcript_extract_disabled":
                    # Switched off for this project (query_config override).
                    print(f"transcript extraction disabled on the server for {project}", file=sys.stderr)
                    off_projects.add(project)
                    disabled += 1
                    continue
                written += 1
            await s.call_tool("memory_end_session", {
                "session_id": sid, "summary": f"transcript extract: {written} written, {dupes} duplicates skipped",
            })
    out = {"written": written, "duplicates": dupes}
    if disabled:
        out["disabled"] = disabled
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="max transcripts per run")
    args = ap.parse_args()

    cfg = load_json(CONFIG, {})
    projects = cfg.get("projects") or {}
    if not projects:
        print(f"no opted-in projects in {CONFIG}; nothing to do")
        return
    try:
        features = asyncio.run(server_features())
    except Exception as e:
        print(f"cannot reach {URL} to check the server switch: {e}", file=sys.stderr)
        sys.exit(2)
    if features is None:
        print("server predates the transcript_extract switch (< 1.43.1); proceeding", file=sys.stderr)
    elif not features.get("transcript_extract"):
        print("transcript extraction is disabled on the server (query_config transcript_extract); nothing to do")
        return
    state = load_json(STATE, {})
    model = cfg.get("model", "sonnet")
    max_records = int(cfg.get("max_records_per_chunk", 5))
    deid = cfg.get("deidentify", True) is not False
    allow = cfg.get("names_allow") or []
    known = {p: set(v) for p, v in load_json(NAMES_FILE, {}).items()}
    now = time.time()

    todo = []
    for dirname, junto_project in projects.items():
        for path in sorted((PROJECTS_DIR / dirname).glob("*.jsonl")):
            try:
                if now - path.stat().st_mtime < IDLE_SECONDS:
                    continue
            except FileNotFoundError:
                continue
            todo.append((path, junto_project))
    batch, new_state, done = [], dict(state), 0
    for path, junto_project in todo:
        if done >= args.limit:
            break
        start_line = int(state.get(str(path), 0))
        text, cc_sid, total, skip = sanitize(path, start_line)
        if deid:
            text = deidentify(text)
        if skip == "transcript vanished":
            new_state.pop(str(path), None)
            continue
        if skip or total <= start_line:
            new_state[str(path)] = total
            continue
        done += 1
        try:
            for chunk in chunks(text):
                for r in extract(chunk, model, max_records, deid, known.setdefault(junto_project, set()), allow):
                    batch.append((junto_project, str(path), cc_sid, r))
            new_state[str(path)] = total
        except Exception as e:  # leave the watermark so the next run retries
            print(f"extract failed for {path}: {e}", file=sys.stderr)
    if deid:
        # A name found in a later chunk also covers the records before it.
        for project, _, _, r in batch:
            r["title"] = scrub_names(r["title"], known.get(project, ()))
            r["details"] = scrub_names(r["details"], known.get(project, ()))
    result = asyncio.run(write_records(batch, args.dry_run))
    print(f"transcripts processed: {done}, records: {len(batch)}, {result}")
    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(new_state, indent=1))
        if deid:
            NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
            NAMES_FILE.touch(mode=0o600)
            NAMES_FILE.write_text(json.dumps({p: sorted(v) for p, v in known.items() if v}, indent=1))


def _arm_deadline(seconds):
    """Hard ceiling on one run. signal.alarm is Unix-only, so a daemon timer
    thread that hard-exits works on Windows (Task Scheduler) too."""
    import threading
    t = threading.Timer(seconds, lambda: (print(f"deadline {seconds}s hit; exiting", file=sys.stderr),
                                          os._exit(3)))
    t.daemon = True
    t.start()


if __name__ == "__main__":
    _arm_deadline(RUN_DEADLINE_SECONDS)
    main()
