"""Import checks through the validator service (``validator.url``).

The main server (plugin or standalone) POSTs the DAG source to ``<url>/v1/check`` of
``autoair validator-serve``, signed with the HMAC scheme of the AI service
(:func:`autoair.ai.access.sign_identity`: method, path and body hash, timestamp, single-use
nonce). The service signs its answer with the same key, bound to that nonce
(:data:`RESPONSE_SIGNATURE_HEADER`); an unsigned or mismatched answer is not trusted. The
key is the password of an Airflow connection (``validator.secret_conn_id``): AutoAir stores
no secret of its own.

* connection errors and HTTP 429/502/503/504 are retried (``retries``, exponential
  ``retry_backoff_seconds``); a request that timed out is not;
* after a failed request the service counts as down in this process: checks fail at once
  and ``/ready`` is probed at most every ``health_check_interval_seconds`` (derived state
  with a deadline; every replica keeps its own);
* failures raise :class:`ValidatorUnavailable`; ``validator.fail_mode`` decides what the
  caller makes of it (:func:`autoair.validation.dagbag_check.check_source`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx2

from autoair.net import tls
from autoair.sections import ValidatorSettings, active
from autoair.validation.dagbag_check import ImportCheckResult

log = logging.getLogger(__name__)

IDENTITY_HEADER = "x-autoair-validator-identity"
SIGNATURE_HEADER = "x-autoair-validator-signature"
# The service's HMAC over its answer, bound to the request's nonce: a host between the main
# server and the validator cannot hand back a forged "imports fine" result.
RESPONSE_SIGNATURE_HEADER = "x-autoair-validator-response-signature"
CHECK_PATH = "/v1/check"
SIGNED_ROLE = "validator"
# Largest validator answer read (dag/task ids and import error tracebacks).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_RETRY_STATUS = frozenset({429, 502, 503, 504})
# Shortest HMAC key accepted (security floor).
MIN_KEY_BYTES = 16


class ValidatorUnavailable(Exception):
    """The validator could not be asked; the message says why (never the key)."""


class KeyUnavailable(ValidatorUnavailable):
    """The HMAC key could not be read from its Airflow connection."""


# --------------------------------------------------------------------------- the HMAC key


def connection_env_name(conn_id: str) -> str:
    return f"AIRFLOW_CONN_{conn_id.upper()}"


def password_from_env(conn_id: str, environ: Any = None) -> str | None:
    """The password of connection ``conn_id`` from ``AIRFLOW_CONN_<CONN_ID>`` (Airflow's
    environment variable backend: JSON or URI form), without importing Airflow."""
    raw = (os.environ if environ is None else environ).get(connection_env_name(conn_id))
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        value = data.get("password") if isinstance(data, dict) else None
        return str(value) if value else None
    try:
        password = urlsplit(raw).password
    except ValueError:
        return None
    return unquote(password) if password else None


def password_from_airflow(conn_id: str) -> str | None:
    """The password of an Airflow connection (secrets backends, then the Task SDK), or the
    environment variable when Airflow is not installed."""
    loaders: list[Any] = []
    try:
        from airflow.models.connection import Connection

        loaders.append(Connection.get_connection_from_secrets)
    except ImportError:
        pass
    try:
        from airflow.sdk import BaseHook

        loaders.append(BaseHook.get_connection)
    except ImportError:
        pass
    for load in loaders:
        try:
            conn = load(conn_id)
        except Exception:  # Airflow raises its own not-found / configuration errors
            log.debug("connection lookup of %r failed", conn_id, exc_info=True)
            continue
        password = getattr(conn, "password", None)
        if password:
            return str(password)
    return password_from_env(conn_id)


@dataclass
class _CachedKey:
    value: bytes
    expires: float


class KeyCache:
    """The key per connection id, read again after ``ttl`` seconds (rotation)."""

    def __init__(self, *, env_only: bool = False) -> None:
        self.env_only = env_only
        self._keys: dict[str, _CachedKey] = {}
        self._lock = threading.Lock()

    def get(self, conn_id: str, ttl: float) -> bytes:
        now = time.monotonic()
        with self._lock:
            hit = self._keys.get(conn_id)
            if hit is not None and hit.expires > now:
                return hit.value
        password = password_from_env(conn_id) if self.env_only else password_from_airflow(conn_id)
        if not password:
            where = (
                f"environment variable {connection_env_name(conn_id)}"
                if self.env_only
                else f"Airflow connection {conn_id!r}"
            )
            raise KeyUnavailable(
                f"no HMAC key: the {where} has no password (validator.secret_conn_id)"
            )
        value = password.encode()
        if len(value) < MIN_KEY_BYTES:
            raise KeyUnavailable(
                f"the HMAC key of validator.secret_conn_id is shorter than {MIN_KEY_BYTES} bytes"
            )
        with self._lock:
            self._keys[conn_id] = _CachedKey(value, now + ttl)
        return value

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()


_KEYS = KeyCache()


def sign(key: bytes, *, method: str, path: str, body: bytes) -> dict[str, str]:
    """Signed headers for one validator request."""
    from autoair.ai.access import IDENTITY_HEADER as AI_IDENTITY
    from autoair.ai.access import SIGNATURE_HEADER as AI_SIGNATURE
    from autoair.ai.access import sign_identity

    signed = sign_identity(key, None, SIGNED_ROLE, method=method, path=path, body=body)
    return {IDENTITY_HEADER: signed[AI_IDENTITY], SIGNATURE_HEADER: signed[AI_SIGNATURE]}


def request_nonce(identity: str) -> str | None:
    """The nonce of a signed identity header (not verified here), or ``None``."""
    try:
        data = json.loads(base64.urlsafe_b64decode(identity.encode()))
    except ValueError:
        return None
    nonce = data.get("nonce") if isinstance(data, dict) else None
    return nonce if isinstance(nonce, str) and nonce else None


def response_signature(key: bytes, nonce: str, status: int, body: bytes) -> str:
    """HMAC-SHA256 of an answer: the request's nonce, the HTTP status and the body hash."""
    message = "\n".join(
        ("autoair-validator-response", nonce, str(status), hashlib.sha256(body).hexdigest())
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def verify_response(key: bytes, nonce: str, status: int, body: bytes, signature: str) -> bool:
    return bool(signature) and hmac.compare_digest(
        response_signature(key, nonce, status, body), signature
    )


# --------------------------------------------------------------------------- health state


@dataclass
class _Health:
    down: bool = False
    reason: str = ""
    next_probe: float = 0.0


_HEALTH: dict[str, _Health] = {}
_HEALTH_LOCK = threading.Lock()


def _health(url: str) -> _Health:
    with _HEALTH_LOCK:
        return _HEALTH.setdefault(url, _Health())


def _mark_down(url: str, reason: str, interval: float) -> None:
    with _HEALTH_LOCK:
        state = _HEALTH.setdefault(url, _Health())
        state.down, state.reason = True, reason
        state.next_probe = time.monotonic() + interval


def _mark_up(url: str) -> None:
    with _HEALTH_LOCK:
        _HEALTH[url] = _Health()


def reset() -> None:
    """Forget health state and cached keys (tests)."""
    with _HEALTH_LOCK:
        _HEALTH.clear()
    _KEYS.clear()


def status(cfg: ValidatorSettings | None = None) -> dict[str, Any]:
    """This process's view of the validator (for diagnostics)."""
    cfg = cfg or active(ValidatorSettings)
    if not cfg.url:
        return {"configured": False}
    state = _health(cfg.url)
    return {"configured": True, "down": state.down, "reason": state.reason or None}


def _probe(client: httpx2.Client, cfg: ValidatorSettings) -> str | None:
    """``None`` when ``/ready`` answers 200, else why not."""
    try:
        resp = client.get(f"{cfg.url}/ready", timeout=cfg.health_timeout_seconds)
    except httpx2.HTTPError as exc:
        return f"not reachable: {type(exc).__name__}"
    if resp.status_code != 200:
        return f"not ready: HTTP {resp.status_code}"
    return None


# --------------------------------------------------------------------------- the check


def _read_capped(resp: httpx2.Response) -> bytes:
    data = bytearray()
    for chunk in resp.iter_bytes():
        data += chunk
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValidatorUnavailable("answer larger than the accepted maximum")
    return bytes(data)


def check_remote(
    source: str,
    filename: str,
    timeout: float,
    cfg: ValidatorSettings | None = None,
    *,
    transport: httpx2.BaseTransport | None = None,
    keys: KeyCache | None = None,
) -> ImportCheckResult:
    """Import ``source`` (as ``filename``) on the validator; :class:`ValidatorUnavailable`
    when it cannot be asked."""
    cfg = cfg or active(ValidatorSettings)
    if not cfg.url:
        raise ValidatorUnavailable("validator.url is not set")
    key = (keys or _KEYS).get(cfg.secret_conn_id, cfg.key_cache_seconds)
    path = urlsplit(cfg.url).path.rstrip("/") + CHECK_PATH
    body = json.dumps({"source": source, "filename": filename, "timeout": timeout}).encode()
    with httpx2.Client(
        transport=transport, **tls.httpx_kwargs("validator", transport=transport)
    ) as client:
        state = _health(cfg.url)
        if state.down:
            if time.monotonic() < state.next_probe:
                raise ValidatorUnavailable(state.reason)
            why = _probe(client, cfg)
            if why is not None:
                _mark_down(cfg.url, why, cfg.health_check_interval_seconds)
                raise ValidatorUnavailable(why)
        reason = "no attempt made"
        busy = False
        for attempt in range(cfg.retries + 1):
            if attempt:
                time.sleep(cfg.retry_backoff_seconds * 2 ** (attempt - 1))
            headers = {
                "Content-Type": "application/json",
                **sign(key, method="POST", path=path, body=body),
            }
            nonce = request_nonce(headers[IDENTITY_HEADER]) or ""
            try:
                with client.stream(
                    "POST",
                    f"{cfg.url}{CHECK_PATH}",
                    content=body,
                    headers=headers,
                    timeout=timeout + cfg.request_overhead_seconds,
                ) as resp:
                    code = resp.status_code
                    answer_signature = resp.headers.get(RESPONSE_SIGNATURE_HEADER, "")
                    data = _read_capped(resp)
            except httpx2.TimeoutException:
                # one slow or queued check says nothing about the service: no circuit
                raise ValidatorUnavailable(
                    f"no answer within {timeout + cfg.request_overhead_seconds:g}s"
                ) from None
            except httpx2.HTTPError as exc:
                reason = f"not reachable: {type(exc).__name__}"
                continue
            if code in _RETRY_STATUS:
                reason = f"HTTP {code}"
                busy = code in (429, 503)
                continue
            busy = False
            if code == 422:
                return ImportCheckResult(
                    ok=False,
                    import_errors=["the validator refused the check request (HTTP 422)"],
                )
            if code in (401, 403):
                reason = (
                    f"HTTP {code}: the validator rejected the signature; both sides need "
                    "the same key (validator.secret_conn_id) and synchronised clocks"
                )
                break
            if code == 413:
                return ImportCheckResult(
                    ok=False,
                    import_errors=[
                        "the DAG file is larger than the validator accepts "
                        "(validator.max_request_bytes)"
                    ],
                )
            if code >= 400:
                reason = f"HTTP {code}"
                break
            if not verify_response(key, nonce, code, data, answer_signature):
                reason = (
                    "the validator's answer is not signed with the shared key "
                    "(validator.secret_conn_id); it was not trusted"
                )
                break
            try:
                result = ImportCheckResult.model_validate_json(data)
            except ValueError:
                reason = "invalid answer"
                break
            _mark_up(cfg.url)
            result.ok = not result.import_errors and bool(result.dag_ids)
            return result
    if not busy:  # a busy validator is up: only outages open the circuit
        _mark_down(cfg.url, reason, cfg.health_check_interval_seconds)
    raise ValidatorUnavailable(reason)


__all__ = [
    "CHECK_PATH",
    "IDENTITY_HEADER",
    "RESPONSE_SIGNATURE_HEADER",
    "SIGNATURE_HEADER",
    "KeyCache",
    "KeyUnavailable",
    "ValidatorUnavailable",
    "check_remote",
    "connection_env_name",
    "password_from_airflow",
    "password_from_env",
    "request_nonce",
    "reset",
    "response_signature",
    "sign",
    "status",
    "verify_response",
]
