"""Tests for agent/project rename tooling."""


class _FakeProjects:
    """Minimal stand-in for db.projects supporting find()/update_many() with the
    operators _rename_agent_in_projects_admins uses."""

    def __init__(self, docs):
        self.docs = [dict(d) for d in docs]

    def find(self, filt, projection=None):
        target = filt.get("admins")
        return [d for d in self.docs if target in d.get("admins", [])]

    def update_many(self, filt, upd):
        name_filt = filt.get("name", {})
        names = name_filt.get("$in", []) if isinstance(name_filt, dict) else []
        modified = 0
        for d in self.docs:
            if d.get("name") not in names:
                continue
            touched = False
            if "$pull" in upd:
                target = upd["$pull"]["admins"]
                if target in d.get("admins", []):
                    d["admins"] = [a for a in d["admins"] if a != target]
                    touched = True
            if "$addToSet" in upd:
                target = upd["$addToSet"]["admins"]
                d.setdefault("admins", [])
                if target not in d["admins"]:
                    d["admins"].append(target)
                    touched = True
            if touched:
                modified += 1

        class _Result:
            modified_count = modified

        return _Result()


class _FakeDB:
    def __init__(self, projects):
        self.projects = _FakeProjects(projects)


def test_rename_agent_in_projects_admins_renames_in_place():
    """Cross-project agent rename rewrites the agent name in projects.admins[]."""
    from shared_memory.tools.rename import _rename_agent_in_projects_admins

    db = _FakeDB([
        {"name": "claudecontrol", "admins": ["claude-control", "shared-memory"]},
        {"name": "junto", "admins": ["memory"]},
        {"name": "nimbus", "admins": []},
    ])

    n = _rename_agent_in_projects_admins(db, "claude-control", "control")

    assert n == 1
    assert set(db.projects.docs[0]["admins"]) == {"control", "shared-memory"}
    assert db.projects.docs[1]["admins"] == ["memory"]
    assert db.projects.docs[2]["admins"] == []


def test_rename_agent_in_projects_admins_idempotent():
    """Re-running with the same args after migration is a no-op."""
    from shared_memory.tools.rename import _rename_agent_in_projects_admins

    db = _FakeDB([{"name": "p", "admins": ["control", "memory"]}])

    n = _rename_agent_in_projects_admins(db, "claude-control", "control")

    assert n == 0
    assert set(db.projects.docs[0]["admins"]) == {"control", "memory"}


def test_rename_agent_in_projects_admins_dedups_when_target_present():
    """If target name already in admins (e.g., from a prior rename), don't duplicate."""
    from shared_memory.tools.rename import _rename_agent_in_projects_admins

    db = _FakeDB([{"name": "p", "admins": ["claude-control", "control", "memory"]}])

    n = _rename_agent_in_projects_admins(db, "claude-control", "control")

    assert n == 1
    assert set(db.projects.docs[0]["admins"]) == {"control", "memory"}
    # No duplicate "control" entry
    assert db.projects.docs[0]["admins"].count("control") == 1


def test_rename_agent_in_projects_admins_noop_on_self_rename():
    """from_agent == to_agent short-circuits without touching the db."""
    from shared_memory.tools.rename import _rename_agent_in_projects_admins

    db = _FakeDB([{"name": "p", "admins": ["control"]}])

    n = _rename_agent_in_projects_admins(db, "control", "control")

    assert n == 0
    assert db.projects.docs[0]["admins"] == ["control"]
