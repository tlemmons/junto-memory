"""Code-defined GLOBAL behavioral guidelines — the source of truth for scope="global".

These seed db.guidelines on server startup (see seed_global_guidelines, called from
clients.py). The runtime fetch path (get_guidelines_for_session) is unchanged — it
still reads db.guidelines; this module just guarantees the global rows match the
deployed code on every boot, so a guidance change travels with the deploy to every
server (home AND the isolated work box) without federating any data.

SCOPE DISCIPLINE: this file ONLY manages scope="global". Project-scoped guidance
(scope="<project>") stays DB-resident and owner-managed per server; the seed never
reads, writes, or deletes it.

TO CHANGE A GLOBAL GUIDELINE: edit it HERE and deploy. The seed upserts by name and
is idempotent (it only writes a row when the content actually differs, stamping
updated_by="code-seed"). Editing a global live via memory_guidelines is no longer
the source of truth — the next restart re-asserts the values in this file.
"""

GLOBAL_GUIDELINES = [
    {
        "name": 'mandatory_memory_query',
        "priority": 3,
        "rule": '''MEMORY FIRST — AT THE ARTIFACT. Call memory_query and/or memory_find_function at these five moments. Every trigger is an observable action — a thing you can watch yourself doing — NEVER a feeling of uncertainty:

(1) ABOUT TO RUN: before executing a build, SQL, deploy, or config command — the exact procedure is likely recorded.
(2) ABOUT TO SEND: before sending a message that asserts a factual claim about system behavior or state you did not verify THIS session — query the claim's topic first. Asides and rationale sentences count the same as conclusions: recent cross-project misassertions all traveled as asides attached to routing messages, not as conclusions anyone scrutinised.
(3) ABOUT TO PROBE: before empirically probing a DESIGNED subsystem to explain its behavior (SSH, logs, DB, grep) — pull its design and prior learnings first. Live symptoms are ambiguous without the design in hand; "reading reality" is not exempt.
(4) ABOUT TO RECORD: before recording a learning that contradicts, supersedes, or surprises — query first. The server's write-time gate backstops this trigger, but the gate is advisory; the query is still yours.
(5) ABOUT TO ASK: before asking the user for build steps, credentials, paths, or process — they are probably recorded.

The knowledge base has 200+ learnings covering build processes, signing configs, deployment steps, database schemas, API gotchas, debug solutions, and platform-specific workarounds. Treat it like a senior teammate you would never bypass for trial-and-error.

WHY THE TRIGGERS CHANGED (2026-07-20): the old rule fired on "before you guess" — and nobody ever experiences themselves guessing. Two rules with that trigger shape failed on the identical failure signature (2026-07 mesh-offline misdiagnosis; 2026-07-20 parked-agent rederivation) even though one of them named the failure precisely. Rules that fire on an inspectable artifact get followed (db_write_safety, anti_sycophancy); rules that ask you to detect an absence do not. The former memory_first_designed_system rule is MERGED into trigger (3) — do not re-add it separately.''',
    },
    {
        "name": 'session_length_discipline',
        "priority": 8,
        "rule": '''RUN LONGER SESSIONS. On 1M-context models the park signal is TASK COMPLETION at a clean stopping point — not token count, not exchange count. Parking early costs more than it saves.

- <500K tokens: keep working. Do not park mid-task "to preserve context." The old "100 exchanges" / "1-3 tasks" rules were calibrated to 200K and do not apply.
- 500-800K: watch for real degradation symptoms — re-reading files you already read, re-asking settled questions, contradicting earlier decisions. Park at the next clean stop.
- >800K: park even mid-task, with handoff notes.
- Coordinator: shift the bands ~150-200K lower (channel messages and spec pulls are large).
- Always: if the user says park, park.

Any park recommendation must cite a token count or a named symptom. "Feels long" is not evidence.''',
    },
]


def seed_global_guidelines(db) -> dict:
    """Idempotent upsert of GLOBAL_GUIDELINES into db.guidelines.

    Writes a global row only when it is missing or its rule/priority/active differs
    from the code, so a no-change boot does zero writes (no timestamp churn). Stamps
    updated_by="code-seed" on anything it writes, so live-vs-code drift is visible in
    memory_guidelines(action="list"). NEVER touches non-global rows.

    Returns a summary dict {inserted, updated, unchanged, orphans}. orphans = active
    global rows present in the DB but absent from the code (logged, NOT deleted — a
    conservative v1 so the seed can never destroy a row on first run against an
    existing DB; reconcile/removal is a deliberate follow-up, not an automatic boot
    side effect).
    """
    import logging

    from shared_memory.helpers import utc_now_iso

    log = logging.getLogger(__name__)
    if db is None:
        return {"inserted": 0, "updated": 0, "unchanged": 0, "orphans": 0}

    code_names = set()
    inserted = updated = unchanged = 0
    now = utc_now_iso()

    for g in GLOBAL_GUIDELINES:
        name = g["name"]
        code_names.add(name)
        rule = g["rule"]
        priority = max(1, min(100, int(g.get("priority", 50))))
        existing = db.guidelines.find_one({"name": name})
        if (existing
                and existing.get("rule") == rule
                and existing.get("priority") == priority
                and existing.get("scope") == "global"
                and existing.get("active", True) is True):
            unchanged += 1
            continue
        db.guidelines.update_one(
            {"name": name},
            {"$set": {
                "name": name,
                "rule": rule,
                "scope": "global",
                "priority": priority,
                "active": True,
                "updated": now,
                "updated_by": "code-seed",
            }},
            upsert=True,
        )
        if existing:
            updated += 1
        else:
            inserted += 1

    # Drift detection only — active global rows not in code. Do NOT delete.
    orphans = []
    for doc in db.guidelines.find({"scope": "global", "active": True}, {"name": 1}):
        if doc["name"] not in code_names:
            orphans.append(doc["name"])
    if orphans:
        log.warning(
            "seed_global_guidelines: %d active global row(s) in DB not in code "
            "(left untouched — reconcile manually if intended): %s",
            len(orphans), ", ".join(sorted(orphans)),
        )

    log.info(
        "seed_global_guidelines: %d inserted, %d updated, %d unchanged, %d orphan(s)",
        inserted, updated, unchanged, len(orphans),
    )
    return {"inserted": inserted, "updated": updated,
            "unchanged": unchanged, "orphans": len(orphans)}
