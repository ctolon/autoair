"""IAM: roles, permission resolution and the Airflow mapping strategies."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from autoair.iam import model as P
from autoair.iam.config import IamSettings
from autoair.iam.login import LoginLimiter
from autoair.iam.resolver import (
    Identity,
    airflow_user_roles,
    claim_roles,
    claim_user,
    jwt_claims,
    map_airflow_roles,
    resolve,
)


def jwt(**claims: object) -> str:
    def enc(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def checks(read: bool = False, edit: bool = False, config: bool = False, verified: bool = True):
    return {
        "dag_read": (read, verified),
        "dag_edit": (edit, verified),
        "configuration_edit": (config, verified),
    }


def run(identity: Identity, **settings: object) -> P.Principal:
    return resolve(identity, IamSettings(**settings), auth="test", mode="plugin")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- model
def test_builtin_roles_are_consistent():
    for role, perms in P.BUILTIN_ROLES.items():
        assert set(perms) <= P.ALL_PERMISSIONS, role
    # nobody gets direct push from a built-in role definition: it is granted by
    # direct_push_roles (deny by default)
    assert all(P.DEPLOY_DIRECT_PUSH not in perms for perms in P.BUILTIN_ROLES.values())
    assert set(P.BUILTIN_ROLES[P.ADMIN]) == P.ALL_PERMISSIONS - {P.DEPLOY_DIRECT_PUSH}
    viewer = set(P.BUILTIN_ROLES[P.VIEWER])
    for role in (P.AUTHOR, P.REVIEWER, P.DEPLOYER, P.OPERATOR, P.TEMPLATE_MANAGER):
        assert viewer <= set(P.BUILTIN_ROLES[role]), role
    assert P.DEPLOY not in P.BUILTIN_ROLES[P.AUTHOR]  # authors propose, deployers deploy
    assert P.REVIEW in P.BUILTIN_ROLES[P.REVIEWER]
    assert P.SPEC_WRITE not in P.BUILTIN_ROLES[P.REVIEWER]
    # every permission maps to a distinct custom-view (method, resource) pair
    pairs = [(p.method, p.resource) for p in P.PERMISSIONS]
    assert len(pairs) == len(set(pairs))


def test_expand_wildcards_and_custom_roles():
    assert P.expand(["spec:*"]) == {P.SPEC_READ, P.SPEC_WRITE, P.SPEC_DELETE}
    # deploy:direct_push never comes from a wildcard
    assert P.expand(["*"]) == P.ALL_PERMISSIONS - {
        P.DEPLOY_DIRECT_PUSH,
        P.DEPLOY_OVERWRITE_EJECTED,
    }
    assert P.expand(["deploy*"]) == {P.DEPLOY}
    table = P.role_table({"Auditor": ["spec:read", "audit:*"], "Viewer": ["builder:use"]})
    assert table["Auditor"] == {P.SPEC_READ, P.AUDIT_READ}
    assert table["Viewer"] == {P.BUILDER_USE}  # built-ins can be overridden
    assert P.canonical_role("deployer", table) == "Deployer"
    assert P.canonical_role("nope", table) is None


# ----------------------------------------------------------------------------- airflow_permissions
def test_airflow_permissions_default_matches_pre_iam_behaviour():
    signed_in = run(Identity(user="u", checks=checks()))
    assert signed_in.roles == ["Authenticated"]
    assert signed_in.permissions == set(P.BUILTIN_ROLES[P.AUTHENTICATED])
    assert not signed_in.has(P.SPEC_READ)

    reader = run(Identity(user="u", checks=checks(read=True)))
    assert reader.has(P.SPEC_READ)
    assert reader.has(P.AUDIT_READ)
    assert not reader.has(P.SPEC_WRITE)

    editor = run(Identity(user="u", checks=checks(read=True, edit=True)))
    for perm in (P.SPEC_WRITE, P.DEPLOY, P.CODE_EXECUTE, P.RUN_TEST, P.TEMPLATE_WRITE, P.PROMOTE):
        assert editor.has(perm), perm
    assert not editor.has(P.ADMIN_DEPLOY_TARGET)
    assert not editor.has(P.DEPLOY_DIRECT_PUSH)

    admin = run(Identity(user="u", checks=checks(read=True, edit=True, config=True)))
    assert admin.has(P.ADMIN_DEPLOY_TARGET)
    assert admin.has(P.DEPLOY_DIRECT_PUSH)  # Admin is in iam.direct_push_roles


def test_unverified_admin_never_gets_direct_push():
    # standalone: no configuration check is possible, every check "passes" unverified
    p = run(Identity(user="u", checks=checks(True, True, True, verified=False)))
    assert p.has(P.ADMIN_DEPLOY_TARGET)  # pre-IAM standalone behaviour kept
    assert not p.has(P.DEPLOY_DIRECT_PUSH)
    assert not p.can_direct_push(["Admin", "Deployer"])


def test_anonymous_gets_anonymous_roles_without_direct_push():
    p = run(Identity(user=None, authenticated=False))
    assert p.roles == ["Admin"]
    assert p.has(P.ADMIN_CONFIG)
    assert not p.has(P.DEPLOY_DIRECT_PUSH)
    assert run(
        Identity(user=None, authenticated=False), anonymous_roles=["Viewer"]
    ).permissions == set(P.BUILTIN_ROLES[P.VIEWER])


def test_configurable_airflow_permission_roles():
    p = run(
        Identity(user="u", checks=checks(read=True, edit=True)),
        airflow_permission_roles={"authenticated": [], "dag_edit": ["Author"]},
    )
    assert p.has(P.SPEC_WRITE)
    assert not p.has(P.DEPLOY)


# ----------------------------------------------------------------------------- role_mapping
def test_role_mapping_from_airflow_role_names():
    s = IamSettings(strategies=["role_mapping"])
    table = P.role_table()
    assert map_airflow_roles(["ADMIN"], s, table) == ["Admin"]  # SimpleAuthManager casing
    assert "Deployer" in map_airflow_roles(["op"], s, table)
    assert map_airflow_roles(["AutoAir Reviewer"], s, table) == ["Reviewer"]  # role prefix
    assert map_airflow_roles(["autoair templatemanager"], s, table) == ["TemplateManager"]
    assert map_airflow_roles(["Public", "AutoAir Nope"], s, table) == []

    p = run(Identity(user="u", airflow_roles=["AutoAir Author"]), strategies=["role_mapping"])
    assert p.roles == ["Author"]
    assert p.has(P.SPEC_WRITE)
    assert not p.has(P.DEPLOY)

    custom = run(
        Identity(user="u", airflow_roles=["data-eng"]),
        strategies=["role_mapping"],
        role_mapping={"data-eng": ["Author", "Deployer"]},
        role_prefix="",
    )
    assert custom.roles == ["Author", "Deployer"]
    assert custom.has(P.DEPLOY)


def test_role_mapping_ignores_airflow_permission_checks():
    p = run(
        Identity(user="u", airflow_roles=["Viewer"], checks=checks(True, True, True)),
        strategies=["role_mapping"],
    )
    assert p.roles == ["Viewer"]
    assert not p.has(P.ADMIN_DEPLOY_TARGET)


def test_direct_push_roles_are_configurable():
    ident = Identity(user="u", airflow_roles=["AutoAir Deployer"])
    assert not run(ident, strategies=["role_mapping"]).has(P.DEPLOY_DIRECT_PUSH)
    p = run(ident, strategies=["role_mapping"], direct_push_roles=["Deployer"])
    assert p.has(P.DEPLOY_DIRECT_PUSH)
    admin = Identity(user="a", airflow_roles=["Admin"])
    assert not run(admin, strategies=["role_mapping"], direct_push_roles=[]).has(
        P.DEPLOY_DIRECT_PUSH
    )
    # a target's own list (storage.git.direct_push_roles) is checked per deploy
    p = run(admin, strategies=["role_mapping"])
    assert p.can_direct_push(["Admin"])
    assert not p.can_direct_push(["Deployer"])


def test_custom_roles_and_default_roles():
    p = run(
        Identity(user="u", checks=checks()),
        roles={"Pusher": ["deploy", "deploy:direct_push", "spec:write"]},
        default_roles=["Pusher"],
    )
    assert p.has(P.DEPLOY_DIRECT_PUSH)
    assert p.explicit_direct_push
    assert p.can_direct_push([])


# ----------------------------------------------------------------------------- custom_view
def test_custom_view_strategy_asks_the_auth_manager():
    asked: list[tuple[str, str]] = []
    granted = {
        ("GET", "AutoAir Specs"),
        ("POST", "AutoAir Deploy"),
        ("POST", "AutoAir Direct Push"),
    }

    def check(method: str, resource: str) -> bool:
        asked.append((method, resource))
        if resource == "AutoAir AI":
            raise RuntimeError("auth manager down")
        return (method, resource) in granted

    p = run(Identity(user="u", custom_view=check), strategies=["custom_view"])
    assert len(asked) == len(P.PERMISSIONS)
    assert p.permissions == {P.SPEC_READ, P.DEPLOY, P.DEPLOY_DIRECT_PUSH}
    assert p.roles == []
    assert p.explicit_direct_push

    prefixed = run(
        Identity(user="u", custom_view=lambda m, r: r == "Builder"),
        strategies=["custom_view"],
        custom_view_prefix="",
    )
    assert prefixed.permissions == {P.BUILDER_USE}


def test_strategies_are_a_union():
    p = run(
        Identity(
            user="u",
            airflow_roles=["AutoAir Reviewer"],
            checks=checks(read=True),
            custom_view=lambda m, r: r == "AutoAir Icons",
        ),
        strategies=["airflow_permissions", "role_mapping", "custom_view"],
    )
    assert {"Authenticated", "Viewer", "Reviewer"} <= set(p.roles)
    assert p.has(P.REVIEW)
    assert p.has(P.ICON_WRITE)
    assert p.has(P.RUN_TRIGGER)


# ----------------------------------------------------------------------------- helpers
def test_user_roles_from_auth_manager_users():
    fab_user = SimpleNamespace(
        roles=[SimpleNamespace(name="Op"), SimpleNamespace(name="AutoAir X")]
    )
    assert airflow_user_roles(fab_user) == ["Op", "AutoAir X"]
    assert airflow_user_roles(SimpleNamespace(role="ADMIN")) == ["ADMIN"]
    assert airflow_user_roles(SimpleNamespace(role=None)) == []

    class Broken:
        @property
        def roles(self):
            raise RuntimeError("detached")

    assert airflow_user_roles(Broken()) == []


def test_jwt_claims():
    token = jwt(sub="alice", role="OP", roles=["a", 1, {"x": 1}])
    claims = jwt_claims(f"Bearer {token}")
    assert claim_user(claims) == "alice"
    assert claim_roles(claims, ["role", "roles"]) == ["OP", "a", "1"]
    assert jwt_claims("garbage") == {}
    assert jwt_claims("a.!!!.c") == {}


def test_settings_from_environment(monkeypatch):
    from autoair.settings import Settings

    monkeypatch.setenv("AUTOAIR_IAM__STRATEGIES", '["role_mapping", "custom_view"]')
    monkeypatch.setenv("AUTOAIR_IAM__DIRECT_PUSH_ROLES", '["Admin", "Deployer"]')
    monkeypatch.setenv("AUTOAIR_IAM__REQUIRE_APPROVAL", "true")
    s = Settings()
    assert s.iam.strategies == ["role_mapping", "custom_view"]
    assert s.iam.direct_push_roles == ["Admin", "Deployer"]
    assert s.iam.require_approval is True
    assert Settings.model_fields["iam"].description
    for name, f in IamSettings.model_fields.items():
        assert f.description, name


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="strategies"):
        IamSettings(strategies=["everyone_is_admin"])  # type: ignore[list-item]


# ----------------------------------------------------------------------------- login limiter
def test_login_limiter_sliding_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("autoair.iam.login.time.monotonic", lambda: now[0])
    limiter = LoginLimiter(max_failures=3, window=60)
    keys = ["ip:1", "user:a"]
    for _ in range(2):
        limiter.failed(keys)
    assert limiter.retry_after(keys) is None
    limiter.failed(keys)
    wait = limiter.retry_after(keys)
    assert wait is not None
    assert 0 < wait <= 60
    assert limiter.retry_after(["ip:2", "user:a"])  # the username alone is locked too
    assert limiter.retry_after(["ip:2", "user:b"]) is None
    limiter.succeeded("user:a")
    assert limiter.retry_after(["ip:2", "user:a"]) is None
    now[0] += 61  # window passed
    assert limiter.retry_after(keys) is None


# ----------------------------------------------------------------------------- FAB sync
class FakeSecurityManager:
    """The subset of FAB's security manager ``sync_fab`` uses."""

    def __init__(self) -> None:
        self.perms: dict[tuple[str, str], SimpleNamespace] = {}
        self.roles: dict[str, SimpleNamespace] = {}

    def get_permission(self, action: str, resource: str):
        return self.perms.get((action, resource))

    def create_permission(self, action: str, resource: str):
        perm = SimpleNamespace(
            action=SimpleNamespace(name=action), resource=SimpleNamespace(name=resource)
        )
        self.perms[(action, resource)] = perm
        return perm

    def find_role(self, name: str):
        return self.roles.get(name)

    def add_role(self, name: str):
        self.roles[name] = SimpleNamespace(name=name, permissions=[])
        return self.roles[name]

    def add_permission_to_role(self, role, perm) -> None:
        role.permissions.append(perm)


