import contextlib
import logging
import os
import socket
import threading
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from netbox_opentelemetry_plugin import otel
from netbox_opentelemetry_plugin.conf import ExporterConfig


@pytest.fixture
def resource():
    return otel.build_resource(
        "netbox",
        {"deployment.environment.name": "test"},
        service_version="4.7.1",
        plugin_version="0.1.0",
        role="web",
    )


@pytest.fixture
def pipeline(resource):
    exporter = InMemoryLogRecordExporter()
    provider = otel.build_logger_provider(resource, exporter, synchronous=True)
    handler = otel.build_logging_handler(provider, logging.INFO)
    yield exporter, handler
    provider.shutdown()


@contextlib.contextmanager
def _logger(name, handler):
    lg = logging.getLogger(name)
    previous_level = lg.level
    previous_propagate = lg.propagate
    previous_handlers = list(lg.handlers)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addHandler(handler)
    try:
        yield lg
    finally:
        lg.setLevel(previous_level)
        lg.propagate = previous_propagate
        lg.handlers = previous_handlers


def test_build_resource_attributes(resource):
    attrs = resource.attributes
    assert attrs["service.name"] == "netbox"
    assert attrs["service.version"] == "4.7.1"
    assert attrs["netbox.plugin.version"] == "0.1.0"
    assert attrs["netbox.process.role"] == "web"
    assert attrs["service.instance.id"] == f"{socket.gethostname()}-{os.getpid()}"
    assert attrs["deployment.environment.name"] == "test"


def test_handler_exports_allowlisted_attributes_only(pipeline):
    exporter, handler = pipeline
    with _logger("netbox.test.allowlist", handler) as lg:
        lg.info("hello %s", "world", extra={"secret_token": "s3cr3t"})
    record = exporter.get_finished_logs()[0]
    assert record.log_record.body == "hello world"
    assert record.log_record.severity_text == "INFO"
    attrs = dict(record.log_record.attributes)
    assert "secret_token" not in attrs
    assert set(attrs) <= otel.LOG_ATTRIBUTE_ALLOWLIST
    assert attrs["logger.name"] == "netbox.test.allowlist"
    assert attrs["code.function.name"] == "test_handler_exports_allowlisted_attributes_only"
    assert record.resource.attributes["service.name"] == "netbox"


def test_handler_exports_exception_attributes(pipeline):
    exporter, handler = pipeline
    with _logger("netbox.test.exc", handler) as lg:
        try:
            raise ValueError("boom")
        except ValueError:
            lg.exception("failed")
    attrs = dict(exporter.get_finished_logs()[0].log_record.attributes)
    assert attrs["exception.type"] == "ValueError"
    assert attrs["exception.message"] == "boom"
    assert "Traceback" in attrs["exception.stacktrace"]


def test_handler_level_filters_records(pipeline):
    exporter, handler = pipeline
    with _logger("netbox.test.level", handler) as lg:
        lg.debug("dropped")
        lg.info("kept")
    assert [r.log_record.body for r in exporter.get_finished_logs()] == ["kept"]


@pytest.mark.parametrize(
    "name",
    [
        "netbox_opentelemetry_plugin",
        "netbox_opentelemetry_plugin.x",
        "opentelemetry.sdk",
        "urllib3.connectionpool",
        "grpc",
    ],
)
def test_handler_rejects_feedback_loop_loggers(pipeline, name):
    exporter, handler = pipeline
    with _logger(name, handler) as lg:
        lg.warning("must not be exported")
    assert len(exporter.get_finished_logs()) == 0


def test_similar_prefix_is_not_rejected(pipeline):
    exporter, handler = pipeline
    with _logger("grpcish", handler) as lg:
        lg.warning("exported")
    assert len(exporter.get_finished_logs()) == 1


def test_build_log_exporter_http():
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    exporter = otel.build_log_exporter(
        ExporterConfig("http://collector:4318/v1/logs", "http/protobuf", {"a": "b"}, 3.0)
    )
    assert isinstance(exporter, OTLPLogExporter)
    assert exporter._endpoint == "http://collector:4318/v1/logs"
    assert exporter._timeout == 3.0


def test_build_log_exporter_grpc():
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    exporter = otel.build_log_exporter(ExporterConfig("http://collector:4317", "grpc", {}, 3.0, insecure=True))
    assert isinstance(exporter, OTLPLogExporter)
    exporter.shutdown()


def test_existing_logger_provider_is_none_by_default():
    assert otel.existing_logger_provider() is None


def test_malformed_format_does_not_raise(pipeline, monkeypatch):
    exporter, handler = pipeline
    monkeypatch.setattr(logging, "raiseExceptions", False)
    with _logger("netbox.test.malformed", handler) as lg:
        lg.info("value %s %s", 1)
    assert len(exporter.get_finished_logs()) == 0


def test_extra_cannot_spoof_exception_attributes(pipeline):
    exporter, handler = pipeline
    with _logger("netbox.test.spoof", handler) as lg:
        lg.info("hello", extra={"exception.type": "Fake"})
    attrs = dict(exporter.get_finished_logs()[0].log_record.attributes)
    assert "exception.type" not in attrs


def test_warning_severity_text_is_warn(pipeline):
    exporter, handler = pipeline
    with _logger("netbox.test.warn", handler) as lg:
        lg.warning("careful")
    assert exporter.get_finished_logs()[0].log_record.severity_text == "WARN"


def test_records_inside_span_carry_trace_id(pipeline):
    exporter, handler = pipeline
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    tracer = tracer_provider.get_tracer(__name__)
    span = tracer.start_span("test-span")
    try:
        with trace.use_span(span), _logger("netbox.test.trace", handler) as lg:
            lg.info("inside span")
    finally:
        span.end()
    record = exporter.get_finished_logs()[0]
    assert record.log_record.trace_id == span.get_span_context().trace_id
    tracer_provider.shutdown()


