"""Ask an Open Policy Agent about a spec (``policy.opa_url``).

AutoAir POSTs ``{"input": {"spec": <DagSpec JSON>, "context": {...}}}`` to the configured
decision URL (e.g. ``http://opa:8181/v1/data/autoair/violations``) and turns the decision
(``result``) into problems with code ``policy_opa``. Accepted decision shapes:

* ``true`` / ``false`` - allowed / one error;
* a list of messages or objects ``{message, severity, node_id, field}``: one problem each
  (severity ``error`` unless the object says ``warning``);
* an object with ``deny``/``violations`` (errors), ``warn``/``warnings`` (warnings) lists
  and/or ``allow`` (bool);
* no ``result`` (undefined decision): nothing to report.

When OPA cannot be asked (timeout, connection error, non-2xx, invalid JSON), ``fail_mode``
decides: ``open`` reports a warning and lets the DAG pass, ``closed`` reports an error.
Decisions are cached for ``cache_seconds`` per identical input, so the live validation of
the editor does not hit OPA on every keystroke of an unchanged document.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx2

from autoair.net import tls
from autoair.policy_rules import OpaOptions
from autoair.spec import DagSpec
from autoair.validation import Problem

_CACHE: OrderedDict[str, tuple[float, list[Problem]]] = OrderedDict()
_CACHE_SIZE = 256  # decisions kept (identical inputs within cache_seconds reuse them)
_LOCK = threading.Lock()


def _message(item: dict[str, Any]) -> str:
    for key in ("message", "msg", "reason"):
        if isinstance(item.get(key), str):
            return str(item[key])
    return json.dumps(item, sort_keys=True)


def _items(value: Any, severity: str) -> list[Problem]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    out: list[Problem] = []
    for item in value:
        if isinstance(item, dict):
            sev = item.get("severity", severity)
            out.append(
                Problem(
                    severity="warning" if sev in ("warning", "warn") else "error",
                    code="policy_opa",
                    message=_message(item),
                    node_id=item.get("node_id") if isinstance(item.get("node_id"), str) else None,
                    field=item.get("field") if isinstance(item.get("field"), str) else None,
                )
            )
        else:
            out.append(
                Problem(
                    severity="warning" if severity == "warning" else "error",
                    code="policy_opa",
                    message=str(item),
                )
            )
    return out


def decision_problems(result: Any) -> list[Problem]:
    """Problems of an OPA decision (``result`` of the Data API response)."""
    if result is None or result is True:
        return []
    if result is False:
        return [Problem(severity="error", code="policy_opa", message="denied by the OPA policy")]
    if isinstance(result, list):
        return _items(result, "error")
    if isinstance(result, dict):
        out: list[Problem] = []
        for key in ("deny", "violations", "errors"):
            out += _items(result.get(key), "error")
        for key in ("warn", "warnings"):
            out += _items(result.get(key), "warning")
        if result.get("allow") is False and not any(p.severity == "error" for p in out):
            out.append(
                Problem(severity="error", code="policy_opa", message="denied by the OPA policy")
            )
        return out
    return [
        Problem(
            severity="warning",
            code="policy_opa",
            message=f"unexpected OPA decision of type {type(result).__name__}; ignored",
        )
    ]


def _failure(options: OpaOptions, why: str) -> list[Problem]:
    closed = options.fail_mode == "closed"
    return [
        Problem(
            severity="error" if closed else "warning",
            code="policy_opa",
            message=f"OPA policy could not be evaluated ({why}); "
            + ("deploys are blocked (fail closed)" if closed else "not enforced (fail open)"),
        )
    ]


def opa_problems(
    spec: DagSpec,
    context: dict[str, Any],
    options: OpaOptions,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> list[Problem]:
    body = json.dumps(
        {"input": {"spec": spec.model_dump(mode="json", by_alias=True), "context": context}},
        sort_keys=True,
        default=str,
    ).encode()
    key = hashlib.sha256(options.url.encode() + b"\0" + body).hexdigest()
    now = time.monotonic()
    if options.cache_seconds > 0:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None and now - hit[0] < options.cache_seconds:
                return [p.model_copy() for p in hit[1]]
    headers = {"Content-Type": "application/json", **options.headers}
    if options.token is not None:
        headers["Authorization"] = f"Bearer {options.token.get_secret_value()}"
    try:
        with httpx2.Client(
            timeout=options.timeout,
            transport=transport,
            **tls.httpx_kwargs("opa", transport=transport),
        ) as client:
            resp = client.post(options.url, content=body, headers=headers)
        if resp.status_code >= 400:
            return _failure(options, f"HTTP {resp.status_code}")
        data = resp.json()
    except httpx2.TimeoutException:
        return _failure(options, f"timeout after {options.timeout:g}s")
    except httpx2.HTTPError as exc:
        return _failure(options, type(exc).__name__)
    except ValueError:
        return _failure(options, "invalid JSON response")
    problems = decision_problems(data.get("result") if isinstance(data, dict) else None)
    if options.cache_seconds > 0:
        with _LOCK:
            _CACHE[key] = (now, problems)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.popitem(last=False)
    return [p.model_copy() for p in problems]


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = ["clear_cache", "decision_problems", "opa_problems"]
