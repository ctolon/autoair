# Security policy

## Supported versions

AutoAir is pre-1.0. Security fixes go to the latest minor release, and to `main` until the
next release.

| Version | Supported |
| --- | --- |
| 0.2.x | yes |
| < 0.2 | no |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub's private vulnerability reporting:

1. Go to https://github.com/ctolon/autoair/security/advisories/new
   (**Security → Advisories → Report a vulnerability**).
2. Describe the problem, the affected versions and deployment mode (Airflow plugin,
   `autoair serve`, `autoair ai-serve`, `autoair local`), and how to reproduce it. A
   proof of concept helps.
3. Say whether you want to be credited, and under which name.

What to expect:

- an acknowledgement within **3 working days**;
- an initial assessment (confirmed or not, severity) within **10 working days**;
- a fix, a GitHub Security Advisory and, when applicable, a CVE. Disclosure is coordinated
  with you. The default embargo is 90 days and is shortened once a fix is released.

## Scope

In scope: the `autoair` Python package, the Airflow plugin, the standalone, AI and MCP
servers, the editor bundles, the container image `ghcr.io/ctolon/autoair`, and the
deployment files in `deploy/`.

Examples of what we want to hear about:

- authentication or authorisation bypasses: tokens, SSO cookies, collaboration tickets, IAM
  roles, four-eyes approvals, direct-push rights, the AI service HMAC;
- code execution on the AutoAir server beyond what `code:execute` and the documented import
  checks allow;
- secrets leaking into generated DAGs, logs, `GET /api/config`, commits or the audit trail;
- injection into generated Python, YAML or git commands;
- SSRF through Airflow, git, marketplace, AI or MCP URLs;
- signature bypasses in marketplace indexes.

Out of scope:

- the Docker Compose **demo** (`deploy/compose/docker-compose.demo.yml`). It is insecure by
  design: all Airflow users are administrators and the secrets are public;
- vulnerabilities in Apache Airflow itself. Report those to the Apache Airflow project
  (https://airflow.apache.org/docs/apache-airflow/stable/security/);
- DAG code written by users. AutoAir executes it only where documented;
- missing hardening that the documentation already lists as a deployment responsibility
  (TLS, a real auth manager, network policy).

## Hardening

See the [security overview](https://ctolon.github.io/autoair/docs/security/overview) (threat
model and checklist) and [Standalone](https://ctolon.github.io/autoair/docs/installation/standalone)
on the documentation site, and the reference in [docs/iam.md](docs/iam.md) and
[docs/sso.md](docs/sso.md).
