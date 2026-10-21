"""Environment for child processes that execute user-authored DAG code.

Import checks, Python DAG imports and test runs execute code written in the builder (or
uploaded) on the AutoAir host. Those processes need Airflow's configuration, but never
AutoAir's own credentials (git tokens, LLM API keys, the collaboration HMAC key...),
which DAG authors must not be able to read from ``os.environ``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache

from autoair.sections import SandboxSettings, active


@lru_cache(maxsize=8)
def _secret_names(
    patterns: tuple[str, ...], exact: frozenset[str]
) -> tuple[tuple[re.Pattern[str], ...], frozenset[str]]:
    return tuple(re.compile(p, re.I) for p in patterns if p), exact


def is_secret_name(name: str) -> bool:
    """``AUTOAIR_*`` settings holding secrets (nested ones included, e.g.
    ``AUTOAIR_STORAGE__GIT__TOKEN``; ``AUTOAIR_SANDBOX__SECRET_ENV_PATTERN``) and provider
    keys read by SDKs directly (``AUTOAIR_SANDBOX__SCRUB_ENV``)."""
    cfg = active(SandboxSettings)
    patterns, exact = _secret_names(
        (cfg.secret_env_pattern, *cfg.secret_env_patterns_extra),
        frozenset(n.upper() for n in (*cfg.scrub_env, *cfg.scrub_env_extra)),
    )
    upper = name.upper()
    return upper in exact or (
        upper.startswith("AUTOAIR_") and any(p.search(upper) for p in patterns)
    )


def user_code_env(
    extra: Mapping[str, str] | None = None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``base`` (default ``os.environ``) without AutoAir secrets, plus ``extra``."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not is_secret_name(k)}
    env.update(extra or {})
    return env


__all__ = ["is_secret_name", "user_code_env"]
