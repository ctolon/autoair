"""Safety rules of source-preserving deploys: hand-written files AutoAir keeps, outside
changes, merge ancestors, hidden content, blueprints, limits (fake importer, no Airflow)."""

from __future__ import annotations

import copy
from typing import Any

from autoair.preserve import fingerprint
from autoair.preserve.config import PreserveSettings
from autoair.preserve.service import (
    SourcePlan,
    _change_mismatch,
    _diff,
    mark_hand_written,
    plan_source,
)
from autoair.spec import DagSource, DagSpec
from autoair.storage.base import ExistingFile, sha256_text
from tests.unit.test_preserve_patcher import CLASSIC, CLASSIC_SPEC, node
from tests.unit.test_preserve_service import SPEC, ctx, existing, fake_importer, gen


def _edited_generated() -> ExistingFile:
    generated = fingerprint.add(gen(SPEC))
    return existing(
        generated.replace("    t_load = ", "    # tuned by hand\n    t_load = "),
        recorded=sha256_text(generated),
    )


def _changed() -> DagSpec:
    data = copy.deepcopy(CLASSIC_SPEC)
    node(data, "load")["params"]["bash_command"] = "echo load --full"
    return DagSpec.model_validate(data)


def test_generate_never_regenerates_the_kept_hand_written_file() -> None:
    kept = existing(CLASSIC, recorded=sha256_text(CLASSIC))
    kept.hand_written = True
    cfg = ctx(fake_importer({}, lambda _s: None))
    plan = plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg)
    assert plan.action == "blocked"
    assert plan.target_state == "hand_written"
    assert "overwrite" not in plan.options  # no permission
    refused = plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg, resolution="overwrite")
    assert refused.action == "blocked"
    assert "preserve.allow_force_overwrite" in refused.message  # off by default
    cfg.can_overwrite = True
    # the permission alone is not enough: the overwrite is neither offered nor accepted
    assert "overwrite" not in plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg).options
    off = plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg, resolution="overwrite")
    assert off.action == "blocked"
    assert off.source is None
    cfg = ctx(fake_importer({}, lambda _s: None), allow_force_overwrite=True)
    no_right = plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg, resolution="overwrite")
    assert no_right.action == "blocked"
    assert "deploy:overwrite_ejected" in no_right.message
    cfg.can_overwrite = True  # a deliberate switch to generate, with switch and permission
    assert "overwrite" in plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg).options
    done = plan_source(SPEC, existing=kept, stored=SPEC, ctx=cfg, resolution="overwrite")
    assert done.action == "generate"
    assert done.overwrite
    assert done.hand_written is False
    assert done.expected_sha == sha256_text(CLASSIC)


def test_ejected_files_warn_applies_preserve_and_marks_the_file_hand_written() -> None:
    target = _edited_generated()
    new = _changed()

    def lookup(src: str) -> DagSpec | None:
        return new if "--full" in src else SPEC

    cfg = ctx(fake_importer({}, lookup), ejected_files="warn")
    plan = plan_source(new, existing=target, stored=SPEC, ctx=cfg)
    assert plan.action == "preserve", plan.refusals
    assert any("ejected_files=warn" in w for w in plan.warnings)
    assert plan.source is not None
    assert "# tuned by hand" in plan.source
    # the stale fingerprint is dropped: the file is a hand-written one from now on
    assert fingerprint.state(plan.source) == "absent"
    assert plan.hand_written is True
    assert plan.stored_spec is not None
    assert plan.stored_spec.source is not None
    assert plan.stored_spec.source.origin == "python"


def test_outside_changes_are_not_merged_with_environment_overlays() -> None:
    data = copy.deepcopy(CLASSIC_SPEC)
    data["environments"] = {"prod": {"tags": ["prod"]}}
    spec = DagSpec.model_validate(data)
    plan = plan_source(
        spec,
        effective=spec.with_environment("prod"),
        existing=_edited_generated(),
        stored=spec,
        ctx=ctx(fake_importer({}, lambda _s: SPEC)),
        resolution="preserve",
        environment="prod",
    )
    assert plan.action == "refused"
    assert plan.refusals[0].code == "conflict"
    assert "environment overlays" in plan.refusals[0].message


def test_environment_overlay_reaches_the_patched_file() -> None:
    data = copy.deepcopy(CLASSIC_SPEC)
    data["environments"] = {"prod": {"tags": ["prod_only"]}}
    spec = mark_hand_written(DagSpec.model_validate(data), CLASSIC)
    effective = spec.with_environment("prod")
    want = effective.model_copy(update={"source": None})

    def lookup(src: str) -> DagSpec | None:
        return want if "prod_only" in src else SPEC

    plan = plan_source(
        spec,
        effective=effective,
        existing=None,
        stored=None,
        ctx=ctx(fake_importer({CLASSIC: SPEC}, lookup)),
        environment="prod",
    )
    assert plan.action == "preserve", plan.refusals
    assert plan.source is not None
    assert "prod_only" in plan.source


def test_code_mode_edits_merge_against_the_last_deployed_version() -> None:
    """A code-mode edit is not the file the editor started from (base_sha differs): the
    stored spec is the common ancestor, so the edit survives the merge."""
    edited = CLASSIC.replace("echo load", "echo load --full")
    spec = mark_hand_written(_changed(), CLASSIC)
    assert spec.source is not None
    spec = spec.model_copy(update={"source": spec.source.model_copy(update={"code": edited})})
    seen: list[str] = []

    def lookup(src: str) -> DagSpec | None:
        seen.append(src)
        return _changed() if "--full" in src else SPEC

    plan_source(spec, existing=_edited_generated(), stored=SPEC, ctx=ctx(fake_importer({}, lookup)))
    assert edited not in seen  # never used as the ancestor
    assert gen(SPEC) in seen  # the version deployed last is


