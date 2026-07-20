"""Write-time facets on learnings (design:memory-facets-v0 v0.3.0).

Four structured fields extracted once at write time so retrieval consumers
(sub's rater, the future task-declared preload, plain memory_query readers)
get indexing signal a topic-embedding cannot carry:

  claim      one-sentence assertion — interface:claim-extraction-v0 v1.0.2
             VERBATIM, shared claim cache (`learning_claims`) with the
             write-time gate; never re-extracted when the gate already did.
  operation  activity class, closed enum (PROVISIONAL per spec §operation-enum:
             diagnose | deploy | config | build | query | process); no-match
             → omitted.
  trigger    ≤3 applies-when conditions, free text, format convention
             "when <observable condition>" / "before <action> on <object>".
             Consumers may not HARD-rely on it until the spec's calibration
             gate passes.
  shelf_life durable | volatile. The classification question is deliberately
             NOT world-state-vs-code-knowledge but "could later evidence
             invalidate this?" (sub amendment msg_0af19e210279): diagnoses /
             root-cause / definitive-X claims are volatile BY DEFAULT even
             when they look durable by topic — that's where the labeled
             HARMFUL injections live.

Storage: mongo `learning_facets`, one row per learning doc id, tagged with
FACETS_RECIPE_VERSION for idempotent backfill. Chroma metadata mirrors ONLY
`facet_operation` (single scalar — multi-key Chroma where-filters silently
return nothing, the 28570d4 gotcha; filtering stays single-key + Python).

Failure posture: identical to the claim gate — ADVISORY, NEVER BLOCKS. The
learning write has already returned by the time extraction runs (fire-and-
forget task); every failure degrades to "no facets", which the contract makes
legal forever (the container is optional; backfill covers gaps).

Config (env):
  JUNTO_FACETS_ENABLED  "true" to enable (default false — dormant).
  Model/endpoint: shares JUNTO_CLAIM_GATE_URL / JUNTO_CLAIM_GATE_MODEL.
"""

import asyncio
import logging
import os
from typing import Dict, List, Optional

from shared_memory import claim_gate

logger = logging.getLogger(__name__)

# Bump with spec §facets changes; a mismatch marks a row for backfill re-run.
FACETS_RECIPE_VERSION = "0.3.0"

FACETS_COLLECTION = "learning_facets"

OPERATION_ENUM = ("diagnose", "deploy", "config", "build", "query", "process")

OPERATION_SYSTEM = (
    "Classify the primary activity this engineering note is about, as exactly "
    "one word from this list:\n"
    "diagnose — debugging, root-causing, explaining a failure\n"
    "deploy — releasing, restarting, shipping, rollout\n"
    "config — settings, env vars, infrastructure setup\n"
    "build — implementing code, features, schemas\n"
    "query — looking up, reading, searching data or docs\n"
    "process — workflow, coordination, procedure, policy\n"
    "Reply with ONLY the one word. If none fits, reply NONE."
)

SHELF_LIFE_SYSTEM = (
    "Decide whether this engineering note is DURABLE or VOLATILE knowledge.\n"
    "The test is exactly this: could later evidence plausibly invalidate the "
    "note's claim?\n"
    "VOLATILE — diagnoses, root-cause conclusions, 'definitive' explanations "
    "(later evidence can overturn them even when the topic is code), and any "
    "snapshot of live state (locks, deploy state, queue contents, who is "
    "working on what, open invoices).\n"
    "DURABLE — how a mechanism works, API behavior, gotchas about code "
    "behavior, constraints and formats: true until the code itself changes.\n"
    "Reply with ONLY one word: DURABLE or VOLATILE."
)

TRIGGER_SYSTEM = (
    "List the conditions under which a future engineer should look this note "
    "up. At most 3 conditions, one per line, each phrased exactly as "
    "\"when <observable condition>\" or \"before <action> on <object>\". "
    "Be concrete and specific to this note — name the actual symptom, "
    "command, file, or subsystem. No preamble, no numbering, no bullets."
)

TRIGGER_MAX_LINES = 3
TRIGGER_MAX_CHARS = 200
TRIGGER_MAX_TOKENS = 150
ONE_WORD_MAX_TOKENS = 8

# Strong references to in-flight extraction tasks — asyncio only keeps weak
# refs, so without this a fire-and-forget task can be GC'd mid-run.
_inflight: set = set()


def facets_enabled() -> bool:
    return os.environ.get("JUNTO_FACETS_ENABLED", "false").lower() in (
        "1", "true", "yes",
    )


# ── per-facet extraction (each returns None on any soft failure) ────────────

def _parse_one_word(raw: str, allowed) -> Optional[str]:
    """Match a one-word completion against an allow-list, case-insensitive.
    Substring parse, earliest hit wins (same posture as the gate's classify
    parse); no hit → None (fail-quiet omit)."""
    upper = (raw or "").upper()
    hits = [(upper.find(w.upper()), w) for w in allowed if w.upper() in upper]
    if not hits:
        return None
    return min(hits)[1]


async def extract_operation(client, title: str, content: str) -> Optional[str]:
    raw = await claim_gate._chat(
        client, OPERATION_SYSTEM,
        f"{title}\n\n{content[:claim_gate.CONTENT_HEAD_CHARS]}",
        ONE_WORD_MAX_TOKENS,
    )
    return _parse_one_word(raw, OPERATION_ENUM)


