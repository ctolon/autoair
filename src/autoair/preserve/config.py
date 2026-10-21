"""Settings of source-preserving deploys (``AUTOAIR_PRESERVE__*``).

The two switches admins flip at runtime are feature flags (``features.source_preserve``,
``features.strict_source_preserve``, overridable in the admin panel); the values here are
configuration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from autoair.sections import _Section

ConcretePolicy = Literal["generate", "preserve", "strict-preserve"]
_ALL: tuple[ConcretePolicy, ...] = ("generate", "preserve", "strict-preserve")


class PreserveSettings(_Section):
    """Editing existing Python DAG files: source policies and ejected-file protection."""

    allowed_policies: list[ConcretePolicy] = Field(
        default_factory=lambda: list(_ALL),
        min_length=1,
        description="Source policies DAGs may use (JSON list in the environment). A DAG "
        "whose policy is not listed is refused at deploy; 'auto' resolves to the closest "
        "allowed one (preserve -> strict-preserve -> generate). Removing 'generate' means a "
        "hand-written file is never regenerated from its spec.",
    )
    ejected_files: Literal["block", "warn", "overwrite"] = Field(
        default="block",
        description="A deploy that would regenerate a DAG file changed outside AutoAir "
        "(ejected: its fingerprint or recorded hash no longer matches, or a hand-written "
        "file without AutoAir's marker): block = stop and offer to apply the change to the "
        "file with preserve; warn = apply it with preserve automatically and report a "
        "warning (stop when that is not possible); overwrite = stop and offer preserve or "
        "an overwrite, which needs an explicit confirmation, the "
        "'deploy:overwrite_ejected' permission and preserve.allow_force_overwrite=true "
        "(without it overwrite acts like block). Files without the marker are never "
        "overwritten.",
    )
    allow_force_overwrite: bool = Field(
        default=False,
        description="Offer and accept the 'Overwrite (force)' choice for a DAG file "
        "AutoAir would otherwise not replace: the hand-written file it keeps for a DAG "
        "(source-preserving deploys, raw code takeovers), a file changed outside AutoAir "
        "(ejected; for Python files also preserve.ejected_files=overwrite) and forced "
        "deploys of the other output modes. The caller still needs "
        "'deploy:overwrite_ejected' and an explicit confirmation. false (default): the "
        "choice is hidden and refused; such files are changed only with preserve or kept "
        "as raw code.",
    )
    import_timeout: float = Field(
        default=90,
        gt=0,
        description="Seconds each import of the round-trip check may take (the existing "
        "file, the generated and the patched file are imported in a sandboxed subprocess).",
    )
    cache_ttl: float = Field(
        default=300,
        ge=0,
        description="Seconds a replica keeps the import of an unchanged file (keyed by its "
        "content hash) between a deploy preview and the deploy. 0: no cache.",
    )
    max_diff_chars: int = Field(
        default=200_000,
        ge=1_000,
        description="Largest unified diff returned by the source preview (longer diffs are "
        "cut and marked truncated).",
    )


__all__ = ["ConcretePolicy", "PreserveSettings"]
