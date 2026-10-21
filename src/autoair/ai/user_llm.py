"""Per-user LLM connections (``ai.llm_mode`` = ``per_user`` or ``both``).

A user stores their own provider / model / endpoint and API key, or references an
Airflow connection the admin allow-listed. Keys are encrypted at rest with Fernet
(``cryptography``, a core dependency) using ``ai.user_llm.encryption_key`` or a key file
created in the state folder; they are never returned again (only "set" + last four
characters) and never logged.

Admin restrictions (``ai.user_llm.*``): allowed providers, model patterns, endpoint
prefixes (with SSRF checks: endpoints resolving to private/loopback addresses are
refused unless ``allow_private_networks``) and connection-id patterns.
"""

from __future__ import annotations

import contextlib
import datetime
import fnmatch
import hashlib
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, SecretStr

from autoair.ai.config import AiSettings, ProviderLiteral, UserLlmSettings
from autoair.ai.net import UrlNotAllowed, check_url, prefix_matches
from autoair.persistence.documents import DocumentStore, FileDocumentStore

log = logging.getLogger("autoair.ai.user_llm")


class UserLlmError(ValueError):
    pass


class _Stored(BaseModel):
    provider: ProviderLiteral | None = None
    model: str | None = None
    base_url: str | None = None
    conn_id: str | None = None
    api_key_enc: str | None = None
    api_key_last4: str | None = None
    updated_at: str


class UserLlmView(BaseModel):
    """What the API returns (never the key)."""

    configured: bool = False
    provider: ProviderLiteral | None = None
    model: str | None = None
    base_url: str | None = None
    conn_id: str | None = None
    api_key_set: bool = False
    api_key_last4: str | None = None
    updated_at: str | None = None


class UserLlmUpdate(BaseModel):
    provider: ProviderLiteral | None = None
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    conn_id: str | None = Field(default=None, max_length=250)
    api_key: SecretStr | None = Field(
        default=None, description="New API key; omit to keep the stored one."
    )
    clear_api_key: bool = False


class UserLlmPolicy(BaseModel):
    """What the UI may offer (from ``ai.user_llm``)."""

    mode: Literal["central", "per_user", "both"]
    providers: list[str]
    models: list[str]
    base_urls: list[str]
    conn_ids_allowed: bool


def policy_view(ai: AiSettings) -> UserLlmPolicy:
    u = ai.user_llm
    return UserLlmPolicy(
        mode=ai.llm_mode,
        providers=list(u.allowed_providers),
        models=list(u.allowed_models),
        base_urls=list(u.allowed_base_urls),
        conn_ids_allowed=bool(u.allowed_conn_ids),
    )


def load_encryption_key(policy: UserLlmSettings, state_dir: Path) -> bytes:
    if policy.encryption_key is not None:
        key = policy.encryption_key.get_secret_value().encode()
        Fernet(key)  # validates the format
        return key
    path = state_dir / "ai-user-llm.key"
    if path.is_file():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    log.warning(
        "created %s to encrypt user API keys; set AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY "
        "for multi-replica deployments",
        path,
    )
    return key


async def validate_update(update: UserLlmUpdate, policy: UserLlmSettings) -> None:
    if update.provider is not None and update.provider not in policy.allowed_providers:
        raise UserLlmError(f"provider {update.provider!r} is not allowed here")
    if update.model and not any(
        fnmatch.fnmatchcase(update.model, p) for p in policy.allowed_models
    ):
        raise UserLlmError(f"model {update.model!r} is not allowed here")
    if update.conn_id and not any(
        fnmatch.fnmatchcase(update.conn_id, p) for p in policy.allowed_conn_ids
    ):
        raise UserLlmError(f"connection {update.conn_id!r} may not be used for AI")
    if update.base_url:
        await check_base_url(update.base_url, policy)
    if not update.provider and not update.conn_id:
        raise UserLlmError("choose a provider or an Airflow connection")


async def check_base_url(url: str, policy: UserLlmSettings) -> None:
    if not policy.allowed_base_urls:
        raise UserLlmError("custom endpoints are not allowed here (provider defaults only)")
    if not prefix_matches(url.rstrip("/"), policy.allowed_base_urls):
        raise UserLlmError("this endpoint is not in the allowed list")
    try:
        await check_url(url, allow_private=policy.allow_private_networks)
    except UrlNotAllowed as exc:
        raise UserLlmError(f"endpoint refused: {exc}") from exc


