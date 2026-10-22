# Policy rules and OPA

The policy guide lives on the documentation site: [Policy](https://ctolon.github.io/autoair/docs/admin/policy).

It covers the policy switches and pattern rules, secret detection, declarative rules
(`policy.rules`: rule fields, match selectors, checks), Open Policy Agent (`policy.opa_url`,
the decision input and result formats, `opa_fail_mode`) and every point where policy is
enforced. The settings are listed in the [`policy` configuration
reference](configuration-reference.md#policy).

In the code: `src/autoair/policy_rules.py` (rule models),
`src/autoair/validation/policy_rules.py` (rule checks), `src/autoair/validation/opa.py` (OPA
client) and `src/autoair/validation/__init__.py` (`_policy`, `_organisation_rules`).
