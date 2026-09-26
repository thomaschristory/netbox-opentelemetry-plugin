import contextlib
import http.server
import logging
import threading

import pytest
import requests
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from netbox_opentelemetry_plugin import conf, otel
from netbox_opentelemetry_plugin.modules import traces as traces_module
from netbox_opentelemetry_plugin.modules.base import Context
from netbox_opentelemetry_plugin.modules.traces import TracesModule, instrument_kwargs, instrumentations
from tests.otel_helpers import data_points, metric_names

RESOURCE = Resource.create({"service.name": "t"})


def _ctx(
    instrument=("django", "psycopg", "redis", "requests"),
    excluded=("/static/", "/api/status/"),
    meter_provider=None,
    traces_on=True,
):
    settings = conf.Settings(
        enabled=True,
        traces=conf.TracesConfig(enabled=traces_on, instrument=instrument, excluded_urls=excluded),
        metrics=conf.MetricsConfig(enabled=meter_provider is not None),
    )
    exporter = InMemorySpanExporter()
    provider = otel.SwitchableTracerProvider(
        otel.build_tracer_provider(RESOURCE, exporter, otel.build_sampler("always_on", 1.0), synchronous=True)
    )
    return (
        Context(
            settings=settings,
            role="web",
            resource=RESOURCE,
            tracer_provider=provider if traces_on else None,
            meter_provider=meter_provider,
        ),
        exporter,
    )


def _meter():
    reader = InMemoryMetricReader()
    return otel.SwitchableMeterProvider(otel.build_meter_provider(RESOURCE, [reader])), reader


class FakeInstrumentor:
    def __init__(self, already=False, fail=False, noop=False):
        self.is_instrumented_by_opentelemetry = already
        self.fail, self.noop = fail, noop
        self.kwargs = None
        self.uninstrumented = False

    def instrument(self, **kwargs):
        self.kwargs = kwargs
        if self.fail:
            raise RuntimeError("boom")
        if not self.noop:
            self.is_instrumented_by_opentelemetry = True

    def uninstrument(self):
        self.uninstrumented = True
        self.is_instrumented_by_opentelemetry = False


@pytest.fixture
def fakes(monkeypatch):
    table = {}
    monkeypatch.setattr(otel, "load_instrumentor", lambda name: table[name])
    return table


def test_instrument_kwargs_per_instrumentation():
    ctx, _ = _ctx()
    django = instrument_kwargs("django", ctx)
    assert django["tracer_provider"] is ctx.tracer_provider
    assert django["excluded_urls"] == "/static/,/api/status/"
    assert "meter_provider" in django
    assert instrument_kwargs("psycopg", ctx) == {
        "tracer_provider": ctx.tracer_provider,
        "enable_commenter": False,
        "capture_parameters": False,
    }
    assert instrument_kwargs("redis", ctx) == {"tracer_provider": ctx.tracer_provider}
    assert set(instrument_kwargs("requests", ctx)) == {"tracer_provider", "meter_provider"}


def test_install_instruments_listed_only_and_shutdown_uninstruments(fakes):
    fakes.update(django=FakeInstrumentor(), redis=FakeInstrumentor())
    ctx, _ = _ctx(instrument=("django", "redis"))
    module = TracesModule()
    module.install(ctx)
    assert fakes["django"].kwargs["tracer_provider"] is ctx.tracer_provider
    assert fakes["redis"].kwargs is not None
    module.shutdown()
    assert fakes["django"].uninstrumented and fakes["redis"].uninstrumented


def test_already_instrumented_is_left_alone(fakes, caplog):
    fakes.update(django=FakeInstrumentor(already=True))
    ctx, _ = _ctx(instrument=("django",))
    module = TracesModule()
    with caplog.at_level(logging.INFO, logger="netbox_opentelemetry_plugin"):
        module.install(ctx)
    assert fakes["django"].kwargs is None
    module.shutdown()
    assert fakes["django"].uninstrumented is False


def test_a_failing_instrumentor_disables_only_itself(fakes, caplog, monkeypatch):
    fakes.update(django=FakeInstrumentor(fail=True), psycopg=FakeInstrumentor(noop=True), redis=FakeInstrumentor())

    def missing(name):
        if name == "requests":
            raise ImportError("no module named requests")
        return fakes[name]

    monkeypatch.setattr(otel, "load_instrumentor", missing)
    ctx, _ = _ctx()
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        TracesModule().install(ctx)
    assert fakes["redis"].is_instrumented_by_opentelemetry is True
    messages = [r.getMessage() for r in caplog.records]
    assert any("django" in m for m in messages)
    assert any("psycopg" in m for m in messages)
    assert any("requests" in m for m in messages)
    assert len(messages) == 3


