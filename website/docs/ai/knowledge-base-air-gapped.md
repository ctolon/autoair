---
title: Knowledge base without internet access
description: Feed the documentation knowledge base on an air-gapped installation from a corporate git mirror, object storage, a local folder, an internal HTTP mirror or a signed bundle built on a connected machine.
sidebar_label: Air-gapped setup
---

With `kb.online=false` (the default) the `official` source is skipped and nothing is fetched
from the internet. Configure one of the sources below instead; they are read whatever
`kb.online` says. Credentials always come from an **Airflow connection** named by `conn_id`:
AutoAir reads it when the source is used and never stores it, and URLs with credentials in them
are rejected when the settings load.

| You have | Use |
|---|---|
| A mirror of the apache/airflow repository on your git server | [Git](#git-mirror-of-apacheairflow) |
| Documentation files on S3, GCS or Azure | [Object storage](#object-storage) |
| A folder on the server (a checkout, a downloaded docs site, your own Markdown) | [Local folder](#local-folder) |
| A web server mirroring airflow.apache.org | [Internal HTTP mirror](#internal-http-mirror) |
| Nothing but a way to copy a file in | [Bundle](#bundle-workflow) |

Pages keep the version of what you index; with a repository checkout the versions come from the
checkout itself, so check out the tag that matches your Airflow (`3.1.0`, or
`providers-amazon/9.12.0` style tags for providers).

## Git mirror of apache/airflow

1. Create an Airflow connection with read access to the repository, the same format as
   `storage.git.conn_id`: login = user name (default `x-access-token`), password = token, or
   `extra.private_key` / `extra.key_file` for SSH; the host may hold the repository URL.

   ```bash
   airflow connections add kb_git --conn-type generic \
     --conn-host https://git.example.corp/mirrors/airflow.git \
     --conn-login svc-autoair --conn-password "$TOKEN"
   ```

2. Add the source:

   ```bash
   AUTOAIR_KB__SOURCES='[{"id": "airflow-git", "kind": "git", "conn_id": "kb_git",
     "ref": "3.1.0", "layout": "airflow_repo"}]'
   ```

3. Refresh: `autoair kb refresh --source airflow-git`, or the admin panel.

AutoAir makes a shallow checkout (`git fetch --depth 1 origin <ref>`) under
`<kb cache>/git/<source id>/worktree` (`kb.cache_dir`, default `<cache_dir or state>/kb`). With
`layout: airflow_repo` only the documentation folders, project files, provider metadata, system
tests and example DAGs are checked out (sparse checkout), so `exampleinclude` directives resolve
to the real example code. The token reaches git through `GIT_CONFIG_*` environment variables,
never the command line, the remote URL or `.git/config`; errors are redacted. TLS and proxy
settings of `outbound.*` (client `git`) apply as for git deploy targets. The commit SHA is the
fingerprint: when the ref did not move, the next refresh reads nothing.

Any other repository works too, for example your own documentation with `layout: files` and a
`public_base_url` for the citation links.

## Object storage

Point a `path` source at the folder and give it the connection of that storage:

```bash
AUTOAIR_KB__SOURCES='[{"id": "docs-s3", "kind": "path", "url": "s3://docs-mirror/airflow-site/",
  "conn_id": "aws_docs", "layout": "sphinx_site"}]'
```

With `conn_id` the files are read through Airflow's `ObjectStoragePath` for that connection, so
the provider of the scheme must be installed (`apache-airflow-providers-amazon` for `s3://`,
`-google` for `gs://`, `-microsoft-azure` for `abfs://`). Without `conn_id` AutoAir uses fsspec
directly with the ambient credentials (instance role, workload identity, environment) and
`storage_options` for non-secret options such as `{"endpoint_url": "https://minio.corp:9000"}`.
Do not put keys in `storage_options`.

A downloaded copy of the docs site (`sphinx_site`) only needs the packages and versions you
want, laid out as on the site: `apache-airflow/3.1.0/searchindex.js` plus its pages,
`apache-airflow-providers-amazon/9.12.0/...` and so on. `kb.versions` and `kb.providers` select
from it exactly as online.

## Local folder

```bash
# a checkout of apache/airflow
AUTOAIR_KB__SOURCES='[{"id": "airflow-src", "kind": "path", "url": "/srv/airflow", "layout": "airflow_repo"}]'

# your own runbooks, cited as https://wiki.corp/airflow/<path>
AUTOAIR_KB__SOURCES='[{"id": "runbooks", "kind": "path", "url": "/srv/runbooks", "layout": "files",
  "component": "runbooks", "versions": ["2026"], "public_base_url": "https://wiki.corp/airflow"}]'
```

Folder sources have no fingerprint: every refresh reads the files, but only pages whose content
hash changed are re-indexed, and deleted files are removed.

## Internal HTTP mirror

A web server with the same layout as `https://airflow.apache.org/docs/` (for example a
`wget --mirror` of the packages you use):

```bash
AUTOAIR_KB__SOURCES='[{"id": "mirror", "kind": "site", "url": "https://docs-mirror.corp/docs/",
  "allow_private": true}]'
```

The mirror's own host is added to the allow-list for this source. `allow_private: true` lets it
resolve to a private address (an internal server); the source may then reach **only its own host**
(the public allow-list `kb.fetch.allowed_domains` does not apply to it). Leave it off for a public
mirror. Citation
links point to the mirror unless `public_base_url` says otherwise. An `llms_txt` source does the
same for Markdown pages listed in an `llms.txt` (for example a mirror of Astronomer's guides,
with `"components": ["astronomer_airflow"]`).

## Bundle workflow

A bundle is a zip with the parsed pages (`pages.jsonl`) and a `manifest.json` holding the units,
page counts and the SHA-256 and size of `pages.jsonl`, optionally signed with Ed25519 (the same
scheme as the template marketplace). It carries no chunks or vectors: the importer chunks and
embeds with its own settings and model.

**On a connected machine:**

```bash
autoair kb keygen --out kb-bundle.pem          # once; prints the public key
AUTOAIR_KB__VERSIONS='["3.1.0"]' \
autoair kb build --online --sign-key kb-bundle.pem --out airflow-docs-3.1.0.zip
```

`build` refreshes a working index at `--store-path` (default `<kb cache>/build.sqlite3`; kept,
so the next build only fetches what changed) with embeddings off, then writes the bundle.
`--source` limits it to some sources, `--description` is stored in the manifest. Configure the
same components, providers and versions as the target installation: with `kb.providers=installed`
the build machine's installed providers decide, so use `kb.provider_include` or `providers=all`
when the two environments differ.

**On the air-gapped installation:**

```bash
AUTOAIR_KB__BUNDLE__PUBLIC_KEYS='["<base64 public key from keygen>"]'
autoair kb import airflow-docs-3.1.0.zip
```

Import always checks the zip members' sizes and the checksum of `pages.jsonl` in a first pass,
before any page is written, so a corrupted or tampered bundle imports nothing. When
`kb.bundle.public_keys` is set (or `kb.bundle.require_signature` is true) the manifest must carry
a signature that verifies against one of the keys. Pages are stored under the source id
`bundle` (`--source-id` to change it); pages of that source that are not in the bundle are
removed, so importing a newer bundle replaces the older one. Limits: `kb.bundle.max_bytes`
(4 GiB) and `kb.bundle.max_pages` (200000).

The admin panel imports a bundle too (`POST /api/kb/import`), bounded by `server.max_body_bytes`
(16 MiB by default); use the CLI for larger bundles. It also exports the current index as an
unsigned bundle (`GET /api/kb/export`).

**Automatic imports.** A `bundle` source imports a bundle from a path or object storage on
every refresh, and skips it while the manifest is unchanged:

```bash
AUTOAIR_KB__SOURCES='[{"id": "docs-bundle", "kind": "bundle",
  "url": "s3://transfer/kb/airflow-docs.zip", "conn_id": "aws_transfer"}]'
```

Drop a new bundle at that URL and the next scheduled refresh picks it up. Use signed bundles
here: set `kb.bundle.public_keys`.