class UserLlmStore:
    """One encrypted JSON document per user (file name = hash of the user id)."""

    def __init__(self, url: str, key: bytes, *, documents: DocumentStore | None = None) -> None:
        # Files <url>/<sha256(user)>.json by default; rows of the SQL database when one
        # is configured. The key is encrypted either way.
        self.documents: DocumentStore = documents or FileDocumentStore(url)
        self._fernet = Fernet(key)
        self._lock = threading.Lock()

    @staticmethod
    def _key(user: str) -> str:
        return hashlib.sha256(user.encode()).hexdigest()

    def get(self, user: str) -> _Stored | None:
        try:
            found = self.documents.get(self._key(user))
            return _Stored.model_validate_json(found.data) if found is not None else None
        except (OSError, ValueError):
            return None

    def view(self, user: str) -> UserLlmView:
        stored = self.get(user)
        if stored is None:
            return UserLlmView()
        return UserLlmView(
            configured=True,
            provider=stored.provider,
            model=stored.model,
            base_url=stored.base_url,
            conn_id=stored.conn_id,
            api_key_set=stored.api_key_enc is not None,
            api_key_last4=stored.api_key_last4,
            updated_at=stored.updated_at,
        )

    def preview(self, user: str, update: UserLlmUpdate) -> _Stored:
        """The record :meth:`put` would store for ``update`` (merged with the stored key
        unless a new key or ``clear_api_key`` is given), without writing it."""
        current = self.get(user)
        enc = current.api_key_enc if current else None
        last4 = current.api_key_last4 if current else None
        if update.clear_api_key:
            enc = last4 = None
        if update.api_key is not None and update.api_key.get_secret_value():
            raw = update.api_key.get_secret_value()
            enc = self._fernet.encrypt(raw.encode()).decode()
            last4 = raw[-4:] if len(raw) >= 8 else None
        return _Stored(
            provider=update.provider,
            model=update.model or None,
            base_url=(update.base_url or "").rstrip("/") or None,
            conn_id=update.conn_id or None,
            api_key_enc=enc,
            api_key_last4=last4,
            updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def put(self, user: str, update: UserLlmUpdate) -> UserLlmView:
        with self._lock:
            stored = self.preview(user, update)
            self.documents.put(self._key(user), stored.model_dump_json().encode())
            if isinstance(self.documents, FileDocumentStore) and "file" in tuple(
                getattr(self.documents.fs, "protocol", ()) or ()
            ):
                with contextlib.suppress(OSError):
                    os.chmod(self.documents.path(self._key(user)), 0o600)
        return self.view(user)

    def delete(self, user: str) -> bool:
        with self._lock:
            try:
                return self.documents.delete(self._key(user))
            except OSError:
                return False

    def api_key(self, stored: _Stored) -> str | None:
        if stored.api_key_enc is None:
            return None
        try:
            return self._fernet.decrypt(stored.api_key_enc.encode()).decode()
        except InvalidToken as exc:
            raise UserLlmError(
                "your stored API key cannot be decrypted (the server key changed); set it again"
            ) from exc


_LOCK = threading.Lock()
_STORES: dict[str, UserLlmStore] = {}


def encryption_key_for(services: Any) -> bytes:
    """The Fernet key protecting stored secrets (user API keys, MCP server headers):
    ``ai.user_llm.encryption_key``, else one key created once in the SQL database (shared
    by every replica), else a key file in the state folder."""
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import is_sql

    settings = services.settings
    ai = ai_settings_of(settings)
    if is_sql(settings) and ai.user_llm.encryption_key is None:
        from autoair.persistence.db import database_for

        db = database_for(settings)
        assert db is not None
        return db.shared_secret("ai-user-llm-key", Fernet.generate_key)
    return load_encryption_key(ai.user_llm, Path(settings.state_dir))


def store_for(services: Any) -> UserLlmStore:
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import document_store, is_sql

    settings = services.settings
    ai = ai_settings_of(settings)
    state = Path(settings.state_dir)
    url = ai.user_llm.store_url or str(state / "ai-users")
    sql = is_sql(settings)
    cache_key = f"sql:{url}" if sql else url
    with _LOCK:
        store = _STORES.get(cache_key)
        if store is None:
            # One key for every replica (a per-host key file would make another replica
            # unable to decrypt): with SQL it is created once in the database.
            key = encryption_key_for(services)
            store = _STORES[cache_key] = UserLlmStore(
                url, key, documents=document_store(settings, "ai_user_llm", file_url=url)
            )
        return store


async def close_llm(llm: Any) -> None:
    """Close the HTTP clients behind ``llm``; errors are logged, never raised."""
    aclose = getattr(llm, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception as exc:  # closing must never break the caller
            log.debug("closing %r failed: %s", llm, exc)


class UserLlmCache:
    """LRU of per-user LLM clients keyed by ``(user, version)``.

    Evicted clients - least recently used beyond ``max_entries``, or an older version of
    the same user's settings - are closed so their HTTP connection pools are released.

    * ``get(key)`` -> the client or ``None`` (marks it recently used);
    * ``await put(key, llm)`` stores it and closes what it displaces;
    * ``await aclose()`` closes everything (application shutdown).
    """

    def __init__(self, max_entries: int = 512) -> None:
        self.max_entries = max(1, max_entries)
        self._items: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def get(self, key: tuple[str, str]) -> Any | None:
        with self._lock:
            llm = self._items.get(key)
            if llm is not None:
                self._items.move_to_end(key)
            return llm

    async def put(self, key: tuple[str, str], llm: Any) -> None:
        evicted: list[Any] = []
        with self._lock:
            for other in [k for k in self._items if k[0] == key[0] and k != key]:
                evicted.append(self._items.pop(other))
            previous = self._items.pop(key, None)
            if previous is not None and previous is not llm:
                evicted.append(previous)
            self._items[key] = llm
            while len(self._items) > self.max_entries:
                evicted.append(self._items.popitem(last=False)[1])
        for old in evicted:
            await close_llm(old)

    async def aclose(self) -> None:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for llm in items:
            await close_llm(llm)


async def build_user_llm(stored: _Stored, store: UserLlmStore, ai: AiSettings) -> Any:
    """The user's LLM client (endpoint re-checked at use time: DNS may have changed)."""
    from autoair.ai.llm import LLM, ModelConfig, llm_from_config

    policy = ai.user_llm
    if stored.provider and stored.provider not in policy.allowed_providers:
        raise UserLlmError("your provider is no longer allowed; update your AI model settings")
    if stored.model and not any(
        fnmatch.fnmatchcase(stored.model, p) for p in policy.allowed_models
    ):
        raise UserLlmError("your model is no longer allowed; update your AI model settings")
    if stored.conn_id:
        if not any(fnmatch.fnmatchcase(stored.conn_id, p) for p in policy.allowed_conn_ids):
            raise UserLlmError("your connection may no longer be used for AI")
        return LLM(
            conn_id=stored.conn_id,
            model_name=stored.model,
            defaults=ModelConfig(timeout=ai.timeout_seconds, max_retries=ai.max_retries),
            max_output_tokens=ai.max_output_tokens,
            pricing=ai.pricing,
        )
    if stored.base_url:
        await check_base_url(stored.base_url, policy)
    key = store.api_key(stored)
    if (
        key is None
        and stored.provider in ("anthropic", "gemini", "openai", "azure")
        and not stored.base_url
    ):
        raise UserLlmError("your AI model needs an API key")
    config = ModelConfig(
        provider=stored.provider or "anthropic",
        model=stored.model,
        api_key=SecretStr(key) if key else None,
        base_url=stored.base_url,
        timeout=ai.timeout_seconds,
        max_retries=ai.max_retries,
        # The client is cached: the one-off check above cannot catch DNS changes later,
        # so every request re-resolves the endpoint and connects to the validated IP.
        pin_public_ip=bool(stored.base_url) and not policy.allow_private_networks,
    )
    return llm_from_config(config, ai)


__all__ = [
    "UserLlmCache",
    "UserLlmError",
    "UserLlmPolicy",
    "UserLlmStore",
    "UserLlmUpdate",
    "UserLlmView",
    "build_user_llm",
    "check_base_url",
    "close_llm",
    "load_encryption_key",
    "policy_view",
    "store_for",
    "validate_update",
]
