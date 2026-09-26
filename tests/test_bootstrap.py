import logging
import threading
import time

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from netbox_opentelemetry_plugin import bootstrap, conf, otel
from netbox_opentelemetry_plugin.conf import Settings
from tests.otel_helpers import RecordingMetricExporter, all_batches_points

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.boot"]}}
ARGV_WEB = ["granian", "netbox.granian:application"]
TRACES_USER = {**USER, "traces": {"enabled": True, "instrument": []}}


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
    assert any("log export disabled" in r.getMessage() for r in caplog.records)


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


def test_describe_redacts_trace_exporter_headers_and_endpoint():
    user = {
        "logs": {"enabled": False},
        "audit": {"enabled": False},
        "traces": {"enabled": True, "endpoint": "https://user:tracepass@tempo:4318/v1/traces"},
        "exporter": {"headers": {"x-api-key": "trace-secret-value"}},
    }
    settings = conf.resolve(user, {})
    assert settings.log_exporter is None and settings.traces.exporter is not None
    exc = RuntimeError("POST https://user:tracepass@tempo:4318/v1/traces failed with key trace-secret-value")
    text = bootstrap._describe(exc, settings)
    assert "tracepass" not in text
    assert "trace-secret-value" not in text


@pytest.fixture
def span_exporter(monkeypatch):
    exp = InMemorySpanExporter()
    monkeypatch.setattr(otel, "build_span_exporter", lambda cfg: exp)
    return exp


def test_traces_install_builds_an_owned_switchable_provider(exporter, span_exporter):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    assert isinstance(ctx.tracer_provider, otel.SwitchableTracerProvider)
    assert bootstrap._state.owns_tracer_provider is True
    assert [m.name for m in bootstrap._state.modules] == ["logs", "audit", "instrumentation", "rq"]
    with ctx.tracer_provider.get_tracer("t").start_as_current_span("s"):
        pass
    assert bootstrap.force_flush(2.0) is True
    (span,) = span_exporter.get_finished_spans()
    assert span.resource.attributes["netbox.process.role"] == "web"


def test_traces_off_by_default_installs_no_tracing(exporter):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert ctx.tracer_provider is None
    assert [m.name for m in bootstrap._state.modules] == ["logs", "audit"]


@pytest.mark.parametrize("argv", [["manage.py", "migrate"], ["manage.py", "nbshell"]])
def test_management_commands_get_no_traces(exporter, span_exporter, argv):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=argv)
    assert ctx.tracer_provider is None
    assert "instrumentation" not in [m.name for m in bootstrap._state.modules]


def test_rqworker_gets_traces(exporter, span_exporter):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=["/opt/netbox/netbox/manage.py", "rqworker"])
    assert ctx.tracer_provider is not None


