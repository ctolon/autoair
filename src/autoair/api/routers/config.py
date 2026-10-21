"""``GET /api/config``: the effective configuration (admins only, ``AUTOAIR_EXPOSE_CONFIG``).

Like Airflow's ``[api] expose_config``: ``false`` (default) refuses, ``true`` returns every
value with secrets masked as ``***``, ``non-sensitive-only`` leaves secrets out.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep
from autoair.config_reference import MASK, iter_options, masked, value_at
from autoair.config_sources import configured_files

router = APIRouter(tags=["config"])


class ConfigOption(BaseModel):
    key: str = Field(description="Dotted key (configuration files), e.g. storage.git.mode.")
    section: str = Field(description="Dotted section ('' = top level).")
    env: str = Field(description="Environment variable, e.g. AUTOAIR_STORAGE__GIT__MODE.")
    type: str
    value: Any = Field(default=None, description="Effective value (secrets: ***).")
    default: Any = Field(default=None, description="Default value (null when computed).")
    default_text: str = Field(description="Human-readable default.")
    description: str
    source: str = Field(
        description="Where the value comes from: default, env, dotenv, secrets, init, "
        "file:<path> or ui (deploy target chosen in the UI)."
    )
    secret: bool


class ConfigInfo(BaseModel):
    expose: Literal["true", "non-sensitive-only"]
    files: list[str] = Field(description="Configuration files in effect, lowest precedence first.")
    options: list[ConfigOption]


def _source(services: Any, key: str) -> str:
    if key.startswith("storage.") and services.storage_source == "ui":
        return "ui"  # chosen in the deploy-target dialog (<state dir>/deploy-target.json)
    origin: str = services.settings.origin_of(key)
    return origin


@router.get("/config", operation_id="getConfig")
def get_config(services: ServicesDep) -> ConfigInfo:
    """Every effective setting with its source, type, default and description."""
    settings = services.settings
    expose = settings.expose_config
    if expose == "false":
        raise HTTPException(
            status_code=403,
            detail="Your AutoAir administrator chose not to expose the configuration "
            "(AUTOAIR_EXPOSE_CONFIG).",
        )
    options: list[ConfigOption] = []
    for opt in iter_options(type(settings)):
        if opt.secret and expose == "non-sensitive-only":
            continue
        value = value_at(settings, opt.key)
        if opt.secret:
            shown: Any = MASK if value not in (None, "", {}, []) else None
        else:
            shown = masked(value)
        options.append(
            ConfigOption(
                key=opt.key,
                section=opt.section,
                env=opt.env,
                type=opt.type,
                value=shown,
                default=None if opt.secret else opt.default,
                default_text=opt.default_text,
                description=opt.description,
                source=_source(services, opt.key),
                secret=opt.secret,
            )
        )
    return ConfigInfo(expose=expose, files=configured_files(), options=options)
