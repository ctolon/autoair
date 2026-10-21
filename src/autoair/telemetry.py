"""Optional OpenTelemetry tracing (``pip install autoair[otel]``, ``AUTOAIR_OTEL_ENABLED=true``).

Exporter configuration follows the standard ``OTEL_*`` environment variables
(``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_SERVICE_NAME``...). Inside Airflow an already
configured global tracer provider is reused instead of replaced.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)
_configured = False


def setup_tracing(app: Any, *, service_name: str = "autoair") -> bool:
    """Instrument a FastAPI app; returns False when OpenTelemetry is not installed."""
    global _configured
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        log.warning(
            "AUTOAIR_OTEL_ENABLED is set but opentelemetry is not installed "
            "(pip install 'autoair[otel]')"
        )
        return False
    if not _configured:
        provider = trace.get_tracer_provider()
        if type(provider).__name__ in ("ProxyTracerProvider", "NoOpTracerProvider"):
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            sdk_provider = TracerProvider(
                resource=Resource.create(
                    {"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)}
                )
            )
            if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
            ):
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )

                    sdk_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                except ImportError:
                    log.warning("OTLP exporter not installed; spans are not exported")
            trace.set_tracer_provider(sdk_provider)
        _configured = True
    from autoair.sections import ServerSettings, active

    excluded = ",".join(active(ServerSettings).otel_excluded_urls)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded)
    return True


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """A span when OpenTelemetry is available, otherwise a no-op."""
    try:
        from opentelemetry import trace
    except ImportError:
        yield
        return
    with trace.get_tracer("autoair").start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(f"autoair.{key}", value)
        yield