def test_sync_fab_creates_permissions_and_roles_idempotently():
    from autoair.iam.fab import fab_permission, sync_fab

    sm = FakeSecurityManager()
    settings = IamSettings()
    assert fab_permission(P.DEPLOY, settings) == ("can_create", "AutoAir Deploy")
    assert fab_permission(P.SPEC_READ, settings) == ("can_read", "AutoAir Specs")
    log = sync_fab(sm, settings)
    assert len(sm.perms) == len(P.PERMISSIONS)
    assert sm.roles == {}
    assert all(line.startswith("created permission") for line in log)

    sync_fab(sm, settings, create_roles=True)
    assert "AutoAir Authenticated" not in sm.roles
    deployer = {(p.action.name, p.resource.name) for p in sm.roles["AutoAir Deployer"].permissions}
    assert ("can_create", "AutoAir Deploy") in deployer
    assert ("can_create", "AutoAir Direct Push") not in deployer
    assert sync_fab(sm, settings, create_roles=True) == []  # idempotent

    # the custom_view strategy reads exactly these permissions back
    granted = {(p.action.name, p.resource.name) for p in sm.roles["AutoAir Reviewer"].permissions}
    actions = {"GET": "can_read", "POST": "can_create", "PUT": "can_edit", "DELETE": "can_delete"}
    p = run(
        Identity(user="r", custom_view=lambda m, r: (actions[m], r) in granted),
        strategies=["custom_view"],
    )
    assert p.permissions == set(P.BUILTIN_ROLES[P.REVIEWER])
