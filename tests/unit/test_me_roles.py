"""/api/me: permissions per role (tooltips of the role chips in the user menu)."""

from __future__ import annotations

from autoair.api.routers.meta import _role_permissions


def test_me_lists_the_permissions_of_each_role():
    table = _role_permissions({"Auditor": ["spec:read", "audit:read"]}, ["Auditor", "Viewer", "?"])
    assert table["Auditor"] == ["audit:read", "spec:read"]
    assert "spec:read" in table["Viewer"]
    assert "?" not in table
