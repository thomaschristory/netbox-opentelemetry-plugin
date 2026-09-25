import logging

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from netbox_opentelemetry_plugin import otel
from netbox_opentelemetry_plugin.conf import ExporterConfig, LogsConfig, Settings
from netbox_opentelemetry_plugin.modules.base import Context
from netbox_opentelemetry_plugin.modules.logs import LogsModule


def _settings(loggers, level=logging.INFO, set_levels=False):
    logs = LogsConfig(
        enabled=True,
        exporter=ExporterConfig("http://collector:4318/v1/logs", "http/protobuf"),
        loggers=tuple(loggers),
        level=level,
        set_logger_levels=set_levels,
    )
    return Settings(enabled=True, logs=logs)


@pytest.fixture
def exporter():
    return InMemoryLogRecordExporter()


def _context(settings, exporter):
    resource = otel.build_resource("netbox", {}, service_version="4.7.1", plugin_version="0.1.0", role="web")
    provider = otel.build_logger_provider(resource, exporter, synchronous=True)
    return Context(settings=settings, role="web", resource=resource, logger_provider=provider)


def _otel_handlers(name):
    return [h for h in logging.getLogger(name).handlers if isinstance(h, otel.AllowlistLoggingHandler)]


def test_enabled_follows_settings():
    assert LogsModule().enabled(_settings(["t.enabled"])) is True
    assert LogsModule().enabled(Settings(enabled=True)) is False


def test_install_attaches_one_handler_per_logger(exporter):
    ctx = _context(_settings(["t.logs.a", "t.logs.b"]), exporter)
    module = LogsModule()
    module.install(ctx)
    assert len(_otel_handlers("t.logs.a")) == 1
    assert len(_otel_handlers("t.logs.b")) == 1
    module.shutdown()


def test_second_install_does_not_duplicate_handlers(exporter):
    ctx = _context(_settings(["t.logs.dup"]), exporter)
    first, second = LogsModule(), LogsModule()
    first.install(ctx)
    second.install(ctx)
    assert len(_otel_handlers("t.logs.dup")) == 1
    first.shutdown()
    second.shutdown()


def test_records_are_exported(exporter):
    ctx = _context(_settings(["t.logs.export"]), exporter)
    module = LogsModule()
    module.install(ctx)
    lg = logging.getLogger("t.logs.export")
    lg.setLevel(logging.INFO)
    lg.info("exported line")
    assert [r.log_record.body for r in exporter.get_finished_logs()] == ["exported line"]
    module.shutdown()


def test_logger_levels_untouched_by_default(exporter):
    lg = logging.getLogger("t.logs.untouched")
    lg.setLevel(logging.NOTSET)
    module = LogsModule()
    module.install(_context(_settings(["t.logs.untouched"]), exporter))
    assert lg.level == logging.NOTSET
    module.shutdown()


def test_set_logger_levels_lowers_and_shutdown_restores(exporter):
    lg = logging.getLogger("t.logs.levels")
    lg.setLevel(logging.NOTSET)
    module = LogsModule()
    module.install(_context(_settings(["t.logs.levels"], set_levels=True), exporter))
    assert lg.level == logging.INFO
    module.shutdown()
    assert lg.level == logging.NOTSET


def test_set_logger_levels_does_not_raise_a_lower_level(exporter):
    lg = logging.getLogger("t.logs.lower")
    lg.setLevel(logging.DEBUG)
    module = LogsModule()
    module.install(_context(_settings(["t.logs.lower"], set_levels=True), exporter))
    assert lg.level == logging.DEBUG
    module.shutdown()


def test_shutdown_removes_handlers(exporter):
    module = LogsModule()
    module.install(_context(_settings(["t.logs.remove"]), exporter))
    module.shutdown()
    assert _otel_handlers("t.logs.remove") == []
