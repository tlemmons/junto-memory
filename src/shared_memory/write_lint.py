"""Write-side lint — envelope-leak strip/re-route + dangling-ref advisory.

Two gates applied where agent-authored text enters the store
(backlog_1115f9fe35f7 + backlog_d03297e01f30, design settled 2026-08-01..06):

1. ENVELOPE-LEAK STRIP (strip_envelope_leak): malformed client tool-call
   emissions serialize the call's own XML envelope into a text param — the
   observed shape ends with a literal ``</learnings>`` followed by
   ``<parameter name="handoff_notes">…`` and sometimes ``</invoke>``
   (8 confirmed docs, 3 distinct callers, 2026-07-29..08-03). Detection
   invariant (mobile-team, msg_fe2efe1eb2dc): a body containing a literal
   closing tag for the field it is being written INTO is corrupt by
   construction — no legitimate body closes its own enclosing field.
   Remedy is STRIP + RE-ROUTE, never reject: every observed leak carried
   substantive swallowed content (librarian 08-01); rejection destroys it.

2. DANGLING-REF ADVISORY (find_unresolved_refs): ID-shaped references that
   don't resolve read as tracked work and actively SUPPRESS the verify
   instinct (coordinator's learning_344598e3f255f1ad — whose author then
   violated it within four days). Advisory-only, returned in the tool
   RESPONSE (the artifact-level chokepoint — a warning that arrives before
   the sender's next action is a gate; anything later is a feed). Never
   rejects: legitimate unresolvables exist (docs in projects the checker
   can't see, deliberately-illustrative example ids).
"""

import re
from typing import Dict, List, Tuple

# Fields whose literal closing tag inside their own value marks a leak.
# end_session(learnings=..., handoff_notes=...) is the observed emitter;
# record_learning(details=...) is guarded with the same set because the
# leaked envelopes observed so far always carry these param names.
_ENVELOPE_FIELDS = ("learnings", "handoff_notes", "details", "summary")

# <parameter name="xyz">content</parameter-or-field-close-or-EOF>
_PARAM_BLOCK_RE = re.compile(
    r'<parameter\s+name="(?P<name>[A-Za-z_][A-Za-z0-9_]*)">'
    r"(?P<content>.*?)"
    r"(?:</(?P=name)>|</parameter>|(?=<parameter\s+name=)|$)",
    re.DOTALL,
)

# Trailing envelope debris worth removing wherever it appears after a leak
# point: closing invoke/function_calls tags in either plain or namespaced form.
_DEBRIS_RE = re.compile(
    r"</?(?:antml:)?(?:invoke|function_calls|parameter)>\s*", re.IGNORECASE
)

# ID-shaped references. Hex length in the corpus runs 12-16; accept 6+ so
# truncated citations are still checked rather than silently skipped.
_REF_RE = re.compile(
    r"\b(?P<ref>(?:learning|backlog|msg|spec|func|skill|handoff)_[0-9a-f]{6,16})\b"
)


