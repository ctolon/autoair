# Source-preserving deploys

User guide: [Editing existing Python DAGs](https://ctolon.github.io/autoair/docs/user-guide/editing-python-dags).

How a deploy decides what to write as a DAG file, how a hand-written file is patched instead of
regenerated, and how files changed outside AutoAir are detected. Code: `src/autoair/preserve/`
(`mapper.py`, `patcher.py`, `service.py`, `fingerprint.py`, `config.py`),
`src/autoair/api/source_plans.py`, the deploy route (`api/routers/specs.py`) and the storage
targets (`storage/files.py`, `storage/git/target.py`).

## Model

`DagSpec` stays the only logical model. LibCST is used at two boundaries only: a static *map*
of a file and a *patch* of it. A spec carries an optional `source` (`DagSource`):

| Field | Meaning |
|---|---|
| `policy` | `auto` (default), `generate`, `preserve`, `strict-preserve` |
| `origin` | `autoair` or `python` (imported, applied in the code mode, or chosen for an ejected file) |
| `code` | the hand-written file the spec was last imported from or applied in the code mode |
| `base_sha` | sha256 of the file the editor started from (see [Outside changes](#outside-changes)) |

`source` is `None` for every DAG built in AutoAir, so existing specs and their etags are
unchanged. The field is optional and additive, so the migration `_v2_to_v3`
(`spec/migrations.py`) only sets the version, but `SPEC_VERSION` is 3 since AutoAir 0.2.0
(`source` and the `blueprint` node kind are new in version 3). Without the bump AutoAir 0.1.0
would fail on a 0.2.0 document with a Pydantic error ("Input tag 'blueprint' found using
'kind' does not match any of the expected tags", or "Extra inputs are not permitted" for
`source`); with it, 0.1.0's migrator refuses the document with "spec_version 3 is newer than
this AutoAir supports (2); upgrade AutoAir".

## The plan

`plan_source(spec, existing=..., stored=..., ctx=...)` (`preserve/service.py`) returns a
`SourcePlan` (policy, action, target state, diff, operations, warnings, refusals, options; the
content to write and the expected hash are private attributes). The deploy route, the preview
route and the MCP tools all call it through `api/source_plans.plan_for`, which reads the
target's current file (`DeployTarget.current_file`, implemented by `FileTarget` and
`GitTarget`; on git the pending PR branch in PR mode) and the stored spec.

1. **Classify the target's file**: `absent`; `clean` (its hash is the one AutoAir recorded in
   `.autoair/state/<dag_id>.json`, or equals `source.code`, or its fingerprint is valid, or it
   is an owned file from before fingerprints); `ejected` (fingerprint mismatch, or a recorded
   hash that no longer matches); `foreign` (no generated-file marker, never written).
2. **Resolve the policy** (`resolve_policy`): `auto` becomes preserve for `origin: python`,
   else generate; `features.source_preserve` off turns auto into generate and refuses explicit
   preserve; `features.strict_source_preserve` turns preserve into strict; a policy outside
   `preserve.allowed_policies` is refused.
3. **Generate**: the generated file with a fingerprint. An `ejected` or `foreign` target stops
   the deploy (`blocked`) unless the request answers it (`source_resolution`) or
   `preserve.ejected_files=warn` applies preserve automatically. Every overwrite needs
   `preserve.allow_force_overwrite=true` (`can_force_overwrite`; refusal code
   `overwrite_disabled` otherwise) and the `deploy:overwrite_ejected` permission. An
   `ejected` file also needs `ejected_files=overwrite` and AutoAir's marker; a `foreign` file
   is never overwritten (`foreign`). Regenerating the kept `hand_written` file (a deliberate
   switch to generate) does not need `ejected_files=overwrite`. The plan offers the
   `overwrite` option only when it would be accepted.
4. **Preserve**: pick the base file, import it, diff in generated-code space, patch, round trip.

### Base file

The base is the target's file, except when `source.code` is newer than what was deployed: the
target state records `source_sha` (hash of `source.code` at the last deploy), so a spec that
still carries the same code patches the target's file, and a new code (an import, a code-mode
edit) patches that code. Without either, the first file is generated.