def test_grpc_insecure_none_is_inferred_from_http_scheme():
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    exporter = otel.build_log_exporter(ExporterConfig("http://collector:4317", "grpc", {}, 3.0, insecure=None))
    assert isinstance(exporter, OTLPLogExporter)
    assert exporter._insecure is True
    exporter.shutdown()


def test_header_values_are_not_in_settings_repr():
    from netbox_opentelemetry_plugin.conf import LogsConfig, Settings

    exporter = ExporterConfig("http://collector:4318/v1/logs", "http/protobuf", {"authorization": "TOPSECRET"})
    settings = Settings(enabled=True, logs=LogsConfig(enabled=True), log_exporter=exporter)
    assert "TOPSECRET" not in repr(settings)


def test_flush_does_not_block_on_provider():
    called = threading.Event()
    release = threading.Event()

    class SlowProvider:
        def force_flush(self):
            called.set()
            release.wait(5)

    handler = otel.AllowlistLoggingHandler(logging.INFO, SlowProvider())
    started = time.monotonic()
    handler.flush()
    assert time.monotonic() - started < 0.5
    assert called.wait(2)
    release.set()


def test_flush_swallows_provider_errors():
    done = threading.Event()

    class BrokenProvider:
        def force_flush(self):
            done.set()
            raise RuntimeError("flush failed")

    handler = otel.AllowlistLoggingHandler(logging.INFO, BrokenProvider())
    handler.flush()
    assert done.wait(2)


def test_emit_event_sets_event_name_scope_and_attributes(resource):
    exporter = InMemoryLogRecordExporter()
    provider = otel.build_logger_provider(resource, exporter, synchronous=True)
    otel.emit_event(
        provider,
        "netbox_opentelemetry_plugin.audit",
        event_name="netbox.object_change",
        body="create ipam.prefix 10.0.0.0/24",
        attributes={"netbox.change.id": 7},
        timestamp_ns=1_700_000_000_000_000_000,
    )
    record = exporter.get_finished_logs()[0]
    assert record.instrumentation_scope.name == "netbox_opentelemetry_plugin.audit"
    assert record.log_record.event_name == "netbox.object_change"
    assert record.log_record.body == "create ipam.prefix 10.0.0.0/24"
    assert record.log_record.severity_text == "INFO"
    assert record.log_record.timestamp == 1_700_000_000_000_000_000
    assert dict(record.log_record.attributes) == {"netbox.change.id": 7}
    provider.shutdown()


def test_build_logger_provider_sets_max_queue_size(resource):
    exporter = InMemoryLogRecordExporter()
    provider = otel.build_logger_provider(resource, exporter, max_queue_size=12345)
    # No public accessor exists for this; the SDK's BatchLogRecordProcessor stores it on a nested
    # BatchProcessor. If this internal shape ever changes, replace with a behavioural test that
    # fills the queue past the default (2048) and checks records beyond it are not dropped.
    processor = provider._multi_log_record_processor._log_record_processors[0]
    assert processor._batch_processor._max_queue_size == 12345
    provider.shutdown()


def _skip_verify_cfg(endpoint="https://collector:4318/v1/logs"):
    return ExporterConfig(endpoint, "http/protobuf", {}, 3.0, insecure_skip_verify=True)


@pytest.mark.parametrize(
    "build", [otel.build_log_exporter, otel.build_span_exporter, otel.build_metric_exporter], ids=lambda b: b.__name__
)
def test_http_exporters_skip_verification_when_asked(build, monkeypatch):
    import requests

    monkeypatch.setattr(otel, "_skip_verify_warned", set())
    seen = {}

    def fake_send(self, request, **kwargs):
        seen["verify"] = kwargs.get("verify")
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "send", fake_send)
    exporter = build(_skip_verify_cfg())
    # The exporter passes verify=True (its default) per request; the session must override it.
    exporter._session.post("https://collector:4318/v1/logs", data=b"", verify=True)
    assert seen["verify"] is False
    exporter.shutdown()


def test_http_exporter_verifies_by_default(monkeypatch):
    exporter = otel.build_log_exporter(ExporterConfig("https://collector:4318/v1/logs", "http/protobuf", {}, 3.0))
    assert not isinstance(exporter._session, otel._NoVerifySession)
    exporter.shutdown()


def test_skip_verify_warns_once_per_process_and_endpoint(monkeypatch, caplog):
    monkeypatch.setattr(otel, "_skip_verify_warned", set())
    caplog.set_level(logging.WARNING, logger="netbox_opentelemetry_plugin")
    for _ in range(2):
        otel.build_log_exporter(_skip_verify_cfg()).shutdown()
    otel.build_span_exporter(_skip_verify_cfg("https://collector:4318/v1/traces")).shutdown()
    messages = [r.getMessage() for r in caplog.records if "certificate verification is disabled" in r.getMessage()]
    assert len(messages) == 2
    assert "https://collector:4318/v1/logs" in messages[0]


def test_skip_verify_warning_redacts_endpoint_userinfo(monkeypatch, caplog):
    monkeypatch.setattr(otel, "_skip_verify_warned", set())
    caplog.set_level(logging.WARNING, logger="netbox_opentelemetry_plugin")
    otel.build_log_exporter(_skip_verify_cfg("https://user:pass@collector:4318/v1/logs")).shutdown()
    assert "user:pass" not in caplog.text
    assert "https://***@collector:4318/v1/logs" in caplog.text
