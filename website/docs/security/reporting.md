---
title: Security and vulnerability reporting
description: How to report a vulnerability in AutoAir privately, what to include, the response timeline, supported versions and what is in scope.
sidebar_label: Vulnerability reporting
---

The authoritative policy is the repository's [SECURITY.md](https://github.com/ctolon/autoair/blob/main/SECURITY.md); the steps below follow it.

## Report privately

**Do not open a public issue, discussion or pull request for a security problem.** Report it through GitHub's private vulnerability reporting:

1. Open [github.com/ctolon/autoair/security/advisories/new](https://github.com/ctolon/autoair/security/advisories/new) (**Security** > **Advisories** > **Report a vulnerability**).
2. Describe the problem, the affected versions and the deployment mode (Airflow plugin, `autoair serve`, `autoair ai-serve`, `autoair local`), and how to reproduce it. A proof of concept helps.
3. Say whether you want to be credited, and under which name.

Useful details: the AutoAir and Airflow versions (`autoair --version`), the auth manager, the affected endpoint or feature, the permissions you expected and the behaviour you observed. Redact tokens, connection passwords and private DAG code from logs and configuration exports before attaching them.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement | within 3 working days |
| Initial assessment (confirmed or not, severity) | within 10 working days |
| Fix | a fix, a GitHub Security Advisory and, when applicable, a CVE |

Disclosure is coordinated with the reporter. The default embargo is 90 days and is shortened once a fix is released.

## Supported versions

AutoAir is pre-1.0. Security fixes go to the latest minor release, and to `main` until the next release.

| Version | Supported |
|---|---|
| 0.1.x | yes |
| < 0.1 | no |

## Scope

In scope: the `autoair` Python package, the Airflow plugin, the standalone, AI and MCP servers, the editor bundles, the container image `ghcr.io/ctolon/autoair`, and the deployment files in `deploy/`.

Examples of reports the project wants:

- authentication or authorisation bypasses: tokens, SSO cookies, collaboration tickets, IAM roles, four-eyes approvals, direct-push rights, the AI service HMAC;
- code execution on the AutoAir server beyond what `code:execute` and the documented import checks allow;
- secrets leaking into generated DAGs, logs, `GET /api/config`, commits or the audit trail;
- injection into generated Python, YAML or git commands;
- SSRF through Airflow, git, marketplace, AI or MCP URLs;
- signature bypasses in marketplace indexes.

Out of scope:

- the Docker Compose demo (`deploy/compose/docker-compose.demo.yml`). It is insecure by design: all Airflow users are administrators and its secrets are public;
- vulnerabilities in Apache Airflow itself; report those to the [Apache Airflow project](https://airflow.apache.org/docs/apache-airflow/stable/security/);
- DAG code written by users. AutoAir executes it only where documented;
- missing hardening that the documentation already lists as a deployment responsibility (TLS, a real auth manager, network policy).

## Deployment boundaries

DAG authors can submit code that will eventually execute in Airflow workers. Restrict Python, shell, deployment and task-test permissions accordingly. A validation subprocess is not a complete sandbox for hostile code; use isolated execution with network and resource restrictions when accepting untrusted authors.

LLM output and retrieved documents are untrusted inputs. Keep tool access scoped, require human approval for changes, and retain audit records. Configurable guardrails reduce risk but do not establish an execution boundary.

## Hardening

Review the [security model](/docs/security/overview), [IAM](/docs/admin/iam), [SSO](/docs/admin/sso), [TLS and headers](/docs/admin/tls-and-headers), [AI architecture](/docs/ai/overview) and [configuration](/docs/admin/configuration) before exposing the server.
