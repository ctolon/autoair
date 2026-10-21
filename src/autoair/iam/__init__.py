"""AutoAir IAM: built-in roles and fine-grained permissions, governed through Airflow.

See ``docs/iam.md``. Routes depend on :func:`autoair.iam.deps.require`; the strategies in
:mod:`autoair.iam.resolver` turn Airflow identities into permissions.
"""

from autoair.iam.config import IamSettings
from autoair.iam.model import (
    ALL_PERMISSIONS,
    BUILTIN_ROLES,
    PERMISSIONS,
    Principal,
)

__all__ = ["ALL_PERMISSIONS", "BUILTIN_ROLES", "PERMISSIONS", "IamSettings", "Principal"]