def test_hand_written_files_of_others_are_not_shown() -> None:
    cfg = ctx(fake_importer({}, lambda _s: None))
    plan = plan_source(SPEC, existing=existing(CLASSIC), stored=None, ctx=cfg)
    assert plan.action == "blocked"
    assert plan.content_hidden is True
    assert plan.diff == ""
    assert plan.current_code is None
    own_spec = SPEC.model_copy(
        update={"source": DagSource(policy="generate", origin="python", code=CLASSIC)}
    )
    own = plan_source(own_spec, existing=existing(CLASSIC), stored=None, ctx=cfg)
    assert own.content_hidden is False  # the user imported this very file


def test_target_sha_identifies_the_previewed_file() -> None:
    cfg = ctx(fake_importer({}, lambda _s: None))
    assert plan_source(SPEC, existing=None, stored=None, ctx=cfg).target_sha == ""
    generated = existing(fingerprint.add(gen(SPEC)))
    plan = plan_source(SPEC, existing=generated, stored=None, ctx=cfg)
    assert plan.target_sha == sha256_text(generated.content)


def test_blueprint_steps_are_refused_early() -> None:
    data = copy.deepcopy(CLASSIC_SPEC)
    data["nodes"].append(
        {
            "kind": "blueprint",
            "id": "bp",
            "step_id": "extract_step",
            "blueprint": "extract",
            "class_path": "acme.blueprints.Extract",
            "config": {},
        }
    )
    spec = mark_hand_written(DagSpec.model_validate(data), CLASSIC)
    cfg = ctx(fake_importer({}, lambda _s: None))
    plan = plan_source(spec, existing=None, stored=None, ctx=cfg)
    assert plan.action == "refused"
    assert plan.refusals[0].code == "unsupported"
    assert "Blueprint" in plan.refusals[0].message


def test_refusals_offer_generate_only_when_allowed() -> None:
    cfg = ctx(fake_importer({}, lambda _s: None), allowed_policies=["preserve"])
    plan = plan_source(mark_hand_written(_changed(), CLASSIC), existing=None, stored=None, ctx=cfg)
    assert plan.action == "refused"
    assert "generate" not in plan.options


def test_oversized_code_is_not_kept() -> None:
    from autoair.spec.models import RAW_CODE_MAX_CHARS

    notes: list[str] = []
    spec = mark_hand_written(SPEC, "x" * (RAW_CODE_MAX_CHARS + 1), notes)
    assert spec.source is not None
    assert spec.source.code is None
    assert spec.source.origin == "python"
    assert notes
    assert "too large" in notes[0]


def test_change_mismatch_compares_the_change_itself() -> None:
    new = _changed()
    assert _change_mismatch(SPEC, new, new) == []
    other = copy.deepcopy(CLASSIC_SPEC)
    node(other, "report")["params"]["bash_command"] = "echo other"
    wrong = _change_mismatch(SPEC, DagSpec.model_validate(other), new)
    assert any("bash_command" in path for path in wrong)


def test_long_diffs_are_cut() -> None:
    plan = SourcePlan()
    before = "".join(f"line {i}\n" for i in range(2000))
    _diff(plan, before, "", "x.py", PreserveSettings(max_diff_chars=1000))
    assert plan.diff_truncated is True
    assert len(plan.diff) == 1000


def test_hidden_files_do_not_leak_through_import_errors() -> None:
    """Import tracebacks quote the file: for a file the caller may not see, only what
    failed is reported."""
    from autoair.importer.python_dag import PythonImportResult

    quoted = "    password = 'hunter2-from-the-hidden-file'"

    def run(sources: Any, timeout: float) -> list[PythonImportResult]:
        error = f'Traceback (most recent call last):\n  File "classic.py", line 3\n{quoted}\n'
        error += "ValueError: hunter2-from-the-hidden-file"
        return [PythonImportResult(dags=[], import_errors=[error]) for _ in sources]

    plan = plan_source(
        SPEC, existing=existing(CLASSIC), stored=SPEC, ctx=ctx(run), resolution="preserve"
    )
    assert plan.action == "refused"
    assert plan.content_hidden is True
    assert plan.refusals[0].code == "import"
    assert "hunter2" not in plan.model_dump_json()
    assert "does not import" in plan.message
    assert "details are not shown" in plan.refusals[0].message
    # the user's own imported file keeps the details
    own_spec = SPEC.model_copy(
        update={"source": DagSource(policy="preserve", origin="python", code=CLASSIC)}
    )
    own = plan_source(own_spec, existing=existing(CLASSIC), stored=SPEC, ctx=ctx(run))
    assert own.content_hidden is False
    assert "hunter2" in own.refusals[0].message


def test_deploy_result_omits_the_merged_spec_of_a_hidden_file() -> None:
    from autoair.api.routers.specs import _returned_spec

    merged = SPEC.model_copy(update={"description": "merged with the hidden file"})
    plan = SourcePlan()
    assert _returned_spec(merged, SPEC, plan) is merged
    assert _returned_spec(SPEC, SPEC, plan) is None
    plan.content_hidden = True
    assert _returned_spec(merged, SPEC, plan) is None
    assert _returned_spec(merged, SPEC, None) is merged