### Generated-code space

The change is computed without knowing how spec fields map to keyword arguments: the base file
is imported (`import_python_sources`, the Python importer in a sandboxed subprocess with
`user_code_env`), and AutoAir generates the code of the imported spec (`old`) and of the
intended spec (`new`). Both come from one generator, so every difference between them is part
of the change: DAG, task and group keyword arguments (compared as AST dumps with task
variables replaced by task ids), TaskFlow bodies and signatures, call arguments, added or
removed tasks, groups, edges (expanded to task level through group roots and leaves) and other
DAG statements (`t.as_setup()`), and module-level definitions.

### Map and patch

`map_source(source, dag_id)` (`mapper.py`) finds the DAG (`with DAG(...)`, `@dag`,
`dag = DAG(...)`) and, inside it, every task with a literal `task_id` (operator calls,
`.partial().expand()`, TaskFlow functions and their call sites, tasks in `with TaskGroup`
blocks or `task_group=`), groups and dependency statements (`>>`, `<<`, lists, labels,
`chain`, `set_upstream/downstream`), plus data dependencies (a task variable passed to a
task). Tasks it cannot pin to one node are *limited* (loops, comprehensions, factories, a
TaskFlow function called twice, `.override()`); computed ids are listed as unmapped.

`patch_source(base, old, new, strict=...)` (`patcher.py`) turns each difference into an edit
addressed by node identity (keyword set/remove keeping comma and closing-line layout and
end-of-line comments, string literal replacement for renames, function body/parameter
replacement, statement removal, chain surgery, statement insertion after the last task
definition or inside the group's `with` block, imports copied from the generated module for
free names) and applies all of them in one `CSTTransformer` pass. Edits it cannot locate are
refusals; *risky* edits (replacing a value that refers to a variable of the file, changing an
operator class or decorator, moving a task through `task_group=`) are warnings in preserve and
refusals in strict. New variables follow the file's naming when its task variables are named
after task ids. The result must compile and must not reference removed variables. Nothing is
ever formatted: LibCST keeps the file's bytes outside the edited nodes, and inserted code comes
from the ruff-formatted generated module.

### Round trip

The patched file and the generated reference of the intended spec are imported, each in its
own subprocess (a file can only influence its own result, never another file's or the
cache); their specs must be identical in the importer's view (`_canonical`: no `source`,
task code compared as AST). When the generated reference does not import (the base file uses
something the importer cannot represent, e.g. positional TaskFlow arguments), the patch is
compared field by field: the change from the imported base to the patched file must equal the
change from the imported base to the intended spec (`_change_mismatch`, node ids rekeyed the
way the importer derives them). Any mismatch rejects the patch (`roundtrip`). The deploy then
validates the patched file (`validate_spec(..., source=...)`: DagBag import check of that file,
secret and operator scans of its hand-written parts).

### Outside changes

When the base is a target file that was changed outside AutoAir, the outside change and the
user's change are merged first (`merge_specs`). The common ancestor is the import of
`source.code` only while that code is the file the editor started from
(`source.base_sha` equals its hash; an edit in the code mode changes it); otherwise the stored
spec's generated file, so code-mode edits count as the user's change. Conflicts refuse the
patch. A hand-written file identical to the code the user imported is no outside change. The
spec stored afterwards is `carry_canvas_state(merged, spec)` marked as hand-written, returned
to the client as `stored_spec`. Merging is not attempted when the DAG has environments.

## Writes