def strip_envelope_leak(body: str, field_name: str) -> Tuple[str, Dict[str, str], bool]:
    """Detect + strip a serialized tool-call envelope from ``body``.

    Returns (clean_body, extracted_params, leaked).
      - clean_body: text up to the leak point, envelope debris removed.
      - extracted_params: {param_name: content} for every ``<parameter>``
        block found after the leak point (the swallowed sibling params —
        re-route these to their proper fields; they are substantive).
      - leaked: True when the closing-tag invariant fired.

    The scan looks for the literal closing tag of the field being written
    (``</learnings>`` inside the learnings value, etc.) and, defensively,
    the closing tag of any known envelope field — the corpus shows the
    enclosing field's own tag first in all 8 instances, but a sibling's
    tag appearing at all is equally impossible in legitimate text.
    """
    if not body:
        return body, {}, False

    cut = -1
    cut_end = -1
    candidates = [field_name] + [f for f in _ENVELOPE_FIELDS if f != field_name]
    for fname in candidates:
        idx = body.find(f"</{fname}>")
        if idx != -1 and (cut == -1 or idx < cut):
            cut = idx
            cut_end = idx + len(f"</{fname}>")
    if cut == -1:
        return body, {}, False

    # ENVELOPE-TAIL REQUIREMENT. Three weeks of remediation threads QUOTE the
    # leak pattern in prose, so a bare closing tag is not evidence (a corpus
    # sweep found 40+ discussion docs vs 10 real leaks). The discriminator:
    # in a REAL leak the field's closing tag is immediately followed by more
    # ENVELOPE — nothing else can legitimately sit there — whereas a
    # discussion doc continues in prose ("...ends with </learnings> then a
    # <parameter> block, which the lint strips").
    #
    # Supersedes the 2026-08-07 "body must END in a closing token" rule, which
    # was correct about false positives but produced a FALSE NEGATIVE on a
    # truncated emission whose tail is an UNTERMINATED parameter — observed
    # live 2026-08-08 (learning_24b33b8aa7ff16f1, learning_f588ce30c5b5c9a4:
    # `</details>\n<parameter name="project">nimbus`, ending in a bare value).
    # Caught by the librarian acting as the corpus verification layer.
    _tail = body[cut_end:].lstrip()
    if _tail and not _tail.startswith(("<parameter", "</", "<", "<function_calls")):
        return body, {}, False

    clean = body[:cut].rstrip()
    tail = body[cut:]

    extracted: Dict[str, str] = {}
    for m in _PARAM_BLOCK_RE.finditer(tail):
        content = _DEBRIS_RE.sub("", m.group("content")).strip()
        if content:
            extracted[m.group("name")] = content

    # Any debris that leaked into the clean half (rare — leak point is the
    # first envelope token in all observed instances) gets swept too.
    clean = _DEBRIS_RE.sub("", clean).rstrip()
    return clean, extracted, True


def extract_refs(text: str) -> List[str]:
    """All distinct ID-shaped references in ``text``, in first-seen order."""
    seen: List[str] = []
    for m in _REF_RE.finditer(text or ""):
        ref = m.group("ref")
        if ref not in seen:
            seen.append(ref)
    return seen


async def find_unresolved_refs(text: str, db, chroma, project: str) -> List[str]:
    """Existence-check ID refs in ``text``. Returns refs NOT found in the
    scopes this checker can see (caller's project collection + shared
    collections + mongo messages/backlog surfaces). Advisory data only —
    the caller phrases it as "verify if typed from memory", never rejects.

    Best-effort by contract: any per-ref lookup failure treats the ref as
    resolved (silence beats a false alarm from an infra hiccup).
    """
    refs = extract_refs(text)
    if not refs:
        return []

    unresolved: List[str] = []
    chroma_ids = [r for r in refs if not r.startswith("msg_")]
    msg_ids = [r for r in refs if r.startswith("msg_")]

    # msg_* live in mongo with the msg-string as _id.
    for ref in msg_ids:
        try:
            if db is not None and db.messages.find_one({"_id": ref}, {"_id": 1}) is None:
                unresolved.append(ref)
        except Exception:
            pass

    if chroma_ids and chroma is not None:
        # Check the writer's own project collection first, then the shared
        # collections — the scopes a same-project reader would search.
        from shared_memory.helpers import get_project_collection, get_shared_collection

        collections = []
        try:
            if project:
                collections.append(await get_project_collection(chroma, project))
        except Exception:
            pass
        for shared_name in ("patterns", "context", "work"):
            try:
                collections.append(await get_shared_collection(chroma, shared_name))
            except Exception:
                pass

        remaining = set(chroma_ids)
        for coll in collections:
            if not remaining:
                break
            try:
                got = await coll.get(ids=list(remaining), include=[])
                for found in got.get("ids") or []:
                    remaining.discard(found)
            except Exception:
                # Some chroma builds raise on fully-unknown id sets; fall
                # back to per-id gets so one bad id can't mask the rest.
                for rid in list(remaining):
                    try:
                        got = await coll.get(ids=[rid], include=[])
                        if got.get("ids"):
                            remaining.discard(rid)
                    except Exception:
                        pass
        unresolved.extend(sorted(remaining))

    return unresolved


def advisory_payload(unresolved: List[str]) -> Dict:
    """Standard response fragment for the dangling-ref advisory."""
    return {
        "unresolved_refs": unresolved,
        "unresolved_refs_note": (
            "These ID-shaped references were not found in the scopes this "
            "server can check (your project + shared + messages). If you "
            "typed any from memory, verify with memory_get_by_id before "
            "relying on it — a dangling ID reads as tracked work and "
            "suppresses the instinct to check. Cross-project references "
            "may be false positives; this is advisory, the write succeeded."
        ),
    }