async def extract_shelf_life(client, title: str, content: str) -> Optional[str]:
    raw = await claim_gate._chat(
        client, SHELF_LIFE_SYSTEM,
        f"{title}\n\n{content[:claim_gate.CONTENT_HEAD_CHARS]}",
        ONE_WORD_MAX_TOKENS,
    )
    word = _parse_one_word(raw, ("DURABLE", "VOLATILE"))
    return word.lower() if word else None


async def extract_trigger(client, title: str, content: str) -> List[str]:
    raw = await claim_gate._chat(
        client, TRIGGER_SYSTEM,
        f"{title}\n\n{content[:claim_gate.CONTENT_HEAD_CHARS]}",
        TRIGGER_MAX_TOKENS,
    )
    lines = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-*•0123456789. ").strip()
        if not line:
            continue
        low = line.lower()
        # Enforce the format convention — a line that doesn't open with the
        # convention is a model digression, not a trigger; drop it.
        if not (low.startswith("when ") or low.startswith("before ")):
            continue
        lines.append(line[:TRIGGER_MAX_CHARS])
        if len(lines) >= TRIGGER_MAX_LINES:
            break
    return lines


# ── storage ─────────────────────────────────────────────────────────────────

def _store_facets(db, doc_id: str, facets: Dict) -> None:
    if db is None:
        return
    from shared_memory.helpers import utc_now_iso

    row = {k: v for k, v in facets.items() if v}
    row.update({
        "recipe_version": FACETS_RECIPE_VERSION,
        "model": claim_gate._gate_model(),
        "extracted_at": utc_now_iso(),
    })
    db[FACETS_COLLECTION].update_one({"_id": doc_id}, {"$set": row}, upsert=True)


async def _mirror_operation_to_chroma(collection, doc_id: str, operation: str) -> None:
    """Best-effort single-scalar metadata mirror. Merges into the existing
    metadata dict (Chroma update replaces the metadata payload wholesale, so a
    naive one-key update would erase title/type/status)."""
    got = await collection.get(ids=[doc_id], include=["metadatas"])
    metas = got.get("metadatas") or []
    if not metas or metas[0] is None:
        return
    meta = dict(metas[0])
    meta["facet_operation"] = operation
    await collection.update(ids=[doc_id], metadatas=[meta])


def get_facets_for_ids(db, doc_ids: List[str]) -> Dict[str, Dict]:
    """Batched read for the delivery-surface guarantee (spec §consumer):
    memory_query attaches facets INLINE to result rows — consumers must never
    need a per-candidate get_by_id round-trip. Returns {doc_id: facets_dict}
    with mongo plumbing stripped; empty dict on any failure."""
    if db is None or not doc_ids:
        return {}
    try:
        out = {}
        for row in db[FACETS_COLLECTION].find({"_id": {"$in": doc_ids}}):
            doc_id = row.pop("_id")
            out[doc_id] = row
        return out
    except Exception as e:  # noqa: BLE001 - delivery is best-effort
        logger.warning("facets: batched read failed (%s)", e)
        return {}


# ── orchestrator ────────────────────────────────────────────────────────────

async def _extract_and_store(db, collection, doc_id: str, title: str,
                             details: str) -> None:
    """The whole pipeline for one learning. Runs detached from the recording
    request — every failure logs and dies quietly here."""
    try:
        import httpx  # lazy, mirrors the gate

        timeout = httpx.Timeout(60.0, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            # claim: the gate may have already extracted+cached it this write.
            claim = claim_gate._cached_claim(db, doc_id)
            if not claim:
                claim = await claim_gate.extract_claim(client, title, details)
                if claim:
                    claim_gate._cache_claim(db, doc_id, claim)
            if not claim:
                # Contract: claim is GUARANTEED non-empty when the facets
                # container exists — no claim means no container this pass
                # (backfill retries under its own recipe stamp).
                logger.warning("facets: empty claim for %s — skipping", doc_id)
                return

            operation = await extract_operation(client, title, details)
            shelf_life = await extract_shelf_life(client, title, details)
            trigger = await extract_trigger(client, title, details)

        _store_facets(db, doc_id, {
            "claim": claim,
            "operation": operation,
            "trigger": trigger,
            "shelf_life": shelf_life,
        })
        if operation:
            await _mirror_operation_to_chroma(collection, doc_id, operation)
        logger.info(
            "facets: stored for %s (operation=%s shelf_life=%s triggers=%d)",
            doc_id, operation, shelf_life, len(trigger),
        )
    except Exception as e:  # noqa: BLE001 - advisory path
        logger.warning("facets: extraction failed for %s (%s) — skipped", doc_id, e)


def schedule_facet_extraction(db, collection, doc_id: str, title: str,
                              details: str) -> bool:
    """Fire-and-forget entry point called by memory_record_learning AFTER the
    write has landed. Returns True when a task was scheduled. Never raises —
    the write is the load-bearing operation."""
    if not facets_enabled():
        return False
    try:
        task = asyncio.create_task(
            _extract_and_store(db, collection, doc_id, title, details),
            name=f"facets_{doc_id}",
        )
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
        return True
    except Exception as e:  # noqa: BLE001 - e.g. no running loop in exotic contexts
        logger.warning("facets: could not schedule extraction for %s (%s)", doc_id, e)
        return False