The write is conditional on the base: targets take `expected={name: sha256}` and overwrite a
file (also one without AutoAir's marker) only while it still has that hash
(`storage/base.check_overwrite`); a changed file raises `EjectedError` and the deploy asks for
a new preview. Without `expected`, a file without AutoAir's marker is never overwritten,
also not one AutoAir wrote before, and `force` (ejected overwrite) is honoured only with
`preserve.allow_force_overwrite=true` and `deploy:overwrite_ejected` (the deploy route clears
`force` otherwise). The target state records the file hashes, `source_sha` and
`hand_written`: the written file is a hand-written one AutoAir keeps (a preserve write whose
result has no valid fingerprint, or a raw code takeover). Such a file classifies as
`hand_written`: Preserve patches it, but Generate stops (regenerating it needs
`preserve.allow_force_overwrite=true`, `deploy:overwrite_ejected` and an explicit
`source_resolution=overwrite`), and deleting the DAG needs only the permission
(`allow_hand_written` on the targets' `delete`). The targets
enforce this themselves on every write, not only through the plan: `save` refuses to replace
or remove (stale file of another output mode) the kept file unless `expected` covers it or
the caller passes `allow_hand_written`. The deploy route passes it only for a confirmed
`force` (which needs `preserve.allow_force_overwrite` and `deploy:overwrite_ejected`) and for
a raw code DAG that was one already; MCP `save_raw_dag_code` passes it for a raw code DAG that
was one already (`raw_write`). Loader/YAML output modes, conversions to raw code, other MCP
saves and promotions are refused otherwise. Even with `allow_hand_written`, `check_overwrite`
still refuses a file without AutoAir's marker, so `force` replaces only a kept file that
carries the marker; a kept file without it (a hand-written file patched by a preserve deploy)
changes only through a source plan.

The deploy route reports the refusal (`HandWrittenError`) as HTTP 200 with
`deployed=false`, `status="ejected"`, `conflict.ejected_file`, `conflict.hand_written=true`
and `conflict.force_allowed`, which is true only when a retry with `force=true` would be
accepted (switch, permission and marker; `_force_refusal` names what is missing in the
message). An `EjectedError` without a source plan carries `conflict.force_allowed` the same
way. `/api/meta` reports `allow_force_overwrite`, and the UI shows the overwrite button only
when it is true and the user holds the permission.

A deploy can be bound to its preview: the plan reports `target_sha` (hash of the target's
file, `""` for none) and the deploy request (`source_target_sha`) or MCP save
(`expected_target_sha`) stops when the re-planned file differs. Content of a hand-written
target file AutoAir never wrote (and the user did not import) is not returned
(`content_hidden`). Base files larger than `RAW_CODE_MAX_CHARS` are not patched, and the diff
stops at `preserve.max_diff_chars`. DAGs with Blueprint steps are refused before any patch.

## Fingerprints

`render_dag_files` prefixes every generated Python file (not raw code DAGs, loader stubs or
YAML) with `# autoair:fingerprint sha256=<hash of the rest>` (`preserve/fingerprint.py`).
`codegen.render` itself is unchanged, so the code preview and the golden files are not affected. A patched file whose fingerprint was valid gets it refreshed;
a stale fingerprint (a generated file edited by hand) is removed, and the file becomes a kept
hand-written file. A hand-written file never gets one.

## Replicas and caches

No new shared state: the plan reads the deploy target and the stored spec, and writes go
through the targets' existing locking and compare-and-swap. Imports are cached per process
(`_ImportCache`, keyed by dag id, file hash and import path; successful imports only; TTL
`preserve.cache_ttl`), so a preview and the following deploy do not import the same file
twice. Replicas never need to agree on that cache.

## Security

- The round trip executes the DAG file like an import (sandboxed subprocess, no AutoAir
  secrets); it needs `features.python_code` and `code:execute` (unless
  `iam.import_check_requires_code_execute` is off).
- A deploy whose written file is the hand-written code sent with the spec (`source.code`)
  needs `code:raw`: it is arbitrary Python, like a raw code DAG.
- On local folder targets the dags folder is on the import path, as in Airflow.
- Inserted code is generated code (AST literals), never request text; values are copied from
  the generated module.

## Not supported

`dag_id` renames, edge label changes, adding or removing mapping, moving tasks out of `with`
blocks, adding tasks to `dag = DAG(...)` style files, promotion of preserved DAGs (refused, the
target branch file would be regenerated), merging with environments, a four-eyes proposal
carrying `source_resolution`.
