"""Values that used to be hard-coded: settings, wiring, the audit and the generated docs."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

from autoair import sections
from autoair.catalog import discovery
from autoair.sections import (
    DiscoverySettings,
    EditorSettings,
    IconSettings,
    InterchangeSettings,
    SandboxSettings,
)
from autoair.settings import Settings
from autoair.spec import DagSpec
from autoair.storage.config import GitStorageSettings, StorageSettings, build_target
from autoair.storage.git import GitHubProvider, GitTarget
from autoair.storage.git.provider import PullRequestOptions
from autoair.validation import Policy, policy_from_settings, validate_spec
from tests.unit.test_storage_providers import SRC, FakeRepo, github_app

ROOT = Path(__file__).resolve().parents[2]
CLI = "from autoair.cli import app; app()"


@pytest.fixture(autouse=True)
def _reset_active_sections():
    yield
    sections.reset()
    discovery.configure(DiscoverySettings())


def make_settings(**kw: object) -> Settings:
    return Settings(dags_folder=Path("/x"), **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- catalog


def test_cache_format_is_gone_and_layout_version_is_derived() -> None:
    from autoair.catalog import service

    assert not hasattr(service, "CACHE_FORMAT")
    version = service.layout_version()
    assert len(version) == 16
    assert (
        service.CatalogData(
            key="k", key_parts={}, strategy="static", built_at=0, build_seconds=0
        ).format
        == version
    )


def test_stale_layout_is_ignored(tmp_path: Path) -> None:
    from autoair.catalog.service import CatalogData, _read_data, _write_data

    data = CatalogData(key="k", key_parts={}, strategy="static", built_at=0, build_seconds=0)
    path = tmp_path / "catalog.json"
    _write_data(path, data)
    assert _read_data(path) is not None
    raw = json.loads(path.read_text())
    raw["format"] = "0" * 16  # written by another layout of CatalogData
    path.write_text(json.dumps(raw))
    assert _read_data(path) is None
    raw["format"] = 8  # the old hand-bumped CACHE_FORMAT
    path.write_text(json.dumps(raw))
    assert _read_data(path) is None


def test_discovery_filters_are_part_of_the_cache_key() -> None:
    from autoair.catalog.service import OperatorCatalog

    catalog = OperatorCatalog(sources=[])
    before = catalog.cache_key_parts()
    discovery.configure(DiscoverySettings(skip_dirs_extra=["build"]))
    # The filters belong to the catalog (taken when it was created): changing the
    # process-wide ones later does not change an existing catalog's key.
    assert catalog.cache_key_parts() == before
    after = OperatorCatalog(sources=[]).cache_key_parts()
    assert before["discovery"] != after["discovery"]
    assert before["format"] == after["format"]


OPS = """
from airflow.sdk import BaseOperator


class HelloOperator(BaseOperator):
    def execute(self, context):
        return "hi"
"""

TASK = """
from acme.base import Base


class GreetingTask(Base):
    def execute(self, context):
        return "hi"
