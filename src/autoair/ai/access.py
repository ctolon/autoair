"""Who is calling the AI endpoints, and with which AI role.

* **User**: an identity set by the auth layer on ``request.state`` (the IAM module may
  set ``autoair_user``), a verified identity forwarded by the AutoAir proxy to a separate
  AI service (HMAC-signed headers), or the subject of the Airflow JWT the request carries
  (already authenticated by the API's auth dependencies).
* **Role** (tool allow-lists, admin endpoints), from the AutoAir IAM principal
  (``request.state.autoair_principal``): ``admin`` with ``ai:admin``, ``editor`` with
  ``code:execute`` (tools that run DAG code), else ``viewer``. Without a principal (the
  separate AI service, bare routers in tests) the role comes from the signed proxy
  identity or the :func:`autoair.auth.permission_flag` flags of the router.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import Request

from autoair.ai.config import Role
from autoair.persistence.documents import DocumentStore

EDITOR_FLAG = "autoair_ai_editor"
ADMIN_FLAG = "autoair_ai_admin"
IDENTITY_HEADER = "x-autoair-ai-identity"
SIGNATURE_HEADER = "x-autoair-ai-signature"
MAX_IDENTITY_AGE = 300


def _user_label(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    for attr in ("username", "user_name", "name", "id", "user_id"):
        got = getattr(value, attr, None)
        if callable(got):
            got = got()
        if got:
            return str(got)
    get_id = getattr(value, "get_id", None)
    if callable(get_id):
        return str(get_id())
    return None


PRINCIPAL_ATTR = "autoair_principal"
ADMIN_PERMISSION = "ai:admin"
EDITOR_PERMISSION = "code:execute"


def request_user(request: Request) -> str | None:
    state = request.state
    principal = getattr(state, PRINCIPAL_ATTR, None)
    if principal is not None and getattr(principal, "user", None):
        return str(principal.user)
    for attr in ("autoair_ai_user", "autoair_user", "user"):
        label = _user_label(getattr(state, attr, None))
        if label:
            return label
    from autoair.audit import request_user as jwt_user

    return jwt_user(request)


def request_role(request: Request) -> Role:
    forwarded = getattr(request.state, "autoair_ai_role", None)
    if forwarded in ("viewer", "editor", "admin"):
        role: Role = forwarded
        return role
    principal = getattr(request.state, PRINCIPAL_ATTR, None)
    has = getattr(principal, "has", None)
    if callable(has):
        if has(ADMIN_PERMISSION):
            return "admin"
        return "editor" if has(EDITOR_PERMISSION) else "viewer"
    if getattr(request.state, ADMIN_FLAG, False):
        return "admin"
    if getattr(request.state, EDITOR_FLAG, False):
        return "editor"
    return "viewer"


def has_permission(request: Request, permission: str) -> bool:
    """Whether the caller holds an AutoAir ``permission``.

    With an IAM principal (main app, direct calls to the AI service) it decides. Without
    one (requests signed by the AutoAir proxy, bare routers in tests) only the AI role is
    known: ``ai:admin`` / ``admin:*`` need the admin role, ``code:execute`` / ``spec:*`` /
    ``deploy`` / ``run:*`` (changes to Airflow runs) the editor role, everything else is
    allowed (Airflow still enforces its own permissions on proxied calls)."""
    principal = getattr(request.state, PRINCIPAL_ATTR, None)
    has = getattr(principal, "has", None)
    if callable(has) and getattr(request.state, "autoair_ai_role", None) is None:
        return bool(has(permission))
    role = request_role(request)
    if permission == ADMIN_PERMISSION or permission.startswith("admin:"):
        return role == "admin"
    if permission in (EDITOR_PERMISSION, "deploy") or permission.startswith(("spec:", "run:")):
        return role in ("editor", "admin")
    return True


# --------------------------------------------------------------------------- signed identity


def _canonical(method: str, path: str, query: str, body: bytes) -> dict[str, str]:
    return {
        "method": method.upper(),
        "path": path,
        "query": query,
        "body": hashlib.sha256(body).hexdigest(),
    }


def sign_identity(
    secret: bytes,
    user: str | None,
    role: str,
    *,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
) -> dict[str, str]:
    """Signed identity headers for one request: user + role, bound to the request's
    method, path, query and body hash, with a single-use nonce and a timestamp."""
    claims = {
        "user": user,
        "role": role,
        "ts": int(time.time()),
        "nonce": secrets.token_urlsafe(16),
        **_canonical(method, path, query, body),
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return {IDENTITY_HEADER: payload, SIGNATURE_HEADER: signature}


class ReplayCache:
    """Nonces seen within :data:`MAX_IDENTITY_AGE` (a signed identity is single-use)."""

    def __init__(self, ttl: float = MAX_IDENTITY_AGE * 2, max_entries: int = 100_000) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, nonce: str, now: float | None = None) -> bool:
        """Remember ``nonce``; ``False`` when it was already used."""
        now = time.time() if now is None else now
        with self._lock:
            while self._seen:
                oldest, expires = next(iter(self._seen.items()))
                if expires > now:
                    break
                del self._seen[oldest]
            if nonce in self._seen or len(self._seen) >= self.max_entries:
                return False
            self._seen[nonce] = now + self.ttl
            return True


class SharedReplayCache(ReplayCache):
    """Atomic replay detection across replicas, with bounded, expiring hash buckets."""

    def __init__(self, store: DocumentStore, *, max_entries: int = 100_000) -> None:
        super().__init__(max_entries=max_entries)
        self.store = store

    def add(self, nonce: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        digest = hashlib.sha256(nonce.encode()).hexdigest()
        # Two hex digits partition the cache into 256 bounded documents. Entries are
        # retained for the entire signature validity window, including future clock skew.
        bucket = digest[:2]
        capacity = max(1, self.max_entries // 256)
        accepted = False

        def update(raw: bytes | None) -> bytes:
            nonlocal accepted
            entries = json.loads(raw) if raw else {}
            entries = {key: expiry for key, expiry in entries.items() if expiry > now}
            accepted = digest not in entries and len(entries) < capacity
            if accepted:
                entries[digest] = now + self.ttl
            return json.dumps(entries).encode()

        self.store.update(bucket, update)
        return accepted


_REPLAY = ReplayCache()


def verify_identity(
    secret: bytes,
    payload: str,
    signature: str,
    *,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    replay: ReplayCache | None = None,
) -> dict[str, Any] | None:
    """The identity claims, or ``None`` when the signature is wrong, expired, bound to
    another request or already used."""
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        ts = float(data.get("ts", 0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or abs(time.time() - ts) > MAX_IDENTITY_AGE:
        return None
    want = _canonical(method, path, query, body)
    signed_path = str(data.get("path", ""))
    # The proxy signs the upstream path; a path prefix of ``ai.service_url`` may be
    # stripped by a reverse proxy in front of the service.
    if not (signed_path == path or (path and signed_path.endswith(path))):
        return None
    if any(data.get(k) != want[k] for k in ("method", "query", "body")):
        return None
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return None
    if not (replay or _REPLAY).add(nonce):
        return None
    return data


__all__ = [
    "ADMIN_FLAG",
    "EDITOR_FLAG",
    "IDENTITY_HEADER",
    "MAX_IDENTITY_AGE",
    "SIGNATURE_HEADER",
    "ReplayCache",
    "has_permission",
    "request_role",
    "request_user",
    "sign_identity",
    "verify_identity",
]
