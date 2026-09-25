import logging
import os
import socket

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

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


def _logger(name, handler):
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addHandler(handler)
    return lg


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
    lg = _logger("netbox.test.allowlist", handler)
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
    lg.removeHandler(handler)


def test_handler_exports_exception_attributes(pipeline):
    exporter, handler = pipeline
    lg = _logger("netbox.test.exc", handler)
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("failed")
    attrs = dict(exporter.get_finished_logs()[0].log_record.attributes)
    assert attrs["exception.type"] == "ValueError"
    assert attrs["exception.message"] == "boom"
    assert "Traceback" in attrs["exception.stacktrace"]
    lg.removeHandler(handler)


def test_handler_level_filters_records(pipeline):
    exporter, handler = pipeline
    lg = _logger("netbox.test.level", handler)
    lg.debug("dropped")
    lg.info("kept")
    assert [r.log_record.body for r in exporter.get_finished_logs()] == ["kept"]
    lg.removeHandler(handler)


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
    lg = _logger(name, handler)
    lg.warning("must not be exported")
    assert len(exporter.get_finished_logs()) == 0
    lg.removeHandler(handler)


def test_similar_prefix_is_not_rejected(pipeline):
    exporter, handler = pipeline
    lg = _logger("grpcish", handler)
    lg.warning("exported")
    assert len(exporter.get_finished_logs()) == 1
    lg.removeHandler(handler)


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
