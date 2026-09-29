"""Export outcome counters and buffered record counts, used by the RQ work-horse flush breaker."""

import logging
import os

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from netbox_opentelemetry_plugin import otel

# Python 3.12+ warns when forking a process that has threads (the batch worker). Expected here.
FORK_WARNING = "ignore:.*use of fork\\(\\) may lead to deadlocks:DeprecationWarning"


@pytest.fixture
def resource():
    return otel.build_resource("netbox", {}, service_version="4.7.1", plugin_version="0.1.0", role="rq_horse")


class _Failing(InMemoryLogRecordExporter):
    def export(self, batch):
        return LogRecordExportResult.FAILURE


class _Raising(InMemoryLogRecordExporter):
    def export(self, batch):
        raise ConnectionError("collector down")


class _FailingSpans(InMemorySpanExporter):
    def export(self, spans):
        return SpanExportResult.FAILURE


def _emit(provider, count=1):
    handler = otel.build_logging_handler(provider, logging.INFO)
    lg = logging.getLogger("t.outcomes")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        for i in range(count):
            lg.info("record %s", i)
    finally:
        lg.removeHandler(handler)


def _span(provider):
    provider.get_tracer("t").start_span("work").end()


def test_successful_log_and_span_exports_are_counted(resource):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter(), synchronous=True)
    spans = otel.build_tracer_provider(
        resource, InMemorySpanExporter(), otel.build_sampler("always_on", 1.0), synchronous=True
    )
    before = otel.export_outcomes()
    _emit(logs)
    _span(spans)
    after = otel.export_outcomes()
    assert after.succeeded - before.succeeded == 2
    assert after.failed == before.failed
    logs.shutdown()
    spans.shutdown()


@pytest.mark.parametrize("exporter_cls", [_Failing, _Raising])
def test_failed_or_raising_log_exports_are_counted(resource, exporter_cls):
    logs = otel.build_logger_provider(resource, exporter_cls(), synchronous=True)
    before = otel.export_outcomes()
    _emit(logs)
    after = otel.export_outcomes()
    assert after.failed - before.failed == 1
    assert after.succeeded == before.succeeded
    logs.shutdown()


def test_failed_span_exports_are_counted(resource):
    spans = otel.build_tracer_provider(
        resource, _FailingSpans(), otel.build_sampler("always_on", 1.0), synchronous=True
    )
    before = otel.export_outcomes()
    _span(spans)
    assert otel.export_outcomes().failed - before.failed == 1
    spans.shutdown()


def test_counting_keeps_the_exporter_usable(resource):
    exporter = InMemoryLogRecordExporter()
    logs = otel.build_logger_provider(resource, exporter, synchronous=True)
    _emit(logs, 2)
    assert len(exporter.get_finished_logs()) == 2
    logs.shutdown()


def test_shutdown_timeout_is_forwarded_only_when_the_exporter_takes_one():
    calls = []

    class WithTimeout:
        def shutdown(self, timeout_millis: float = 30_000, **kwargs):
            calls.append(timeout_millis)

    class WithoutTimeout:
        def shutdown(self):
            calls.append("plain")

    otel._OutcomeCountingExporter(WithTimeout()).shutdown(timeout_millis=250)
    otel._OutcomeCountingExporter(WithoutTimeout()).shutdown(timeout_millis=250)
    assert calls == [250, "plain"]


def test_buffered_records_counts_queued_logs_and_spans(resource):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter())
    spans = otel.build_tracer_provider(resource, InMemorySpanExporter(), otel.build_sampler("always_on", 1.0))
    try:
        baseline = otel.buffered_records()
        _emit(logs, 3)
        _span(spans)
        # The batch threads only wake up after their schedule delay (1 s for logs, 5 s for spans).
        assert otel.buffered_records() - baseline == 4
    finally:
        logs.shutdown()
        spans.shutdown()


def test_buffered_records_is_none_when_sdk_internals_change(resource, monkeypatch):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter())
    try:
        processor = otel._batch_processors[-1][1]()
        monkeypatch.delattr(processor, "_batch_processor")
        assert otel.buffered_records() is None
    finally:
        monkeypatch.undo()
        logs.shutdown()


def test_buffered_records_ignores_processors_of_the_parent_process(resource, monkeypatch):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter())
    try:
        baseline = otel.buffered_records()
        _emit(logs, 2)
        monkeypatch.setattr(os, "getpid", lambda: -1)
        assert otel.buffered_records() == 0
        monkeypatch.undo()
        assert otel.buffered_records() - baseline == 2
    finally:
        logs.shutdown()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings(FORK_WARNING)
def test_counters_work_in_a_forked_child(resource):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter(), synchronous=True)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            before = otel.export_outcomes()
            _emit(logs)
            ok = otel.export_outcomes().succeeded - before.succeeded == 1
            os.write(write_fd, b"1" if ok else b"0")
        finally:
            os._exit(0)
    os.close(write_fd)
    os.waitpid(pid, 0)
    assert os.read(read_fd, 1) == b"1"
    os.close(read_fd)
    logs.shutdown()