"""


def _modules(source) -> list[str]:
    return sorted(ref.module for ref in source.modules())


def test_discovery_skip_dirs_modules_and_hints(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    for rel, text in {
        "ops/hello.py": OPS,
        "ops/tasks.py": TASK,
        "build/hello_copy.py": OPS,
        "ops/legacy/old.py": OPS,
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    source = discovery.PathsSource([root])
    assert _modules(source) == ["build.hello_copy", "ops.hello", "ops.legacy.old"]
    discovery.configure(
        DiscoverySettings(
            skip_dirs_extra=["build"],
            skip_module_patterns_extra=[r"\.legacy(\.|$)"],
            class_hint_patterns_extra=["Task"],
        )
    )
    assert _modules(source) == ["ops.hello", "ops.tasks"]
    discovery.configure(DiscoverySettings(max_file_bytes=10))
    assert _modules(source) == []


def test_discovery_skip_class_pattern_override() -> None:
    f = discovery.DiscoveryFilters.from_settings(DiscoverySettings())
    assert f.skip_class_name("DecoratedSensorOperator")
    f = discovery.DiscoveryFilters.from_settings(
        DiscoverySettings(skip_class_pattern="", skip_class_patterns_extra=["^Internal"])
    )
    assert not f.skip_class_name("DecoratedSensorOperator")
    assert f.skip_class_name("InternalOperator")


def test_exclude_distributions_extend() -> None:
    f = discovery.DiscoveryFilters.from_settings(
        DiscoverySettings(exclude_distributions_extra=["acme-airflow-*"])
    )
    assert f.excluded_distribution("apache_airflow_core")
    assert f.excluded_distribution("Acme.Airflow_Ops")
    assert not f.excluded_distribution("airflow-dbt-python")


def test_files_source_infers_the_import_root(tmp_path: Path) -> None:
    pkg = tmp_path / "vendor" / "acme_ops"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "sub" / "__init__.py").write_text("")
    (pkg / "sub" / "jira.py").write_text(OPS)
    (pkg / "other.py").write_text(OPS)
    single = discovery.FilesSource([pkg / "sub" / "jira.py"])
    assert _modules(single) == ["acme_ops.sub.jira"]
    assert str(tmp_path / "vendor") in discovery.registered_search_paths()
    folder = discovery.FilesSource([pkg])
    assert _modules(folder) == ["acme_ops.other", "acme_ops.sub.jira"]


def test_distribution_source_and_top_level_packages() -> None:
    names = discovery.distribution_packages("pydantic")
    assert "pydantic" in names
    assert discovery.distribution_packages("no-such-distribution-xyz") == []
    source = discovery.DistributionsSource(["no-such-distribution-xyz"])
    assert list(source.modules()) == []


def test_configured_sources_include_distributions_and_paths(tmp_path: Path) -> None:
    from autoair.catalog.service import configured_sources

    (tmp_path / "x.py").write_text(OPS)
    s = make_settings(
        extra_operator_distributions=["acme-airflow-ops"],
        extra_operator_paths=[tmp_path / "x.py"],
        discover_airflow_folders=False,
        discovery={"skip_dirs_extra": ["build"]},
    )
    kinds = {type(src).__name__ for src in configured_sources(s)}
    assert {"DistributionsSource", "FilesSource", "ProvidersSource"} <= kinds
    assert "build" in discovery.filters().skip_dirs


# --------------------------------------------------------------------------- policy


SPEC = {
    "dag_id": "Bad-Name",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "Upper",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            "params": {"vault_secret_ref": "abcdefgh"},
        }
    ],
}


def _codes(policy: Policy, spec: dict | None = None) -> list[str]:
    report = validate_spec(DagSpec.model_validate(spec or SPEC), import_check=False, policy=policy)
    return [f"{p.code}:{p.message}" for p in report.problems]


def test_dag_id_and_task_id_patterns() -> None:
    policy = policy_from_settings(
        make_settings(policy={"dag_id_pattern": "[a-z_]+", "task_id_patterns_extra": ["[a-z]+"]})
    )
    found = _codes(policy)
    assert any("dag_id must match '[a-z_]+'" in c for c in found)
    assert any("task_id must match '[a-z]+'" in c for c in found)
    assert not any("must match" in c for c in _codes(policy_from_settings(make_settings())))


def test_privileged_operators_extend_and_feature_flag() -> None:
    s = make_settings(
        allow_privileged_operators=False,
        policy={"privileged_operators_extra": ["*.EmptyOperator"]},
    )
    policy = policy_from_settings(s)
    assert policy.is_privileged("airflow.providers.standard.operators.empty.EmptyOperator")
    assert policy.is_privileged("airflow.providers.standard.operators.bash.BashOperator")
    assert any("disabled by policy" in c for c in _codes(policy))
    assert (
        policy_from_settings(make_settings(features={"python_code": False})).allow_python_code
        is False
    )


def test_secret_patterns_extend_and_override() -> None:
    plain = _codes(policy_from_settings(make_settings()))
    assert not any(c.startswith("secret:") for c in plain)  # "_ref" names are excluded
    extended = policy_from_settings(
        make_settings(
            policy={"secret_param_exclude_pattern": "", "secret_param_patterns_extra": ["vault"]}
        )
    )
    assert any(c.startswith("secret:") for c in _codes(extended))
    off = policy_from_settings(make_settings(policy={"secret_param_pattern": ""}))
    spec = json.loads(json.dumps(SPEC))
    spec["nodes"][0]["params"] = {"password": "hunter2hunter2"}
    assert any(c.startswith("secret:") for c in _codes(policy_from_settings(make_settings()), spec))
    assert not any(c.startswith("secret:") for c in _codes(off, spec))


def test_jinja_names_extra() -> None:
    from autoair.validation.jinja import check_template

    assert check_template("{{ company_macro() }}")
    assert check_template("{{ company_macro() }}", extra_names=["company_macro"]) == []


def test_sandbox_scrubs_extra_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair._subprocess import is_secret_name, user_code_env

    monkeypatch.setenv("VAULT_TOKEN", "v")
    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__TOKEN", "t")
    monkeypatch.setenv("AUTOAIR_CUSTOM_DSN", "d")
    assert is_secret_name("AUTOAIR_STORAGE__GIT__TOKEN")
    assert not is_secret_name("VAULT_TOKEN")  # not AUTOAIR_*, not listed
    sections.activate(
        make_settings(
            sandbox=SandboxSettings(
                scrub_env_extra=["VAULT_TOKEN"], secret_env_patterns_extra=["DSN"]
            )
        )
    )
    env = user_code_env()
    assert "VAULT_TOKEN" not in env
    assert "AUTOAIR_CUSTOM_DSN" not in env
    assert "AUTOAIR_STORAGE__GIT__TOKEN" not in env


# --------------------------------------------------------------------------- limits


def test_active_sections_follow_activate() -> None:
    assert sections.active(IconSettings).max_side == 128
    sections.activate(make_settings(icons={"max_side": 32}, editor={"max_completions": 3}))
    assert sections.active(IconSettings).max_side == 32
    assert sections.active(EditorSettings).max_completions == 3
    sections.reset()
    assert sections.active(IconSettings).max_side == 128


def test_icon_limits(tmp_path: Path) -> None:
    import io

    from PIL import Image

    from autoair.icons import IconError, normalize_png

    out = io.BytesIO()
    Image.new("RGBA", (300, 300), (1, 2, 3, 255)).save(out, format="PNG")
    data = out.getvalue()
    sections.activate(make_settings(icons={"max_side": 32}))
    assert Image.open(io.BytesIO(normalize_png(data))).size == (32, 32)
    sections.activate(make_settings(icons={"max_upload_bytes": 1024}))
    with pytest.raises(IconError, match="larger than 1 KB"):
        normalize_png(data)


def test_yaml_node_limit() -> None:
    from autoair.codegen.yaml_dag import load_yaml

    doc = "a: [" + ", ".join(str(i) for i in range(500)) + "]\n"
    assert load_yaml(doc)
    sections.activate(make_settings(interchange=InterchangeSettings(yaml_max_nodes=100)))
    with pytest.raises(ValueError, match="more than 100 values"):
        load_yaml(doc)


def test_codegen_line_length(simple_spec_data) -> None:
    from autoair.codegen.format import _passes

    assert "100" in _passes()[1]
    sections.activate(make_settings(codegen={"line_length": 79, "format_timeout": 5}))
    fmt, timeout = _passes()[1], _passes()[2]
    assert "79" in fmt
    assert timeout == 5


# --------------------------------------------------------------------------- storage


def test_git_settings_build_a_configured_target() -> None:
    cfg = GitStorageSettings(
        provider="github",
        repo="acme/dags",
        token="tok",
        branch_template="dags/{environment_path}{dag_id}",
        commit_message_template="chore(dags): {dag_id} by {author}",
        pr_title_template="[{environment}] {dag_id}",
        pr_body_template="{message}\n\n-- bot",
        pr_labels=["autoair"],
        pr_reviewers=["alice", "acme/data"],
        pr_draft=True,
        delete_branch_after_merge=False,
        direct_push_roles=["Admin"],
        max_attempts=5,
        read_cache_size=7,
        timeout=3,
    )
    target = build_target(StorageSettings(mode="git", git=cfg), default_local_path=Path("/x"))
    assert isinstance(target, GitTarget)
    assert target.pr_options == PullRequestOptions(
        draft=True, labels=("autoair",), reviewers=("alice", "acme/data"), delete_branch=False
    )
    assert (target.max_attempts, target.cache_size) == (5, 7)
    assert target.direct_push_roles == frozenset({"Admin"})
    assert target.pending_branch("d", "prod") == "dags/prod/d"
    assert target.pending_branch("d") == "dags/d"


def test_templates_are_validated() -> None:
    with pytest.raises(ValueError, match="unknown placeholder"):
        GitStorageSettings(commit_message_template="{nope}")
    with pytest.raises(ValueError, match="file_mode"):
        StorageSettings(file_mode="999")


def test_pr_options_and_templates_reach_github(simple_spec_data) -> None:
    repo = FakeRepo()
    inner = github_app(repo)
    extra: list[tuple[str, dict]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/labels") or path.endswith("/requested_reviewers"):
            extra.append((path, json.loads(request.content)))
            return httpx2.Response(200, json={})
        return inner(request)

    gh = GitHubProvider("acme/dags", "tok", transport=httpx2.MockTransport(handler))
    target = GitTarget(
        gh,
        base_branch="main",
        mode="pr",
        branch_template="feature/{dag_id}",
        commit_message_template="deploy {dag_id} ({author})",
        pr_title_template="Deploy {dag_id}",
        pr_body_template="{message} -- reviewed by bots",
        pr_options=PullRequestOptions(draft=True, labels=("dags",), reviewers=("bob", "acme/team")),
    )
    spec = DagSpec.model_validate(simple_spec_data)
    result = target.save(spec, {"simple.py": SRC}, author="alice")
    assert result.branch == "feature/simple"
    pr = repo.prs[0]
    assert pr["title"] == "Deploy simple"
    assert pr["body"] == "deploy simple (alice) -- reviewed by bots"
    assert pr["draft"] is True
    assert extra == [
        ("/repos/acme/dags/issues/1/labels", {"labels": ["dags"]}),
        (
            "/repos/acme/dags/pulls/1/requested_reviewers",
            {"reviewers": ["bob"], "team_reviewers": ["team"]},
        ),
    ]
    # direct push for this save (the deploy route decides it from deploy:direct_push)
    pushed = target.save(
        spec.model_copy(update={"description": "x"}), {"simple.py": SRC}, mode="push"
    )
    assert pushed.branch == "main"


def test_gitlab_bitbucket_azure_pr_payloads() -> None:
    from autoair.storage.git.azure import AzureDevOpsProvider
    from autoair.storage.git.bitbucket import BitbucketProvider
    from autoair.storage.git.gitlab import GitLabProvider

    seen: dict[str, dict] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/users"):
            return httpx2.Response(200, json=[{"id": 42}])
        body = json.loads(request.content)
        seen[path] = body
        if "merge_requests" in path:
            return httpx2.Response(201, json={"web_url": "u", "iid": 1})
        if "pullrequests" in path and "repositories" in path and "_apis" not in str(request.url):
            return httpx2.Response(201, json={"links": {"html": {"href": "u"}}, "id": 1})
        return httpx2.Response(201, json={"pullRequestId": 1})

    opts = PullRequestOptions(draft=True, labels=("x",), reviewers=("bob",), delete_branch=False)
    t = httpx2.MockTransport(handler)
    GitLabProvider("g/p", "tok", transport=t).open_pr("h", "main", "T", "B", options=opts)
    mr = next(v for k, v in seen.items() if "merge_requests" in k)
    assert mr["title"] == "Draft: T"
    assert mr["remove_source_branch"] is False
    assert (mr["labels"], mr["reviewer_ids"]) == ("x", [42])
    BitbucketProvider("ws/repo", "tok", transport=t).open_pr("h", "main", "T", "B", options=opts)
    bb = next(v for k, v in seen.items() if k.endswith("/repositories/ws/repo/pullrequests"))
    assert bb["draft"] is True
    assert bb["close_source_branch"] is False
    assert bb["reviewers"] == [{"account_id": "bob"}]
    AzureDevOpsProvider("org/proj/repo", "tok", transport=t).open_pr(
        "h", "main", "T", "B", options=opts
    )
    az = next(v for k, v in seen.items() if k.endswith("/pullrequests") and "isDraft" in v)
    assert az["isDraft"] is True
    assert az["completionOptions"] == {"deleteSourceBranch": False}
    assert az["labels"] == [{"name": "x"}]
    assert az["reviewers"] == [{"id": "bob"}]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_local_file_and_dir_modes_and_temp_dir(tmp_path: Path, simple_spec_data) -> None:
    temp = tmp_path / "tmp"
    target = build_target(
        StorageSettings(
            local_path=tmp_path / "dags", file_mode="0600", dir_mode="0750", temp_dir=temp
        ),
        default_local_path=tmp_path,
    )
    spec = DagSpec.model_validate(simple_spec_data)
    target.save(spec, {"simple.py": '"""Generated by AutoAir"""\n'})
    written = tmp_path / "dags" / "simple.py"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "dags" / ".autoair").stat().st_mode) == 0o750
    assert temp.is_dir()
    assert not any(temp.iterdir())  # temp files renamed away


# --------------------------------------------------------------------------- features / meta


def test_effective_features_apply_policy_switches() -> None:
    s = make_settings(allow_python_code=False)
    flags = s.effective_features()
    assert flags["python_code"] is False
    assert flags["marketplace"] is True
    assert make_settings(features={"collab": False}).feature("collab") is False


def test_cli_respects_feature_flags(tmp_path: Path) -> None:
    env = {**os.environ, "AUTOAIR_FEATURES__IMPORTS": "false", "AUTOAIR_DAGS_FOLDER": str(tmp_path)}
    (tmp_path / "d.yaml").write_text("dag_id: d\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from autoair.cli import app; app()",
            "import-yaml",
            str(tmp_path / "d.yaml"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 2, proc.stderr
    assert "imports feature is disabled" in proc.stderr


# --------------------------------------------------------------------------- generated docs


def test_hardcoded_audit_is_complete_and_fresh() -> None:
    """Every finding has a decision and no decision is stale (no committed report to compare)."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scan_hardcoded.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_configuration_reference_is_fresh() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import gen_config_docs
    finally:
        sys.path.pop(0)
    expected = gen_config_docs.render() + "\n"
    current = (ROOT / "docs" / "configuration-reference.md").read_text(encoding="utf-8")
    assert current == expected, "run: uv run python scripts/gen_config_docs.py"


def test_every_setting_is_documented() -> None:
    from autoair.config_reference import iter_options

    mine = (
        "features.",
        "discovery.",
        "policy.",
        "runner.",
        "editor.",
        "codegen.",
        "interchange.",
        "icons.",
        "sandbox.",
        "server.",
        "plugin.",
        "storage.",
    )
    undocumented = [
        o.key
        for o in iter_options(Settings)
        if not o.description and (o.section == "" or o.key.startswith(mine))
    ]
    assert undocumented == []
