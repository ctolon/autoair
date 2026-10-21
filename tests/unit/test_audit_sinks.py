"""audit.sinks: file / db / log / otel emission (Airflow 3 has no event-log write API)."""

from __future__ import annotations

import json
import logging

import pytest

from autoair.api.app import _audit_log
from autoair.audit import AuditLog, FileAuditRepository
from autoair.sections import AuditSettings
from autoair.settings import Settings


def test_defaults_follow_the_state_store():
    assert AuditSettings().effective(has_database=False) == ["file", "log"]
    assert AuditSettings().effective(has_database=True) == ["db", "log"]
    assert AuditSettings(sinks=["log", "log", "otel"]).sinks == ["log", "otel"]
    with pytest.raises(ValueError, match="sinks"):
        AuditSettings(sinks=["airflow"])


def test_file_and_structured_log(tmp_path, caplog):
    audit = AuditLog(repositories=[FileAuditRepository(str(tmp_path))], log_sink=True)
    with caplog.at_level(logging.INFO, logger="autoair.audit"):
        rec = audit.record("deploy", user="alice", dag_id="d1", env="prod")
    [line] = [r for r in caplog.records if r.name == "autoair.audit"]
    assert line.audit == rec.model_dump()  # structured extra for JSON formatters
    assert json.loads(line.getMessage().removeprefix("audit "))["action"] == "deploy"
    assert [r.action for r in audit.tail()] == ["deploy"]


def test_log_only_and_failing_repository_never_breaks(caplog):
    class Broken:
        def append(self, record):
            raise OSError("disk full")

        def query(self, **_):
            return []

    audit = AuditLog(repositories=[Broken()], log_sink=False)
    with caplog.at_level(logging.INFO, logger="autoair.audit"):
        audit.record("delete", user="bob")
    assert not [r for r in caplog.records if r.getMessage().startswith("audit {")]
    assert any("failed to write audit record" in r.getMessage() for r in caplog.records)


def test_build_services_wiring(tmp_path, caplog):
    settings = Settings(dags_folder=tmp_path, subfolder="", audit={"sinks": ["file", "db"]})
    with caplog.at_level(logging.WARNING):
        audit = _audit_log(settings, tmp_path, None)
    assert [type(r).__name__ for r in audit.repositories] == ["FileAuditRepository"]
    assert "no database configured" in caplog.text
    assert audit.log_sink is False
    assert audit.otel_sink is False

    sentinel = object()
    both = _audit_log(
        Settings(dags_folder=tmp_path, subfolder="", audit={"sinks": ["file", "db", "otel"]}),
        tmp_path,
        sentinel,
    )
    assert both.repositories[0] is sentinel  # queries read the database first
    assert both.otel_sink is True


def test_otel_span_per_record():
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from autoair import audit as audit_mod

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = audit_mod.emit_otel

    def emit(rec):
        from opentelemetry import trace

        real = trace.get_tracer
        trace.get_tracer = lambda name, *a, **k: provider.get_tracer(name)
        try:
            original(rec)
        finally:
            trace.get_tracer = real

    audit_mod.emit_otel = emit
    try:
        AuditLog(log_sink=False, otel_sink=True).record("deploy", user="u", dag_id="d", n=1)
    finally:
        audit_mod.emit_otel = original
    [span] = exporter.get_finished_spans()
    assert span.name == "audit deploy"
    assert span.attributes["autoair.audit.dag_id"] == "d"
    assert json.loads(span.attributes["autoair.audit.details"]) == {"n": 1}
