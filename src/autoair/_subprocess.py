"""Environment for child processes that execute user-authored DAG code.

Import checks, Python DAG imports and test runs execute code written in the builder (or
uploaded) on the AutoAir host. Those processes need Airflow's configuration, but never
AutoAir's own credentials (git tokens, LLM API keys, the collaboration HMAC key...), nor
Airflow's secrets (metadata DB URL, Fernet key, JWT secret, connections and variables from
the environment) or cloud credentials, which DAG authors must not be able to read from
``os.environ``.

Only the environment is scrubbed: values Airflow reads from ``airflow.cfg`` in
``AIRFLOW_HOME`` or from files the process can read are outside this module's reach (keep
secrets out of a config file the AutoAir process user can read, or use the isolated
validator service).
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from functools import lru_cache

from autoair.sections import SandboxSettings, ValidatorSettings, active


@lru_cache(maxsize=8)
def _secret_names(
    patterns: tuple[str, ...], exact: frozenset[str]
) -> tuple[tuple[re.Pattern[str], ...], frozenset[str]]:
    return tuple(re.compile(p, re.I) for p in patterns if p), exact


# A URL with a password in its userinfo (redis://:pw@host, https://user:pw@index/simple),
# whatever the variable is called.
_URL_PASSWORD = re.compile(r"://[^/\s:@]*:[^/\s@]+@")


@lru_cache(maxsize=8)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.I) for p in patterns if p)


def _validator_key_env() -> str:
    return f"AIRFLOW_CONN_{active(ValidatorSettings).secret_conn_id.upper()}"


def is_secret_name(name: str) -> bool:
    """``AUTOAIR_*`` settings holding secrets (nested ones included, e.g.
    ``AUTOAIR_STORAGE__GIT__TOKEN``; ``AUTOAIR_SANDBOX__SECRET_ENV_PATTERN``), provider
    keys read by SDKs directly (``AUTOAIR_SANDBOX__SCRUB_ENV``) and the validator's HMAC
    key connection. Never passed through (:func:`is_credential_name` can be)."""
    cfg = active(SandboxSettings)
    patterns, exact = _secret_names(
        (cfg.secret_env_pattern, *cfg.secret_env_patterns_extra),
        frozenset(n.upper() for n in (*cfg.scrub_env, *cfg.scrub_env_extra)),
    )
    upper = name.upper()
    if upper == _validator_key_env():
        return True  # the validator's HMAC key (validator.secret_conn_id)
    return upper in exact or (
        upper.startswith("AUTOAIR_") and any(p.search(upper) for p in patterns)
    )


def is_credential_name(name: str, value: str = "") -> bool:
    """Airflow secrets and cloud credentials (``sandbox.credential_env_pattern``, or a
    ``value`` that is a URL with a password) not listed in ``sandbox.env_passthrough``."""
    cfg = active(SandboxSettings)
    upper = name.upper()
    if not any(
        p.search(upper)
        for p in _compiled((cfg.credential_env_pattern, *cfg.credential_env_patterns_extra))
    ) and not (cfg.credential_env_pattern and _URL_PASSWORD.search(value)):
        return False
    return not any(fnmatch.fnmatchcase(upper, p.upper()) for p in cfg.env_passthrough)


def user_code_env(
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
    *,
    keep: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """``base`` (default ``os.environ``) without AutoAir secrets, Airflow secrets and cloud
    credentials, plus ``extra``. ``keep``: names (upper case) of credentials this caller
    deliberately hands to the child (never AutoAir's own secrets)."""
    env = {
        k: v
        for k, v in (os.environ if base is None else base).items()
        if not is_secret_name(k) and (k.upper() in keep or not is_credential_name(k, v))
    }
    env.update(extra or {})
    return env


__all__ = ["is_credential_name", "is_secret_name", "user_code_env"]
