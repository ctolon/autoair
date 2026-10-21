"""Token and cost accounting.

Every provider response carries a :class:`Usage`. A :func:`track_usage` scope (a context
variable, so it is per request / per thread) adds up everything the LLM facade records
while it is active; AI endpoints return that total so users see what a feature cost.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, Field


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)
    cache_read: float | None = Field(default=None, ge=0, description="Default: input price.")
    cache_write: float | None = Field(default=None, ge=0, description="Default: input price.")


class Usage(BaseModel):
    """Tokens of one or more model calls. ``input_tokens`` excludes cached input."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = Field(
        default=None, description="Known only when pricing is configured for the model."
    )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        cost: float | None
        if self.cost_usd is None and other.cost_usd is None:
            cost = None
        else:
            cost = round((self.cost_usd or 0) + (other.cost_usd or 0), 8)
        return Usage(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=cost,
        )

    def priced(self, price: ModelPrice | None) -> Usage:
        if price is None:
            return self
        read = price.cache_read if price.cache_read is not None else price.input
        write = price.cache_write if price.cache_write is not None else price.input
        cost = (
            self.input_tokens * price.input
            + self.output_tokens * price.output
            + self.cache_read_tokens * read
            + self.cache_write_tokens * write
        ) / 1_000_000
        return self.model_copy(update={"cost_usd": round(cost, 8)})


def price_for(model: str, pricing: Mapping[str, Mapping[str, float]] | None) -> ModelPrice | None:
    """Pricing entry of ``model`` (exact id, else the longest configured prefix)."""
    if not pricing:
        return None
    if model in pricing:
        return ModelPrice.model_validate(dict(pricing[model]))
    prefixes = [k for k in pricing if model.startswith(k)]
    if not prefixes:
        return None
    return ModelPrice.model_validate(dict(pricing[max(prefixes, key=len)]))


class UsageMeter:
    """Thread-safe accumulator; usage also counts in the enclosing (parent) scope."""

    def __init__(self, parent: UsageMeter | None = None) -> None:
        self._lock = threading.Lock()
        self._total = Usage()
        self.parent = parent

    def add(self, usage: Usage) -> None:
        with self._lock:
            self._total = self._total + usage
        if self.parent is not None:
            self.parent.add(usage)

    @property
    def total(self) -> Usage:
        with self._lock:
            return self._total


_METER: ContextVar[UsageMeter | None] = ContextVar("autoair_ai_usage", default=None)


@contextmanager
def track_usage() -> Iterator[UsageMeter]:
    """Collect the usage of every model call made inside the ``with`` block (nested
    scopes also add to the enclosing one, e.g. a feature's meter and the audit meter)."""
    meter = UsageMeter(_METER.get())
    token = _METER.set(meter)
    try:
        yield meter
    finally:
        _METER.reset(token)


def record_usage(usage: Usage) -> None:
    meter = _METER.get()
    if meter is not None:
        meter.add(usage)
