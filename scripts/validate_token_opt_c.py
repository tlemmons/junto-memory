"""Validate token-opt (c) per-project config flip path.

Exercises query_config.set_config_value / reset_config / get_effective_config
end-to-end against Mongo. Mirrors the validation pattern used for Phase 1f.
"""

import sys

from shared_memory import query_config
from shared_memory.clients import get_mongo


def fail(msg): print(f"FAIL: {msg}", file=sys.stderr); sys.exit(1)
def ok(msg): print(f"OK: {msg}")


def main() -> None:
    db = get_mongo()
    if db is None:
        fail("MongoDB unavailable")

    print("=== Token-opt (c) per-project config validation ===\n")

    # Pre-clean
    db.query_config.delete_many({"scope": {"$in": ["__default__", "junto", "_test_proj_"]}})

    # 1. Defaults with no docs
    cfg = query_config.get_effective_config(db, None)
    if cfg["default_expand"] != True or cfg["default_expand_top"] != 0 or cfg["default_snippet_length"] != 200:
        fail(f"unexpected defaults: {cfg}")
    if cfg["_sources"]["default_expand"] != "code_default":
        fail(f"expected code_default source, got {cfg['_sources']}")
    ok(f"code defaults: expand=True snippet=200 expand_top=0")

    # 2. Set server default expand=False
    r = query_config.set_config_value(db, None, "default_expand", False, actor="test")
    if not r.get("ok"):
        fail(f"set server default failed: {r}")
    cfg = query_config.get_effective_config(db, None)
    if cfg["default_expand"] != False:
        fail(f"after server default flip: {cfg}")
    if cfg["_sources"]["default_expand"] != "server_default":
        fail(f"expected server_default source, got {cfg['_sources']}")
    ok(f"server default flipped to expand=False; source=server_default")

    # 3. Set project override expand=True for junto
    r = query_config.set_config_value(db, "junto", "default_expand", True, actor="test")
    if not r.get("ok"):
        fail(f"set project override failed: {r}")
    cfg_junto = query_config.get_effective_config(db, "junto")
    if cfg_junto["default_expand"] != True:
        fail(f"junto override didn't apply: {cfg_junto}")
    if cfg_junto["_sources"]["default_expand"] != "project:junto":
        fail(f"expected project:junto source, got {cfg_junto['_sources']}")
    ok(f"junto override: expand=True (overrides server default of False)")

    # 4. Different project gets server default
    cfg_other = query_config.get_effective_config(db, "_test_proj_")
    if cfg_other["default_expand"] != False:
        fail(f"_test_proj_ should inherit server default False, got {cfg_other}")
    ok(f"_test_proj_ inherits server_default expand=False")

    # 5. Validation: bad value type
    r = query_config.set_config_value(db, None, "default_expand", "not_a_bool", actor="test")
    if "error" not in r:
        fail(f"expected type validation error, got {r}")
    ok(f"validation rejects non-bool: {r['error']}")

    # 6. Validation: unknown key
    r = query_config.set_config_value(db, None, "bogus", 42, actor="test")
    if "error" not in r:
        fail(f"expected unknown-key error, got {r}")
    ok(f"validation rejects unknown key: {r['error']}")

    # 7. Validation: snippet_length out of range
    r = query_config.set_config_value(db, None, "default_snippet_length", 10, actor="test")
    if "error" not in r:
        fail(f"expected range error, got {r}")
    ok(f"validation rejects snippet_length<50: {r['error']}")

    r = query_config.set_config_value(db, None, "default_snippet_length", 5000, actor="test")
    if "error" not in r:
        fail(f"expected range error, got {r}")
    ok(f"validation rejects snippet_length>2000: {r['error']}")

    # 8. Validation: expand_top out of range
    r = query_config.set_config_value(db, None, "default_expand_top", -1, actor="test")
    if "error" not in r:
        fail(f"expected range error, got {r}")
    ok(f"validation rejects expand_top<0: {r['error']}")

    r = query_config.set_config_value(db, None, "default_expand_top", 100, actor="test")
    if "error" not in r:
        fail(f"expected range error, got {r}")
    ok(f"validation rejects expand_top>50: {r['error']}")

    # 9. Reset one key on project
    r = query_config.reset_config(db, "junto", "default_expand", actor="test")
    if not r.get("ok"):
        fail(f"reset key failed: {r}")
    cfg_junto = query_config.get_effective_config(db, "junto")
    if cfg_junto["default_expand"] != False:
        fail(f"after reset, junto should fall back to server default False, got {cfg_junto['default_expand']}")
    ok(f"junto.default_expand reset → falls back to server default (False)")

    # 10. Reset all on project
    query_config.set_config_value(db, "junto", "default_expand_top", 5, actor="test")
    r = query_config.reset_config(db, "junto", None, actor="test")
    if not r.get("ok"):
        fail(f"reset all failed: {r}")
    cfg_junto = query_config.get_effective_config(db, "junto")
    if cfg_junto["default_expand_top"] != 0:
        fail(f"after reset-all: expected 0 (code default), got {cfg_junto['default_expand_top']}")
    ok(f"junto reset-all clears all overrides")

    # 11. Cannot reset server default
    r = query_config.reset_config(db, None, "default_expand", actor="test")
    if "error" not in r:
        fail(f"expected error on reset of server default, got {r}")
    ok(f"server-default reset rejected: {r['error']}")

    # Cleanup
    db.query_config.delete_many({"scope": {"$in": ["__default__", "junto", "_test_proj_"]}})
    print("\n=== ALL TOKEN-OPT (C) PER-PROJECT CONFIG ASSERTIONS PASSED ===")


if __name__ == "__main__":
    main()
