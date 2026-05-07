"""Tests for admin tool permission split (admin vs admin.write)."""

import shared_memory.auth as auth_mod


def test_admin_permission_allows_admin_and_owner():
    """The 'admin' permission gates entry to read-only admin actions."""
    assert "admin" in auth_mod.PERMISSIONS["admin"]
    assert "owner" in auth_mod.PERMISSIONS["admin"]
    # Lower tiers must not have access
    assert "agent" not in auth_mod.PERMISSIONS["admin"]
    assert "user" not in auth_mod.PERMISSIONS["admin"]
    assert "readonly" not in auth_mod.PERMISSIONS["admin"]


def test_admin_write_permission_owner_only():
    """The 'admin.write' permission is owner-only — destructive admin actions."""
    assert auth_mod.PERMISSIONS["admin.write"] == ["owner"]


def test_check_permission_admin_role_can_admin():
    """An admin-tier session passes the entry gate."""
    from shared_memory.auth import check_permission

    assert check_permission("admin", "admin") is True
    assert check_permission("owner", "admin") is True
    assert check_permission("agent", "admin") is False


def test_check_permission_admin_role_cannot_admin_write():
    """An admin-tier session is blocked from destructive admin actions."""
    from shared_memory.auth import check_permission

    assert check_permission("admin", "admin.write") is False
    assert check_permission("owner", "admin.write") is True
