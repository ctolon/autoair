---
title: Editing existing Python DAGs
description: Keep hand-written DAG files as they are while you edit them on the canvas - source policies, what can be patched, the deploy diff, and DAG files changed outside AutoAir.
sidebar_label: Editing existing Python DAGs
---

A DAG imported from a hand-written Python file keeps that file. When you change it on the
canvas or in the code mode and deploy, AutoAir applies only your change to the existing file
(a minimal patch, made with [LibCST](https://libcst.readthedocs.io/)): comments, formatting,
ordering and code AutoAir does not model stay exactly as they were. A DAG built in AutoAir
keeps getting a generated file.

The DAG spec stays the single model you edit. The patch is computed at deploy time from the
difference between what the file currently is and what you changed, and it is verified before
anything is written.

## Source policies

Each Python DAG has a **Source policy** (`source.policy` in the spec). Set it in the DAG
settings (**General** tab, **Source policy**) or in the deploy dialog (**DAG file**).

| Policy | What a deploy writes |
|---|---|
| **Auto** (default) | **Preserve** for DAGs that come from hand-written Python (imported, applied in the code mode, or a file changed outside AutoAir that you chose to keep), **Generate** for DAGs built in AutoAir. The select shows what Auto means for the open DAG, e.g. `Auto (Preserve)`. |
| **Generate** | The file AutoAir generates from the spec. A file changed outside AutoAir is never overwritten silently (see [Files changed outside AutoAir](#files-changed-outside-autoair)). |
| **Preserve** | Your change patched into the existing file. A change that cannot be applied stops the deploy with an explanation; nothing is regenerated instead. |
| **Strict preserve** | Like Preserve, and also refuses the edits Preserve makes with a warning (see the table below). |

The source policy applies to the **Generated Python** output mode only. Loader stubs and YAML
DAGs are always generated, and raw code DAGs are deployed as written; for them the select is
disabled or hidden.

Administrators can restrict policies:

| Setting | Default | Effect |
|---|---|---|
| `features.source_preserve` | `true` | Off: Auto means Generate for every DAG, and a DAG that explicitly asks for Preserve or Strict preserve is refused until the flag is back on. Files changed outside AutoAir stay protected. Switchable at runtime in the admin panel. |
| `features.strict_source_preserve` | `false` | Every Preserve deploy runs as Strict preserve. Switchable at runtime. |
| `preserve.allowed_policies` | all three | Policies DAGs may use. A DAG that asks for another one is refused at deploy; Auto picks the closest allowed one (Preserve, then Strict preserve, then Generate). Remove `generate` to make sure a hand-written file is never regenerated. |

All settings are in the [`preserve` reference](/docs/reference/configuration#preserve) and the
flags in [Feature flags](/docs/admin/feature-flags).

## What a deploy does

The deploy dialog shows, under **DAG file**, the policy in force, what the deploy will do and a
unified diff of the DAG file. The same preview is available as
`POST /api/specs/{dag_id}/source-preview` (permission `spec:write`).

For a Preserve deploy AutoAir:

1. picks the file to patch: the DAG file in the deploy target, or the hand-written code the
   spec carries (`source.code`) when that is newer, for example right after an import or an
   edit applied in the code mode;
2. imports that file in a sandboxed subprocess (like a Python import) and generates the code
   of both the imported spec and your spec; the difference between the two is your change;
3. locates each difference in the file through a static map of it (which statement defines
   which task, task group and dependency) and patches only there;
4. imports the patched file again and compares it with your spec (the *round trip*). If the
   patched file does not import as exactly your change, the patch is rejected;
5. validates the patched file like any deploy, including the DagBag import check when it is
   on.

The write is conditional: if the file in the target changed between the preview and the
write, the deploy stops and asks you to preview again.

Preserve needs the `code:execute` permission, because the round trip runs the file's code (in
the same sandbox as an import). When the written file is the hand-written code sent with the
spec (step 1, after an import or a code-mode edit), the deploy also needs `code:raw`: that file
is arbitrary Python, like a raw code DAG. Without it the deploy stops and offers to switch the
DAG to **Generate**.

A deploy of a DAG whose file is unchanged writes the hand-written file as it is. The first
deploy of an imported DAG therefore writes exactly the file you imported.

## What can be patched

| Change | Preserve | Strict preserve |
|---|---|---|
| Add, change or remove a keyword argument of the DAG, a task or a task group | yes | yes |
| Change a value that is written as a variable or expression of the file (`default_args=DEFAULTS`) | replaced by the literal value, with a warning | refused |
| Change a TaskFlow function's body or parameters, or its call arguments | yes | yes |
| Add a task (inserted after the last task definition of the DAG, or inside its `with TaskGroup` block) and the imports it needs | yes | yes |
| Add a dependency (a `a >> b` statement is appended) | yes | yes |
| Rename a task whose `task_id` is a literal | yes (the literal changes; a TaskFlow task gets `task_id=`) | yes |
| Remove a task that is defined on its own line, with its dependencies | yes | yes |
| Remove a dependency: a whole statement, one link of `a >> b >> c`, or one entry of a list at the end of a chain | yes | yes |
| Change the operator class or the TaskFlow decorator | yes, with a warning | refused |
| Move a task to another group through `task_group=` | yes, with a warning | refused |

Always refused, with the reason in the deploy dialog:

- changes to tasks AutoAir cannot pin to one statement: a computed `task_id` (f-strings,
  variables), tasks created in loops, comprehensions or factory functions, a TaskFlow
  function called more than once;
- renaming the DAG (`dag_id`), changing an edge label, adding or removing dynamic task
  mapping, moving a task out of a `with TaskGroup` block, removing a task group that still has
  tasks;
- adding tasks to a DAG created with `dag = DAG(...)` (its tasks are attached through `dag=`);
- removing a dependency that the file expresses some other way (a helper function, a group);
- a file that does not import, and a patch that fails the round trip.

Code the map cannot read is left as written and listed as a warning (`not mapped`). When a
patch is refused, the dialog **This change cannot be applied to the existing file** offers:

- **Keep as raw code DAG**: the file becomes a [raw code DAG](/docs/user-guide/code-mode#raw-code-dags)
  that you edit as code (needs `code:raw`);
- **Switch to generated code**: sets the source policy to **Generate** (an undoable edit) and
  reopens the deploy dialog, so you regenerate the file deliberately.

## Files changed outside AutoAir

Every file AutoAir generates starts with a fingerprint line:

```python
# autoair:fingerprint sha256=<hash of the rest of the file>
```

A file whose content no longer matches its fingerprint, or the hash AutoAir recorded when it
last wrote it, was changed outside AutoAir (*ejected*). A file that AutoAir never wrote and
that has no generated-file marker is *hand-written*. On folder, object storage and git targets
alike, a deploy that would regenerate such a file stops with the dialog **The DAG file was
changed outside AutoAir**:

| Button | Effect |
|---|---|
| **Apply to the file** | Your change is applied to the changed file with Preserve. The outside changes and yours are merged first (three-way, from the version deployed last); conflicting edits stop the deploy. Later deploys of the DAG use Preserve (Auto). |
| **Overwrite the file** | Regenerates the file and discards the outside changes. Shown only when `preserve.allow_force_overwrite` is `true`, `preserve.ejected_files` is `overwrite` and you hold the permission `deploy:overwrite_ejected`; needs the confirmation checkbox. Hand-written files without AutoAir's marker are never overwritten. |
| **Keep as raw code DAG** | The changed file becomes a raw code DAG. |

`preserve.ejected_files` decides what happens first:

| Value | Effect |
|---|---|
| `block` (default) | The deploy stops and offers **Apply to the file** and **Keep as raw code DAG**. |
| `warn` | The change is applied to the file with Preserve right away and the deploy reports a warning; it stops only when that is not possible. |
| `overwrite` | The deploy stops and also offers **Overwrite the file**, but only with `preserve.allow_force_overwrite=true`. Without it, `overwrite` behaves like `block` for every user, administrators included. |

### Overwriting is off by default

`preserve.allow_force_overwrite` (`AUTOAIR_PRESERVE__ALLOW_FORCE_OVERWRITE`, default `false`)
must be `true` before AutoAir offers or accepts any overwrite (**Overwrite the file**, or
**Overwrite (force)** in the conflict dialog) of:

- the hand-written file AutoAir keeps for a DAG (written by a Preserve deploy or a raw code takeover);
- a DAG file changed outside AutoAir (ejected);
- the file of a `force=true` deploy of the loader or YAML output mode or of a raw code DAG.

Each overwrite also needs the permission `deploy:overwrite_ejected` and an explicit
confirmation. While the setting is `false`, the overwrite buttons are hidden, the API refuses
the overwrite (refusal code `overwrite_disabled` in the source plan), and such files change only
through **Apply to the file** or **Keep as raw code DAG**. `GET /api/meta` reports the value as
`allow_force_overwrite`.

| File in the target | Overwrite needs |
|---|---|
| Generated Python file changed outside AutoAir | `preserve.allow_force_overwrite=true`, `preserve.ejected_files=overwrite`, `deploy:overwrite_ejected` |
| Hand-written file AutoAir keeps (switch to **Generate**) | `preserve.allow_force_overwrite=true`, `deploy:overwrite_ejected` |
| Hand-written file AutoAir did not create | Never overwritten |

### Kept hand-written files

A hand-written file AutoAir wrote with Preserve is *kept*: switching the DAG to **Generate** stops at the same dialog, and regenerating the file (**Overwrite the file**) needs `preserve.allow_force_overwrite=true` and `deploy:overwrite_ejected` (not `preserve.ejected_files`). Deleting the DAG needs only `deploy:overwrite_ejected`.

The deploy target enforces this on every write. Switching such a DAG to another output mode (loader, YAML) or converting it to a raw code DAG stops with status `ejected` (HTTP 200, `deployed: false`, `conflict.hand_written: true`). `conflict.force_allowed` is `true` only when a retry with `force=true` would be accepted: `preserve.allow_force_overwrite=true`, the permission, and a kept file that carries AutoAir's generated-file marker. `force` never replaces a kept file without that marker, such as an imported hand-written file that a Preserve deploy patched; change it with the Python output mode (Preserve) or keep it as raw code. The editor opens the conflict dialog with **Overwrite (force)** only when `force_allowed` is `true`, and otherwise shows the server's message. Promotions refuse such DAGs. Over MCP, `delete_dag_spec` and saves that would replace the file refuse such DAGs; MCP has no overwrite.

A hand-written file in the target that AutoAir never wrote, and that you did not import yourself, is not shown: the preview reports its state but no diff or content.

`deploy:overwrite_ejected` is held by **Admin** only and never comes from a wildcard
(`"*"`, `"deploy*"`); see [IAM](/docs/admin/iam). The `force` field of a deploy does not
overwrite a changed Python file (use `source_resolution`); for the other output modes and raw
code it needs `preserve.allow_force_overwrite=true` and that permission, and is ignored
otherwise.

A DAG with Preserve whose file was changed outside AutoAir is patched on top of the changed
file automatically, with the same merge and a warning.

## Code mode

For a DAG that keeps its hand-written file, the [code mode](/docs/user-guide/code-mode) and
the bottom panel's **Code** tab show that file with your canvas changes patched in (badge
**Hand-written file**), not generated code. What you apply there becomes the DAG's file.
When the latest canvas change cannot be applied to the file, editing is off and the reason is
shown; the deploy dialog offers the options above.

## Imports

**File → Import → From Python file…**, **From Airflow…** and the MCP tool `import_python_dag`
mark the imported DAG as hand-written (`source.origin: python`) and keep the file in the spec
(`source.code`), when the file defines exactly one DAG. In **Import a DAG repository**, the
**DAG files** select chooses between keeping the imported files and generating files from
the specs for the whole import.

A kept file is deployed as written, so it must work in the deploy target: a file that imports
helper modules the target does not have is refused (the round trip reports the
`ModuleNotFoundError`). On a local folder target the folder is on the import path, like in
Airflow; generated files resolve such values and need no helpers.

## Limits

- DAGs with [Blueprint](/docs/user-guide/blueprints) steps cannot keep a hand-written file yet: Preserve refuses them; deploy them with Generate.
- A deploy from the deploy dialog is bound to the file it previewed: when the file in the target changed in between, the deploy stops and asks for a new preview (`source_target_sha`).

- Environment overlays apply to the patched file like to a generated one, but a file changed
  outside AutoAir cannot be merged when the DAG has environments.
- Promoting a DAG that keeps its hand-written file between environments is refused (`409`):
  promotion regenerates the file on the target branch. Promote the file through the
  repository, or switch the DAG to Generate first.
- A four-eyes deploy proposal does not keep an answer to a stopped deploy; answer it when you
  deploy the approved proposal.
- The round trip uses AutoAir's Python importer. Values it cannot read (callbacks, objects)
  are not checked; they are also not changed.

## API and MCP

| Endpoint / tool | Fields |
|---|---|
| `PUT /api/specs/{dag_id}` | `source_resolution`: `preserve` or `overwrite`, the answer to a stopped deploy (`overwrite` needs `preserve.allow_force_overwrite=true`); `force`: overwrite for output modes other than Python and raw code; `source_target_sha`: the previewed `target_sha` (the deploy stops when the file changed). The result carries `source` (policy, action, diff, operations, warnings, refusals, options), `status` `ejected` or `source_refused` when it stopped (with `conflict.ejected_file`, `conflict.hand_written` and `conflict.force_allowed` when no source plan applies), and `stored_spec` when a merge changed the stored spec. |
| `POST /api/specs/{dag_id}/source-preview` | Body: `spec`, `environment`, `source_resolution`. Returns the same plan plus `content`, the complete file. |
| MCP `preview_dag_source`, `save_dag_spec` | The preview, and a save that follows the source policy (a dry run reports the plan; `expected_target_sha` binds it to the preview; the kept file is validated like a deploy). |

How it works inside: [Source-preserving deploys](https://github.com/ctolon/autoair/blob/main/docs/architecture/source-preserving-edits.md).
