import logging
import threading
import time

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
    bootstrap._next_fork_role = None
    yield
    bootstrap.shutdown()
    bootstrap._state = None
    bootstrap._next_fork_role = None


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


def test_describe_is_fast_on_pathological_input():
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    message = "a" * 100_000 + ":" + "b" * 100_000
    start = time.monotonic()
    text = bootstrap._describe(RuntimeError(message), settings)
    assert time.monotonic() - start < 1
    assert text.endswith("[truncated]")


def test_describe_truncates_long_messages():
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    message = "x" * 100_000
    text = bootstrap._describe(RuntimeError(message), settings)
    assert len(text) <= 2000 + len(" [truncated]") + len("RuntimeError: ")


def test_describe_header_value_straddling_the_cut_is_not_leaked():
    settings = conf.resolve(
        {"exporter": {"endpoint": "http://collector:4318", "headers": {"authorization": "Bearer TOPSECRET"}}}, {}
    )
    # "x " * 994 is 1988 chars; "Bearer TOPSECRET" (16 chars) then spans indices 1988-2003,
    # straddling the old 2000-char truncation cut.
    message = "x " * 994 + "Bearer TOPSECRET tail"
    text = bootstrap._describe(RuntimeError(message), settings)
    assert "TOP" not in text
    assert "Bearer TOPSEC" not in text


def test_describe_userinfo_straddling_the_cut_is_not_leaked():
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    # "x " * 990 is 1980 chars; "SECRETPW" then spans indices 1993-2000, straddling the old
    # 2000-char truncation cut.
    message = "x " * 990 + "https://user:SECRETPW@host tail"
    text = bootstrap._describe(RuntimeError(message), settings)
    assert "SECR" not in text


@pytest.mark.parametrize("n", [1970, 1978, 1985, 1995, 2005])
def test_describe_userinfo_straddling_cut_without_whitespace_is_not_leaked(n):
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    message = "a" * n + "/https://user:SECRETPW@host tail"
    text = bootstrap._describe(RuntimeError(message), settings)
    assert "SECR" not in text
    assert "[truncated]" in text


def test_describe_redacts_token_only_userinfo():
    settings = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    text = bootstrap._describe(RuntimeError("cannot reach https://ABCTOKEN123@collector:4318"), settings)
    assert "ABCTOKEN" not in text
    assert "https://***@collector" in text


def test_force_flush_without_state_is_true():
    assert bootstrap.force_flush(1.0) is True


def test_force_flush_flushes_provider(exporter):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)
    lg = logging.getLogger("t.boot")
    lg.setLevel(logging.INFO)
    lg.info("flush me")
    assert bootstrap.force_flush(2.0) is True
    assert [r.log_record.body for r in exporter.get_finished_logs()] == ["flush me"]


def test_force_flush_gives_up_after_timeout(exporter, monkeypatch):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    release = threading.Event()
    monkeypatch.setattr(ctx.logger_provider, "force_flush", lambda timeout_millis=None: release.wait(5))
    started = time.monotonic()
    assert bootstrap.force_flush(0.2) is False
    assert time.monotonic() - started < 1.0
    release.set()


def test_force_flush_never_raises(exporter, monkeypatch):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def boom(timeout_millis=None):
        raise RuntimeError("flush failed")

    monkeypatch.setattr(ctx.logger_provider, "force_flush", boom)
    assert bootstrap.force_flush(1.0) is True


def test_force_flush_passes_timeout_millis_to_provider(exporter, monkeypatch):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    received = {}

    def stub(timeout_millis=None):
        received["timeout_millis"] = timeout_millis

    monkeypatch.setattr(ctx.logger_provider, "force_flush", stub)
    assert bootstrap.force_flush(2.5) is True
    assert received["timeout_millis"] == 2500


def test_force_flush_returns_false_when_thread_cannot_start(exporter, monkeypatch):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def boom(self, *args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    assert bootstrap.force_flush(1.0) is False