def test_external_tracer_provider_is_reused(exporter, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider

    external = TracerProvider()
    monkeypatch.setattr(otel, "existing_tracer_provider", lambda: external)
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    assert ctx.tracer_provider is external
    assert bootstrap._state.owns_tracer_provider is False
    bootstrap.shutdown()
    # Not ours to shut down: it still hands out recording spans.
    assert external.get_tracer("t").start_span("x").is_recording()


def test_span_exporter_failure_disables_traces_only(exporter, monkeypatch, caplog):
    def boom(cfg):
        raise OSError("certificate unreadable")

    monkeypatch.setattr(otel, "build_span_exporter", boom)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    assert ctx.tracer_provider is None
    assert ctx.logger_provider is not None
    assert any("trace export disabled" in r.getMessage() for r in caplog.records)


def test_force_flush_flushes_spans_and_logs(exporter, span_exporter):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    logging.getLogger("t.boot").setLevel(logging.INFO)
    with ctx.tracer_provider.get_tracer("t").start_as_current_span("s"):
        logging.getLogger("t.boot").info("in span")
    assert bootstrap.force_flush(2.0) is True
    assert len(span_exporter.get_finished_spans()) == 1
    assert [r.log_record.body for r in exporter.get_finished_logs()] == ["in span"]


def test_force_flush_reports_a_slow_tracer_flush(exporter, span_exporter, monkeypatch):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    release = threading.Event()
    monkeypatch.setattr(ctx.tracer_provider, "force_flush", lambda timeout_millis=30000: release.wait(5))
    started = time.monotonic()
    try:
        assert bootstrap.force_flush(0.2) is False
    finally:
        release.set()
    assert time.monotonic() - started < 1.5


def test_shutdown_shuts_down_the_owned_tracer_provider(exporter, span_exporter, monkeypatch):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    calls = []
    monkeypatch.setattr(ctx.tracer_provider, "shutdown", lambda: calls.append("tracer"))
    bootstrap.shutdown()
    assert calls == ["tracer"]


def test_rebuild_shuts_down_the_new_child_logger_provider_when_tracer_rebuild_fails(
    exporter, span_exporter, monkeypatch
):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    inherited_provider = ctx.logger_provider
    inherited_calls = []
    monkeypatch.setattr(inherited_provider, "shutdown", lambda: inherited_calls.append("inherited"))

    new_provider_calls = []

    class _FakeChildProvider:
        def shutdown(self) -> None:
            new_provider_calls.append("new")

    monkeypatch.setattr(otel, "build_logger_provider", lambda *a, **k: _FakeChildProvider())

    def boom(cfg):
        raise OSError("certificate unreadable")

    monkeypatch.setattr(otel, "build_span_exporter", boom)

    # Force the next reinit_after_fork() call to actually rebuild, as if this process had forked.
    bootstrap._state.pid = -1
    bootstrap.reinit_after_fork()

    assert new_provider_calls == ["new"]
    assert inherited_calls == []
    # The inherited (parent-owned-in-this-child) provider is never assigned away by _rebuild_for_child
    # itself; reinit_after_fork's own except-handler is what stops using it (see test_fork.py).
    assert ctx.logger_provider is None


METRICS_USER = {**USER, "metrics": {"enabled": True, "export_interval": 3600}}


@pytest.fixture
def metric_exporter(monkeypatch):
    exp = RecordingMetricExporter()
    monkeypatch.setattr(otel, "build_metric_exporter", lambda cfg: exp)
    return exp


def test_metrics_install_builds_an_owned_pipeline_behind_a_switchable(exporter, metric_exporter):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    assert isinstance(ctx.meter_provider, otel.SwitchableMeterProvider)
    state = bootstrap._state
    assert state.metrics_pipeline is not None
    assert ctx.meter_provider.delegate is state.metrics_pipeline.provider


def test_metrics_off_by_default_installs_no_meter_provider(exporter):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert ctx.meter_provider is None
    assert bootstrap._state.metrics_pipeline is None


@pytest.mark.parametrize("argv", [["manage.py", "migrate"], ["manage.py", "nbshell"]])
def test_management_commands_get_no_metrics(exporter, metric_exporter, argv):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=argv)
    assert ctx.meter_provider is None


def test_rqworker_gets_metrics(exporter, metric_exporter):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=["/opt/netbox/netbox/manage.py", "rqworker"])
    assert ctx.meter_provider is not None