def test_install_sets_stable_http_semconv(fakes, monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    TracesModule().install(_ctx(instrument=())[0])
    import os

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"


def test_install_without_provider_does_nothing(fakes):
    fakes.update(django=FakeInstrumentor())
    ctx, _ = _ctx(instrument=("django",))
    ctx.tracer_provider = None
    TracesModule().install(ctx)
    assert fakes["django"].kwargs is None


def test_metrics_only_instruments_django_and_requests_with_a_noop_tracer():
    meter, _ = _meter()
    ctx, _ = _ctx(traces_on=False, meter_provider=meter)
    assert instrumentations(ctx) == ("django", "requests")
    for name in ("django", "requests"):
        kwargs = instrument_kwargs(name, ctx)
        assert kwargs["meter_provider"] is meter
        assert not isinstance(kwargs["tracer_provider"], otel.SwitchableTracerProvider)


def test_traces_and_metrics_union_keeps_traces_instrument_order():
    meter, _ = _meter()
    ctx, _ = _ctx(instrument=("psycopg", "requests"), meter_provider=meter)
    assert instrumentations(ctx) == ("psycopg", "requests", "django")
    assert instrument_kwargs("requests", ctx)["tracer_provider"] is ctx.tracer_provider
    # django is instrumented for metrics only: not in traces.instrument, so no spans.
    assert instrument_kwargs("django", ctx)["tracer_provider"] is not ctx.tracer_provider


def test_traces_only_keeps_the_noop_meter():
    ctx, _ = _ctx()
    assert instrumentations(ctx) == ("django", "psycopg", "redis", "requests")
    assert not isinstance(instrument_kwargs("django", ctx)["meter_provider"], otel.SwitchableMeterProvider)


def test_enabled_follows_traces_or_metrics():
    module = TracesModule()
    assert module.name == "instrumentation"
    assert module.enabled(conf.Settings(enabled=True, metrics=conf.MetricsConfig(enabled=True))) is True
    assert module.enabled(conf.Settings(enabled=True)) is False


def test_install_with_metrics_only_applies_http_instrumentors(fakes):
    fakes.update({name: FakeInstrumentor() for name in ("django", "psycopg", "redis", "requests")})
    meter, _ = _meter()
    ctx, _ = _ctx(traces_on=False, meter_provider=meter)
    TracesModule().install(ctx)
    assert fakes["django"].kwargs is not None and fakes["requests"].kwargs is not None
    assert fakes["psycopg"].kwargs is None and fakes["redis"].kwargs is None


def test_real_requests_instrumentation_records_client_duration_with_allowlisted_attributes(local_server, monkeypatch):
    # requests only: the Django instrumentor needs configured Django settings (covered in Task 7).
    monkeypatch.setattr(traces_module, "HTTP_METRIC_INSTRUMENTATIONS", ("requests",))
    meter, reader = _meter()
    ctx, _ = _ctx(instrument=(), traces_on=False, meter_provider=meter)
    module = TracesModule()
    module.install(ctx)
    try:
        requests.get(f"{local_server}/hook?token=s3cret", timeout=5)
        with contextlib.suppress(requests.RequestException):
            requests.get("http://127.0.0.1:9/refused", timeout=2)
    finally:
        module.shutdown()
    data = reader.get_metrics_data()
    points = data_points(data, "http.client.request.duration")
    assert points
    for point in points:
        assert set(point.attributes) <= {
            "http.request.method",
            "server.address",
            "http.response.status_code",
            "error.type",
        }
    assert any(p.attributes.get("error.type") for p in points)
    assert "s3cret" not in repr(data)
    assert metric_names(data) == {"http.client.request.duration"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _all_text(spans):
    parts = []
    for span in spans:
        parts += [str(v) for v in span.attributes.values()]
        parts.append(str(span.status.description))
        for event in span.events:
            parts += [str(v) for v in event.attributes.values()]
    return "\n".join(parts)


def test_real_requests_instrumentation_redacts_queries_and_headers(local_server, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST", ".*")
    ctx, exporter = _ctx(instrument=("requests",))
    module = TracesModule()
    module.install(ctx)
    try:
        tracer = ctx.tracer_provider.get_tracer("test")
        requests.get(f"{local_server}/root?token=unparented", timeout=5)  # parentless CLIENT: dropped
        with tracer.start_as_current_span("job", kind=SpanKind.CONSUMER):
            requests.get(f"{local_server}/hook?token=s3cret", headers={"X-Secret": "hunter2"}, timeout=5)
            with pytest.raises(requests.ConnectionError):
                requests.get("http://127.0.0.1:9/down?token=s3cret", timeout=2)
    finally:
        module.shutdown()
    spans = exporter.get_finished_spans()
    client = [s for s in spans if s.kind is SpanKind.CLIENT]
    assert len(client) == 2
    text = _all_text(spans)
    assert "s3cret" not in text and "unparented" not in text and "hunter2" not in text
    assert not any(k.startswith("http.request.header.") for s in spans for k in s.attributes)
    urls = [s.attributes.get("url.full") or s.attributes.get("http.url") for s in client]
    assert all(url.endswith("?REDACTED") for url in urls)


def test_logs_and_audit_events_inside_a_span_carry_its_ids():
    ctx, _ = _ctx(instrument=())
    log_exporter = InMemoryLogRecordExporter()
    log_provider = otel.build_logger_provider(RESOURCE, log_exporter, synchronous=True)
    handler = otel.build_logging_handler(log_provider, logging.INFO)
    lg = logging.getLogger("t.traces.corr")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        with ctx.tracer_provider.get_tracer("t").start_as_current_span("req", kind=SpanKind.SERVER) as span:
            lg.info("inside")
            otel.emit_event(log_provider, "scope", event_name="e", body="b", attributes={})
    finally:
        lg.removeHandler(handler)
    span_ctx = span.get_span_context()
    records = log_exporter.get_finished_logs()
    assert len(records) == 2
    for record in records:
        assert record.log_record.trace_id == span_ctx.trace_id
        assert record.log_record.span_id == span_ctx.span_id
