"""System-version labels derived from tags (interface:knowledge-versioning-v0).

Tags are the source of truth: `sys:old` | `sys:rebuild` | `sys:both` say which
system an entry applies to, `valid-until:<event>` says when it stops being
true. Nothing is stored; result rows get `applies_to` / `valid_until` computed
here at read time, and the keys are ABSENT on untagged entries.
"""

import json
from typing import Dict, Iterable, Union

SYS_PREFIX = "sys:"
VALID_UNTIL_PREFIX = "valid-until:"
SYS_VALUES = ("old", "rebuild", "both")


def _tag_list(tags: Union[str, Iterable[str], None]) -> list:
    # Chroma metadata stores tags as a JSON string; callers may also pass a list.
    if not tags:
        return []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (ValueError, TypeError):
            return []
    return [t for t in tags if isinstance(t, str)]


def version_labels(tags: Union[str, Iterable[str], None]) -> Dict[str, str]:
    """{"applies_to": ..., "valid_until": ...} for the tags, omitting absent keys.

    sys:old together with sys:rebuild reads as "both". Unknown sys:* values are
    ignored. The first valid-until tag wins.
    """
    systems = set()
    valid_until = None
    for t in _tag_list(tags):
        low = t.strip().lower()
        if low.startswith(SYS_PREFIX):
            value = low[len(SYS_PREFIX):]
            if value in SYS_VALUES:
                systems.add(value)
        elif low.startswith(VALID_UNTIL_PREFIX) and valid_until is None:
            value = t.strip()[len(VALID_UNTIL_PREFIX):].strip()
            if value:
                valid_until = value

    out = {}
    if "both" in systems or {"old", "rebuild"} <= systems:
        out["applies_to"] = "both"
    elif systems:
        out["applies_to"] = systems.pop()
    if valid_until:
        out["valid_until"] = valid_until
    return out
