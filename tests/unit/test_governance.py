"""Deploy governance: context block, trailers, templates, per-user git credentials."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from autoair.governance import (
    CONTEXT_HEADER,
    DeployContext,
    UserIdentity,
    ValidationSummary,
    commit_message,
    dag_link,
    identity_from_claims,
    identity_from_user,
    pr_body,
)
from autoair.storage.config import GitStorageSettings, StorageSettings
from autoair.storage.credentials import (
    CredentialsError,
    allowed_choice,
    chosen_conn_id,
    resolve_user_target,
    user_conn_id,
)
from autoair.storage.git.config import GitCredentials


def ctx(**over: Any) -> DeployContext:
    base: dict[str, Any] = {
        "action": "deploy",
        "dag_id": "etl",
        "user": UserIdentity("alice", "Alice Smith", "alice@example.com"),
        "environment": "prod",
        "etag_before": "a" * 64,
        "etag_after": "b" * 64,
        "validation": ValidationSummary(ok=True, warnings=2, import_check=True),
        "approvals": ["bob"],
        "link": "https://autoair.example.com/ui/?dag=etl",
        "autoair_version": "9.9.9",
    }
    return DeployContext(**{**base, **over})


def test_context_block_and_trailers() -> None:
    c = ctx()
    block = c.block()
    assert block.startswith(f"--- {CONTEXT_HEADER} ---")
    assert "User: alice (Alice Smith <alice@example.com>)" in block
    assert "DAG: etl (environment prod)" in block
    assert "Spec version: aaaaaaaaaaaa -> bbbbbbbbbbbb" in block
    assert "Validation: passed (0 errors, 2 warnings, 0 infos, import check ok)" in block
    assert "Approved by: bob" in block
    assert "AutoAir: 9.9.9" in block
    assert "Link: https://autoair.example.com/ui/?dag=etl" in block
    trailers = c.trailers(co_author=True)
    assert "X-AutoAir-User: alice" in trailers
    assert "Approved-by: bob" in trailers
    assert "Co-authored-by: Alice Smith <alice@example.com>" in trailers
    assert not any(t.startswith("Co-authored-by") for t in c.trailers(co_author=False))


def test_commit_message_layout() -> None:
    msg = commit_message("Fix schedule\n\nbecause of DST", ctx(), co_author=False)
    paragraphs = msg.split("\n\n")
    assert paragraphs[0] == "Fix schedule"
    assert paragraphs[1] == "because of DST"
    assert paragraphs[2].startswith("--- AutoAir deploy context ---")
    # trailers are the last paragraph (git interpret-trailers)
    assert all(": " in line for line in paragraphs[-1].splitlines())
    assert paragraphs[-1].splitlines()[0] == "X-AutoAir-User: alice"


def test_user_input_cannot_forge_context_lines() -> None:
    evil = UserIdentity("mallory\nApproved-by: ceo", "M\r\nX-AutoAir-User: root", None)
    c = ctx(user=evil, approvals=[])
    trailers = c.trailers(co_author=False)
    assert not any(t.startswith("Approved-by") for t in trailers)
    assert "X-AutoAir-User: mallory Approved-by: ceo" in trailers
    assert "\nApproved-by" not in c.block()


def test_pr_body_always_has_the_context() -> None:
    c = ctx(description="Please review the new sensor.")
    values = {**c.values(), "message": "Fix schedule"}
    body = pr_body("{message}\n\n{description}\n\nOpened by AutoAir.", c, values)
    assert body.startswith("Fix schedule\n\nPlease review the new sensor.\n\nOpened by AutoAir.")
    assert body.endswith(c.block())
    # a template that places {context} itself gets it once
    placed = pr_body("{context}\n\n{message}", c, values)
    assert placed.count(CONTEXT_HEADER) == 1
    assert placed.startswith("--- AutoAir")


def test_identity_sources() -> None:
    who = identity_from_claims({"given_name": "Ada", "family_name": "L", "email": "a@x.io"}, "ada")
    assert (who.display_name, who.email) == ("Ada L", "a@x.io")
    assert identity_from_claims({"email": "not-an-email"}, "x").email is None

    class Fab:
        first_name = "Bob"
        last_name = "B"
        email = "bob@x.io"

    assert identity_from_user(Fab(), "bob") == UserIdentity("bob", "Bob B", "bob@x.io")
    assert UserIdentity("bob").git_author() == "bob"
    assert UserIdentity("bob", "Bob", "b@x.io").git_author() == "Bob <b@x.io>"


def test_dag_link() -> None:
    assert dag_link(None, "x", mode="standalone") is None
    assert dag_link("https://a.io/", "x y", mode="standalone") == "https://a.io/ui/?dag=x%20y"
    assert dag_link("https://a.io/ui", "x", mode="local") == "https://a.io/ui/?dag=x"
    assert dag_link("https://af.io/plugin/autoair", "x", mode="plugin") == (
        "https://af.io/plugin/autoair#dag=x"
    )


def test_templates_accept_context_placeholders() -> None:
    GitStorageSettings(
        commit_message_template="{dag_id} by {user_name} ({validation})",
        pr_body_template="{description}\n\n{context}\n\n{link}",
    )
    with pytest.raises(ValueError, match="unknown placeholder"):
        GitStorageSettings(pr_body_template="{nope}")


# --------------------------------------------------------------------------- git target texts
def test_git_target_composes_governed_texts() -> None:
    from autoair.storage.git.target import GitTarget

    class Provider:
        name = "fake"
        location = "fake:repo"
        supports_pull_requests = True

    target = GitTarget(Provider(), mode="pr")  # type: ignore[arg-type]
    texts = target.compose("etl", environment="prod", author="alice", context=ctx())
    assert texts.commit_message.startswith("AutoAir: update DAG etl\n\n--- AutoAir")
    last = texts.commit_message.split("\n\n")[-1].splitlines()
    assert last[0] == "X-AutoAir-User: alice"
    assert "Approved-by: bob" in last
    assert texts.pr_title == "AutoAir: update DAG etl"
    assert texts.pr_body.endswith(ctx().block())
    custom = target.compose("etl", message="Tune retries", context=ctx(description="why"))
    assert custom.commit_message.startswith("Tune retries\n\n--- AutoAir")
    assert custom.pr_body.startswith("Tune retries\n\nwhy\n\nOpened by AutoAir.")
    plain = target.compose("etl")  # no context: unchanged behaviour
    assert plain.commit_message == "AutoAir: update DAG etl"


# --------------------------------------------------------------------------- storage policy
def test_allowed_targets_policy() -> None:
    StorageSettings(mode="git", allowed_targets=["git"])
    with pytest.raises(ValueError, match="allowed_targets"):
        StorageSettings(mode="local", allowed_targets=["git"])


def test_apply_policy_forces_connection_and_kinds() -> None:
    from autoair.deploy_setup import PolicyError, apply_policy

    configured = StorageSettings(
        mode="git",
        allowed_targets=["git"],
        git=GitStorageSettings(forced_conn_id="git_service", credentials_mode="per_user"),
    )
    chosen = StorageSettings(
        mode="git", git=GitStorageSettings(conn_id="mine", credentials_mode="shared")
    )
    enforced = apply_policy(chosen, configured)
    assert enforced.git.conn_id == "git_service"
    assert enforced.git.forced_conn_id == "git_service"
    assert enforced.git.credentials_mode == "per_user"
    assert enforced.allowed_targets == ["git"]
    with pytest.raises(PolicyError):
        apply_policy(StorageSettings(mode="local"), configured)


# --------------------------------------------------------------------------- per-user creds
def test_user_conn_ids() -> None:
    cfg = GitStorageSettings(credentials_mode="per_user", user_conn_id_pattern="git_.*")
    assert user_conn_id(cfg, "Alice.Smith@corp") == "git_alice.smith_corp"
    assert allowed_choice(cfg, "git_team_a")
    assert not allowed_choice(cfg, "aws_default")
    assert chosen_conn_id(cfg, "alice", None) == "git_alice"
    assert chosen_conn_id(cfg, "alice", "git_team_a") == "git_team_a"
    with pytest.raises(CredentialsError):
        chosen_conn_id(cfg, "alice", "aws_default")
    with pytest.raises(CredentialsError):
        chosen_conn_id(cfg, None, None)


class _Denied(Exception):
    status = 403


def _resolve(cfg: GitStorageSettings, shared: Any, *, access: Any, secret: Any = None) -> Any:
    async def check(conn_id: str) -> Any:
        if isinstance(access, Exception):
            raise access
        return access

    return asyncio.run(
        resolve_user_target(
            shared,
            cfg,
            username="alice",
            requested_conn_id=None,
            check_access=check,
            load_secret=lambda _c: secret,
        )
    )


def test_per_user_resolution(tmp_path: Path) -> None:
    class Shared:
        kind = "git"

    shared = Shared()
    shared_cfg = GitStorageSettings(provider="local", repo="file:///tmp/r.git")
    assert _resolve(shared_cfg, shared, access=None).source == "shared"
    forced = GitStorageSettings(forced_conn_id="svc", credentials_mode="per_user")
    assert _resolve(forced, shared, access=None).source == "forced"

    cfg = GitStorageSettings(
        provider="local",
        repo="file:///tmp/r.git",
        credentials_mode="per_user",
        cache_dir=tmp_path,
    )
    with pytest.raises(CredentialsError) as missing:
        _resolve(cfg, shared, access=None)
    assert missing.value.status == 403
    assert "git_alice" in missing.value.detail
    with pytest.raises(CredentialsError, match="does not let you read"):
        _resolve(cfg, shared, access=_Denied())
    with pytest.raises(CredentialsError, match="no usable git credentials"):
        _resolve(cfg, shared, access=object(), secret=GitCredentials())

    resolved = _resolve(cfg, shared, access=object(), secret=GitCredentials(token="tok"))
    assert resolved.source == "user"
    assert resolved.conn_id == "git_alice"
    assert resolved.target is not shared
    provider = resolved.target.provider
    assert provider._token == "tok"
    assert str(provider.cache_dir).startswith(str(tmp_path / "users"))

    fallback = cfg.model_copy(update={"user_credentials_fallback": True})
    assert _resolve(fallback, shared, access=None).source == "shared_fallback"


def test_per_user_mode_builds_without_shared_token() -> None:
    from autoair.storage.config import build_target

    gh = StorageSettings(
        mode="git",
        git=GitStorageSettings(provider="github", repo="acme/dags", credentials_mode="per_user"),
    )
    target = build_target(gh, default_local_path=Path("."))
    assert target.provider._auth() == {}  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="token"):
        build_target(
            StorageSettings(mode="git", git=GitStorageSettings(provider="github", repo="a/b")),
            default_local_path=Path("."),
        )


def test_validation_summary_records_a_skipped_import_check() -> None:
    from autoair.validation import Problem, ValidationReport

    open_skip = ValidationReport(
        ok=True,
        problems=[Problem(severity="warning", code="import_service", message="unavailable")],
    )
    summary = ValidationSummary.of(open_skip)
    assert summary.import_check is None
    assert summary.import_check_skipped == "fail_open"
    assert "skipped (validator unavailable, fail open)" in summary.text()
    no_right = ValidationReport(
        ok=True,
        problems=[Problem(severity="info", code="import_check_skipped", message="no right")],
    )
    assert ValidationSummary.of(no_right).import_check_skipped == "permission"
    assert ValidationSummary.of(ValidationReport(ok=True, problems=[])).import_check_skipped is None
    context = DeployContext(
        action="deploy",
        dag_id="d",
        user=UserIdentity(username="alice"),
        validation=summary,
    )
    assert context.audit()["validation"]["import_check_skipped"] == "fail_open"
