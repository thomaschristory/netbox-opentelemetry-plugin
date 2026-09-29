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


# Horses use the batch processors (synchronous=False); the simple ones are covered too.
SYNC = pytest.mark.parametrize("synchronous", [False, True], ids=["batch", "simple"])


@SYNC
def test_successful_log_and_span_exports_are_counted(resource, synchronous):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter(), synchronous=synchronous)
    spans = otel.build_tracer_provider(
        resource, InMemorySpanExporter(), otel.build_sampler("always_on", 1.0), synchronous=synchronous
    )
    try:
        before_logs, before_spans = otel.export_outcomes("logs"), otel.export_outcomes("traces")
        _emit(logs)
        assert logs.force_flush()
        after_logs = otel.export_outcomes("logs")
        assert after_logs.succeeded - before_logs.succeeded == 1
        assert otel.export_outcomes("traces") == before_spans
        _span(spans)
        assert spans.force_flush()
        after_spans = otel.export_outcomes("traces")
        assert after_spans.succeeded - before_spans.succeeded == 1
        assert otel.export_outcomes("logs") == after_logs
        assert after_logs.failed == before_logs.failed
        assert after_spans.failed == before_spans.failed
    finally:
        logs.shutdown()
        spans.shutdown()


@SYNC
@pytest.mark.parametrize("exporter_cls", [_Failing, _Raising])
def test_failed_or_raising_log_exports_are_counted(resource, exporter_cls, synchronous):
    logs = otel.build_logger_provider(resource, exporter_cls(), synchronous=synchronous)
    try:
        before = otel.export_outcomes("logs")
        _emit(logs)
        logs.force_flush()
        after = otel.export_outcomes("logs")
        assert after.failed - before.failed == 1
        assert after.succeeded == before.succeeded
    finally:
        logs.shutdown()


@SYNC
def test_failed_span_exports_are_counted(resource, synchronous):
    spans = otel.build_tracer_provider(
        resource, _FailingSpans(), otel.build_sampler("always_on", 1.0), synchronous=synchronous
    )
    try:
        before = otel.export_outcomes("traces")
        _span(spans)
        spans.force_flush()
        assert otel.export_outcomes("traces").failed - before.failed == 1
    finally:
        spans.shutdown()


def test_time_spent_in_failed_exports_is_added_up(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(otel.time, "monotonic", lambda: now[0])

    class Timed:
        def __init__(self):
            self.result, self.seconds, self.raises = LogRecordExportResult.FAILURE, 0.0, False

        def export(self, batch):
            now[0] += self.seconds
            if self.raises:
                raise ConnectionError("collector down")
            return self.result

    delegate = Timed()
    counted = otel._OutcomeCountingExporter(delegate, "logs")
    before, before_spans = otel.export_outcomes("logs"), otel.export_outcomes("traces")
    delegate.seconds = 2.5
    counted.export([])
    delegate.result = LogRecordExportResult.SUCCESS
    delegate.seconds = 4.0
    counted.export([])  # time spent in a successful export is not added
    delegate.seconds, delegate.raises = 1.0, True
    with pytest.raises(ConnectionError):
        counted.export([])
    after = otel.export_outcomes("logs")
    assert after.failed_seconds - before.failed_seconds == pytest.approx(3.5)
    assert otel.export_outcomes("traces") == before_spans


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

    otel._OutcomeCountingExporter(WithTimeout(), "logs").shutdown(timeout_millis=250)
    otel._OutcomeCountingExporter(WithoutTimeout(), "traces").shutdown(timeout_millis=250)
    assert calls == [250, "plain"]


def test_buffered_records_counts_queued_logs_and_spans(resource):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter())
    spans = otel.build_tracer_provider(resource, InMemorySpanExporter(), otel.build_sampler("always_on", 1.0))
    try:
        logs_before, spans_before = otel.buffered_records("logs"), otel.buffered_records("traces")
        _emit(logs, 3)
        _span(spans)
        # The batch threads only wake up after their schedule delay (1 s for logs, 5 s for spans).
        assert otel.buffered_records("logs") - logs_before == 3
        assert otel.buffered_records("traces") - spans_before == 1
    finally:
        logs.shutdown()
        spans.shutdown()


def test_buffered_records_is_none_when_sdk_internals_change(resource, monkeypatch):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter())
    try:
        processor = otel._batch_processors[-1][2]()
        monkeypatch.delattr(processor, "_batch_processor")
        assert otel.buffered_records("logs") is None
    finally:
        monkeypatch.undo()
        logs.shutdown()


def test_buffered_records_ignores_processors_of_the_parent_process(resource, monkeypatch):
    logs = otel.build_logger_provider(resource, InMemoryLogRecordExporter())
    try:
        baseline = otel.buffered_records("logs")
        _emit(logs, 2)
        monkeypatch.setattr(os, "getpid", lambda: -1)
        assert otel.buffered_records("logs") == 0
        monkeypatch.undo()
        assert otel.buffered_records("logs") - baseline == 2
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
            before = otel.export_outcomes("logs")
            _emit(logs)
            ok = otel.export_outcomes("logs").succeeded - before.succeeded == 1
            os.write(write_fd, b"1" if ok else b"0")
        finally:
            os._exit(0)
    os.close(write_fd)
    os.waitpid(pid, 0)
    assert os.read(read_fd, 1) == b"1"
    os.close(read_fd)
    logs.shutdown()
