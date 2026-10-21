"""Masking of per-request secrets (a git token the user typed for one deploy).

A secret registered with :func:`masking` is replaced by ``***`` — in its clear form and the
encodings it travels in (HTTP Basic credentials, URL-encoding) — wherever it could be echoed
while the request runs: log records (:func:`install_log_masking`), audit records
(:class:`autoair.audit.AuditLog`) and error details (:func:`mask`).

The registry is a :class:`~contextvars.ContextVar`: it lives in the request's own context
(copied into the worker threads ``run_in_threadpool`` uses) and is never visible to other
requests. It only ever *masks*; nothing authenticates with it — the token itself is passed
explicitly to the provider that uses it (:mod:`autoair.storage.request_token`).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import threading
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

MASK = "***"
_MIN_LENGTH = 4  # shorter strings would mask ordinary text; such tokens are refused anyway
# Basic-auth user names AutoAir pairs a token with (git http.extraHeader, Azure DevOps PAT).
_BASIC_USERS = ("x-access-token", "", "oauth2", "x-token-auth")

_ACTIVE: ContextVar[tuple[str, ...]] = ContextVar("autoair_masked_secrets", default=())
_INSTALL_LOCK = threading.Lock()
_installed = False


def forms(secret: str) -> tuple[str, ...]:
    """``secret`` and the encodings it is sent in (longest first)."""
    out = {secret, quote(secret, safe="")}
    for user in _BASIC_USERS:
        out.add(base64.b64encode(f"{user}:{secret}".encode()).decode())
    return tuple(sorted((f for f in out if len(f) >= _MIN_LENGTH), key=len, reverse=True))


@contextlib.contextmanager
def masking(secret: str | None) -> Iterator[None]:
    """Mask ``secret`` in logs, audit records and :func:`mask` within this block."""
    if not secret or len(secret) < _MIN_LENGTH:
        yield
        return
    token = _ACTIVE.set((*_ACTIVE.get(), *forms(secret)))
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):  # exited in another context: let it expire
            _ACTIVE.reset(token)


def active() -> bool:
    return bool(_ACTIVE.get())


def mask(text: str) -> str:
    """``text`` with every registered secret replaced by ``***``."""
    for secret in _ACTIVE.get():
        if secret in text:
            text = text.replace(secret, MASK)
    return text


def mask_data(value: Any) -> Any:
    """Plain data (dicts, lists, strings) with registered secrets masked, recursively."""
    if not _ACTIVE.get():
        return value
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {mask(k) if isinstance(k, str) else k: mask_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(mask_data(v) for v in value)
    return value


def install_log_masking() -> None:
    """Mask registered secrets in every log record (message, arguments and traceback),
    whatever handler or formatter writes it. Idempotent."""
    global _installed
    with _INSTALL_LOCK:
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            if _ACTIVE.get():
                _mask_record(record)
            return record

        logging.setLogRecordFactory(factory)
        _installed = True


def _mask_record(record: logging.LogRecord) -> None:
    try:
        message = record.getMessage()
    except Exception:  # a broken format string: logging reports it itself
        return
    masked = mask(message)
    if masked != message:
        record.msg, record.args = masked, ()
    if record.exc_info and record.exc_info[1] is not None:
        # Formatters reuse exc_text instead of formatting the exception again.
        record.exc_text = mask(logging.Formatter().formatException(record.exc_info))
    if record.stack_info:
        record.stack_info = mask(record.stack_info)


__all__ = ["MASK", "active", "forms", "install_log_masking", "mask", "mask_data", "masking"]
