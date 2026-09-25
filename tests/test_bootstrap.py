import logging

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from netbox_opentelemetry_plugin import bootstrap, conf, otel
from netbox_opentelemetry_plugin.conf import Settings

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.boot"]}}
ARGV_WEB = ["granian", "netbox.granian:application"]


@pytest.fixture(autouse=True)
def reset_state():
    bootstrap.shutdown()
    bootstrap._state = None
    yield
    bootstrap.shutdown()
    bootstrap._state = None


@pytest.fixture
def exporter(monkeypatch):
    exp = InMemoryLogRecordExporter()
    monkeypatch.setattr(otel, "build_log_exporter", lambda cfg: exp)
    return exp


def _otel_handlers(name):
    return [h for h in logging.getLogger(name).handlers if isinstance(h, otel.AllowlistLoggingHandler)]


@pytest.mark.parametrize(
    ("argv", "env", "role"),
    [
        (["granian", "netbox.granian:application"], {}, "web"),
        (["/opt/netbox/venv/bin/gunicorn", "netbox.wsgi"], {}, "web"),
        (["/opt/netbox/netbox/manage.py", "rqworker", "high"], {}, "rqworker"),
        (["manage.py", "migrate"], {}, "management"),
        (["manage.py", "runserver"], {}, "runserver_parent"),
        (["manage.py", "runserver"], {"RUN_MAIN": "true"}, "web"),
        (["manage.py", "runserver", "--noreload"], {}, "web"),
        ([], {}, "web"),
    ],
)
def test_detect_role(argv, env, role):
    assert bootstrap.detect_role(argv, env) == role


def test_install_exports_logs(exporter):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB, netbox_version="4.7.1")
    assert ctx is not None
    lg = logging.getLogger("t.boot")
    lg.setLevel(logging.INFO)
    lg.info("from bootstrap")
    ctx.logger_provider.force_flush()
    records = exporter.get_finished_logs()
    assert [r.log_record.body for r in records] == ["from bootstrap"]
    assert records[0].resource.attributes["service.version"] == "4.7.1"
    assert records[0].resource.attributes["netbox.process.role"] == "web"


def test_install_is_idempotent_per_process(exporter):
    first = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    second = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert first is second
    assert len(_otel_handlers("t.boot")) == 1


def test_shutdown_removes_handler(exporter):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)
    bootstrap.shutdown()
    assert _otel_handlers("t.boot") == []


def test_missing_endpoint_warns_once_and_installs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        ctx = bootstrap.install({"logs": {"loggers": ["t.boot"]}}, env={}, argv=ARGV_WEB)
    assert ctx is not None
    assert ctx.logger_provider is None
    assert _otel_handlers("t.boot") == []
    warnings = [r for r in caplog.records if r.name == "netbox_opentelemetry_plugin"]
    assert len(warnings) == 1
    assert "no endpoint" in warnings[0].getMessage()


def test_exporter_failure_warns_and_continues(monkeypatch, caplog):
    def boom(cfg):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(otel, "build_log_exporter", boom)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert ctx is not None
    assert ctx.logger_provider is None
    assert _otel_handlers("t.boot") == []
    assert any("logs disabled" in r.getMessage() for r in caplog.records)


def test_runserver_parent_installs_nothing(exporter):
    assert bootstrap.install(USER, env={}, argv=["manage.py", "runserver"]) is None
    assert _otel_handlers("t.boot") == []


def test_disabled_plugin_installs_nothing(exporter):
    assert bootstrap.install({**USER, "enabled": False}, env={}, argv=ARGV_WEB) is None
    assert _otel_handlers("t.boot") == []


def test_existing_global_provider_is_reused_and_not_shut_down(monkeypatch, exporter):
    external = otel.build_logger_provider(
        otel.build_resource("ext", {}, service_version="x", plugin_version="x", role="web"),
        InMemoryLogRecordExporter(),
        synchronous=True,
    )
    calls = []
    monkeypatch.setattr(external, "shutdown", lambda *a, **k: calls.append("shutdown"))
    monkeypatch.setattr(otel, "existing_logger_provider", lambda: external)
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert ctx.logger_provider is external
    bootstrap.shutdown()
    assert calls == []


def test_debug_output_masks_headers(exporter, caplog):
    user = {**USER, "exporter": {"endpoint": "http://collector:4318", "headers": {"authorization": "TOPSECRET"}}}
    with caplog.at_level(logging.DEBUG, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(user, env={}, argv=ARGV_WEB)
    assert "TOPSECRET" not in caplog.text
    assert "resolved config" in caplog.text


def test_describe_never_raises_when_str_fails():
    class Boom(Exception):
        def __str__(self):
            raise RuntimeError("no string for you")

    assert bootstrap._describe(Boom(), Settings(enabled=True)) == "Boom"


def test_describe_redacts_url_credentials_in_endpoint():
    settings = conf.resolve({"logs": {"endpoint": "https://user:SECRETPW@collector:4318/v1/logs"}}, env={})
    exc = RuntimeError("connection failed: https://user:SECRETPW@collector:4318/v1/logs")
    message = bootstrap._describe(exc, settings)
    assert "SECRETPW" not in message
    assert "***@collector:4318" in message


def test_exporter_failure_warning_redacts_header_values(monkeypatch, caplog):
    user = {
        **USER,
        "exporter": {"endpoint": "http://collector:4318", "headers": {"authorization": "Bearer TOPSECRET"}},
    }

    def boom(cfg):
        raise RuntimeError("bad header Bearer TOPSECRET")

    monkeypatch.setattr(otel, "build_log_exporter", boom)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(user, env={}, argv=ARGV_WEB)
    assert "TOPSECRET" not in caplog.text
    assert "***" in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize(
    "message",
    [
        "Failed to establish a new connection to 'user:SECRETPW@collector' port 4318",
        "cannot reach https://user:SECRETPW@collector:4318/v1/logs",
        "cannot reach https://user:SECRET%40PW@collector:4318",
    ],
)
def test_describe_scrubs_userinfo_patterns(message):
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    text = bootstrap._describe(RuntimeError(message), settings)
    assert "SECRET" not in text
    assert "***@collector" in text


def test_describe_leaves_plain_host_port_alone():
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    text = bootstrap._describe(RuntimeError("connection refused: collector:4318"), settings)
    assert text == "RuntimeError: connection refused: collector:4318"
