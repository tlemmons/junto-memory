"""State-spec write guard — make silent loss at park visible.

interface:state-spec-write-guard-v0 (backlog_889bbe240666). memory_define_spec
replaces content, so a line an agent omits when re-writing its state spec is
deleted with no trace (learning_8f912797b5eebed8 — e.g. "NEVER `-Recurse`
D:\\Media" vanished from state:server-team 1.1.228 → 1.1.229).

ADVISORY ONLY: report which standing fragments of the previous version appear
nowhere in the new content. Never blocks; the >50% shrink guard in
tools/specs.py is separate and unchanged. Pure functions, no I/O.
"""

import re
from typing import List

# Headings whose content is meant to persist across parks. Status / Next Steps
# churn legitimately every park, so they are deliberately NOT diffed.
STANDING_HEADING = re.compile(
    r"key context|standing|rules|don'?t repeat|gotcha|never|warning", re.IGNORECASE
)
# Only RULE-SHAPED fragments are reported. Replay over every fleet state write
# 05-14→10-13 (5,375 writes): all standing fragments flagged 82% of parks
# (median 3) — Key Context is reworded every park, so that is noise agents
# would learn to skip. Rule-shaped only: 38% of parks, mean 1.07, and it still
# catches the server-team 1.1.229 "NEVER `-Recurse` D:\\Media" loss.
RULE_SHAPED = re.compile(
    r"\b(NEVER|ALWAYS|DON'?T|DO NOT|MUST(?: NOT)?|forbidden|no approval)\b|⚠️|🚨|⛔",
    re.IGNORECASE,
)
MIN_FRAGMENT_CHARS = 12
MAX_REPORTED = 25

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_LEAD = re.compile(r"^(?:[-*•>]|\d+[.)]|⚠️|\s)+")
_SPLIT = re.compile(r" · |; ")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def standing_fragments(content: str) -> List[str]:
    """Fragments (one line split on ' · ' / '; ') under standing headings.

    A standing section runs until the next heading of the same or higher level.
    """
    out: List[str] = []
    level = None  # level of the standing heading we are inside, if any
    for raw in content.splitlines():
        m = _HEADING.match(raw)
        if m:
            depth = len(m.group(1))
            if level is not None and depth <= level:
                level = None
            if level is None and STANDING_HEADING.search(m.group(2)):
                level = depth
            continue
        if level is None:
            continue
        line = _LEAD.sub("", raw).strip()
        for frag in _SPLIT.split(line):
            frag = frag.strip()
            if len(frag) >= MIN_FRAGMENT_CHARS:
                out.append(frag)
    return out


def dropped_standing(old_content: str, new_content: str) -> List[str]:
    """Rule-shaped standing fragments of old_content found nowhere in new_content.

    Matching is case/whitespace-insensitive substring against the WHOLE new
    content, so a fragment moved to another section does not count as dropped.
    """
    new_norm = _norm("\n".join(_LEAD.sub("", ln) for ln in new_content.splitlines()))
    seen = set()
    dropped: List[str] = []
    for frag in standing_fragments(old_content):
        if not RULE_SHAPED.search(frag):
            continue
        key = _norm(frag)
        if key in seen:
            continue
        seen.add(key)
        if key not in new_norm:
            dropped.append(frag)
    return dropped