def test_external_meter_provider_is_reused_and_wrapped(exporter, monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider

    external = MeterProvider(shutdown_on_exit=False)
    monkeypatch.setattr(otel, "existing_meter_provider", lambda: external)
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    assert isinstance(ctx.meter_provider, otel.SwitchableMeterProvider)
    assert ctx.meter_provider.delegate is external
    assert bootstrap._state.metrics_pipeline is None
    bootstrap.shutdown()
    external.get_meter("t").create_counter("still.works").add(1)  # not shut down by the plugin


def test_metric_exporter_failure_disables_metrics_only(exporter, monkeypatch, caplog):
    def boom(cfg):
        raise RuntimeError("bad cert")

    monkeypatch.setattr(otel, "build_metric_exporter", boom)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    assert ctx.meter_provider is None and ctx.logger_provider is not None
    assert any("metric export disabled" in r.getMessage() for r in caplog.records)


def test_force_flush_exports_owned_metrics(exporter, metric_exporter):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    ctx.meter_provider.get_meter("t").create_counter("netbox.object_changes").add(2)
    assert bootstrap.force_flush(2.0) is True
    assert [p.value for p in all_batches_points(metric_exporter, "netbox.object_changes")] == [2]


def test_force_flush_runs_providers_in_parallel(exporter, metric_exporter, monkeypatch):
    ctx = bootstrap.install({**METRICS_USER, "traces": {"enabled": True, "instrument": []}}, env={}, argv=ARGV_WEB)
    started = []
    barrier = threading.Barrier(3, timeout=2)

    def slow_flush(name):
        def flush(timeout_millis=0):
            started.append(name)
            barrier.wait()  # only returns if all three flushes run at the same time
            return True

        return flush

    monkeypatch.setattr(ctx.logger_provider, "force_flush", slow_flush("logs"))
    monkeypatch.setattr(ctx.tracer_provider, "force_flush", slow_flush("traces"))
    monkeypatch.setattr(bootstrap._state.metrics_pipeline, "force_flush", slow_flush("metrics"))
    assert bootstrap.force_flush(3.0) is True
    assert sorted(started) == ["logs", "metrics", "traces"]


def test_shutdown_order_modules_metrics_traces_logs(exporter, metric_exporter, monkeypatch):
    ctx = bootstrap.install({**METRICS_USER, "traces": {"enabled": True, "instrument": []}}, env={}, argv=ARGV_WEB)
    order = []
    state = bootstrap._state

    def recording(name, original):
        def wrapper(*args, **kwargs):
            order.append(name)
            return original(*args, **kwargs)

        return wrapper

    for module in state.modules:
        monkeypatch.setattr(module, "shutdown", recording(module.name, module.shutdown))
    # Provider labels differ from the module names ("logs", "traces", ...) recorded above.
    monkeypatch.setattr(state.metrics_pipeline, "shutdown", recording("p:metrics", state.metrics_pipeline.shutdown))
    monkeypatch.setattr(ctx.tracer_provider, "shutdown", recording("p:traces", ctx.tracer_provider.shutdown))
    monkeypatch.setattr(ctx.logger_provider, "shutdown", recording("p:logs", ctx.logger_provider.shutdown))
    bootstrap.shutdown()
    providers = [name for name in order if name.startswith("p:")]
    assert providers == ["p:metrics", "p:traces", "p:logs"]
    assert order.index("p:metrics") > max(order.index(m.name) for m in state.modules)
    assert isinstance(ctx.meter_provider.delegate, type(otel.noop_meter_provider()))


def test_shutdown_exports_pending_metrics_once(exporter, metric_exporter):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    ctx.meter_provider.get_meter("t").create_counter("netbox.object_changes").add(1)
    bootstrap.shutdown()
    assert [p.value for p in all_batches_points(metric_exporter, "netbox.object_changes")] == [1]
    assert metric_exporter.shutdown_called


def test_describe_redacts_metric_exporter_headers():
    user = {"exporter": {"endpoint": "http://c:4318", "headers": {"k": "s3cret-metrics"}}, "metrics": {"enabled": True}}
    settings = conf.resolve(user, {})
    assert "s3cret-metrics" not in bootstrap._describe(RuntimeError("header s3cret-metrics rejected"), settings)


def test_runtime_module_is_a_candidate_only_with_metrics(exporter, metric_exporter, monkeypatch):
    from netbox_opentelemetry_plugin.modules.runtime import RuntimeModule

    monkeypatch.setattr(RuntimeModule, "install", lambda self, ctx: None)
    bootstrap.install({**METRICS_USER, "metrics": {**METRICS_USER["metrics"], "runtime": True}}, env={}, argv=ARGV_WEB)
    assert any(m.name == "runtime" for m in bootstrap._state.modules)
    bootstrap.shutdown()
    bootstrap._state = None
    bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert not any(m.name == "runtime" for m in bootstrap._state.modules)
