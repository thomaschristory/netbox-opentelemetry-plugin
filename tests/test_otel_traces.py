import os
import threading

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from netbox_opentelemetry_plugin import otel
from netbox_opentelemetry_plugin.conf import ExporterConfig

RESOURCE = Resource.create({"service.name": "t"})


def _provider(exporter, sampler=None):
    return otel.build_tracer_provider(
        RESOURCE, exporter, sampler or otel.build_sampler("always_on", 1.0), synchronous=True
    )


def test_build_span_exporter_http_and_grpc():
    http = otel.build_span_exporter(ExporterConfig(endpoint="http://c:4318/v1/traces", protocol="http/protobuf"))
    assert type(http).__module__ == "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    grpc = otel.build_span_exporter(ExporterConfig(endpoint="http://c:4317", protocol="grpc", insecure=True))
    assert type(grpc).__module__ == "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    http.shutdown()
    grpc.shutdown()


@pytest.mark.parametrize(
    "name",
    [
        "always_on",
        "always_off",
        "traceidratio",
        "parentbased_always_on",
        "parentbased_always_off",
        "parentbased_traceidratio",
    ],
)
def test_build_sampler_wraps_every_known_sampler(name):
    sampler = otel.build_sampler(name, 0.5)
    assert isinstance(sampler, otel.RootClientSpanFilter)
    assert "RootClientSpanFilter" in sampler.get_description()


def test_parentless_client_spans_are_dropped_but_children_kept():
    exporter = InMemorySpanExporter()
    tracer = _provider(exporter).get_tracer("t")
    tracer.start_span("root-client", kind=SpanKind.CLIENT).end()
    tracer.start_span("root-internal").end()
    with tracer.start_as_current_span("server", kind=SpanKind.SERVER):
        tracer.start_span("child-client", kind=SpanKind.CLIENT).end()
    names = sorted(s.name for s in exporter.get_finished_spans())
    assert names == ["child-client", "root-internal", "server"]


def test_ratio_sampler_arg_is_used():
    exporter = InMemorySpanExporter()
    tracer = _provider(exporter, otel.build_sampler("traceidratio", 0.0)).get_tracer("t")
    tracer.start_span("dropped", kind=SpanKind.SERVER).end()
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://h/p?token=abc", "http://h/p?REDACTED"),
        ("http://h/p?token=abc#frag", "http://h/p?REDACTED"),
        ("/api/dcim/sites/?q=x&limit=1", "/api/dcim/sites/?REDACTED"),
        ("http://h/p", "http://h/p"),
        ("http://h/p?", "http://h/p?"),
    ],
)
def test_redact_url_query(url, expected):
    assert otel.redact_url_query(url) == expected


def test_scrub_query_strings_in_free_text():
    text = "Max retries exceeded with url: /hook?token=s3cret (Caused by X)\nGET http://h:9/a/b?x=1&y=2 failed"
    scrubbed = otel.scrub_query_strings(text)
    assert "s3cret" not in scrubbed and "x=1" not in scrubbed
    assert "/hook?REDACTED (Caused by X)" in scrubbed
    assert "http://h:9/a/b?REDACTED failed" in scrubbed
    assert otel.scrub_query_strings("why? because") == "why? because"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("url: /x?tok=ab(cd) z", "cd"),
        ('http://h/p?tok="q"sec', "sec"),
        ("http://[::1]?tok=sec", "sec"),
        ("GET /p~?tok=sec", "sec"),
    ],
)
def test_scrub_query_strings_does_not_leak_around_punctuation(text, secret):
    scrubbed = otel.scrub_query_strings(text)
    assert secret not in scrubbed


def _span(attributes, status=None, events=()):
    ctx = trace.SpanContext(trace_id=1, span_id=2, is_remote=False, trace_flags=trace.TraceFlags(1))
    return ReadableSpan(
        name="s",
        context=ctx,
        resource=RESOURCE,
        attributes=attributes,
        events=events,
        kind=SpanKind.CLIENT,
        status=status or Status(StatusCode.UNSET),
        start_time=1,
        end_time=2,
    )


def test_redact_span_rewrites_urls_drops_headers_and_scrubs_text():
    span = _span(
        {
            "url.full": "http://hook/x?token=a",
            "http.url": "http://hook/x?token=a",
            "http.target": "/x?token=a",
            "url.query": "token=a",
            "url.path": "/x",
            "http.request.header.authorization": ("Bearer a",),
            "http.response.header.set_cookie": ("sid=a",),
            "http.request.method": "POST",
        },
        status=Status(StatusCode.ERROR, "ConnectionError: url: /x?token=a"),
        events=[Event("exception", {"exception.message": "url: /x?token=a", "exception.escaped": "False"}, 3)],
    )
    red = otel.redact_span(span)
    assert dict(red.attributes) == {
        "url.full": "http://hook/x?REDACTED",
        "http.url": "http://hook/x?REDACTED",
        "http.target": "/x?REDACTED",
        "url.query": "REDACTED",
        "url.path": "/x",
        "http.request.method": "POST",
    }
    assert red.status.status_code is StatusCode.ERROR
    assert red.status.description == "ConnectionError: url: /x?REDACTED"
    assert dict(red.events[0].attributes)["exception.message"] == "url: /x?REDACTED"
    assert red.events[0].timestamp == 3
    for field in ("context", "parent", "resource", "kind", "start_time", "end_time", "name"):
        assert getattr(red, field) == getattr(span, field)


def test_redact_span_returns_the_same_object_when_clean():
    span = _span({"url.path": "/x", "http.request.method": "GET"})
    assert otel.redact_span(span) is span


class _Recorder:
    def __init__(self):
        self.ended = []

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        self.ended.append(span)

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def test_processor_drops_a_span_it_cannot_redact(monkeypatch):
    recorder = _Recorder()
    processor = otel.RedactingSpanProcessor(recorder)

    def boom(span):
        raise RuntimeError("redaction failed")

    monkeypatch.setattr(otel, "redact_span", boom)
    processor.on_end(_span({"url.query": "a=1"}))
    assert recorder.ended == []


def test_provider_exports_redacted_spans_with_header_env_set(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")
    exporter = InMemorySpanExporter()
    tracer = _provider(exporter).get_tracer("t")
    with tracer.start_as_current_span("server", kind=SpanKind.SERVER) as span:
        span.set_attribute("http.request.header.cookie", ("sessionid=abc",))
        span.set_attribute("url.query", "token=abc")
    (exported,) = exporter.get_finished_spans()
    assert "http.request.header.cookie" not in exported.attributes
    assert exported.attributes["url.query"] == "REDACTED"


def test_switchable_provider_follows_its_delegate():
    first, second = InMemorySpanExporter(), InMemorySpanExporter()
    switchable = otel.SwitchableTracerProvider(_provider(first))
    tracer = switchable.get_tracer("t")
    with tracer.start_as_current_span("one", kind=SpanKind.SERVER):
        pass
    switchable.set_delegate(_provider(second))
    with tracer.start_as_current_span("two", kind=SpanKind.SERVER):
        pass
    switchable.set_delegate(otel.noop_tracer_provider())
    with tracer.start_as_current_span("three", kind=SpanKind.SERVER) as span:
        assert not span.is_recording()
    assert [s.name for s in first.get_finished_spans()] == ["one"]
    assert [s.name for s in second.get_finished_spans()] == ["two"]
    assert switchable.force_flush() is True
    switchable.shutdown()


def test_switchable_tracer_start_span_uses_current_delegate():
    exporter = InMemorySpanExporter()
    switchable = otel.SwitchableTracerProvider(otel.noop_tracer_provider())
    tracer = switchable.get_tracer("t")
    switchable.set_delegate(_provider(exporter))
    tracer.start_span("late", kind=SpanKind.SERVER).end()
    assert [s.name for s in exporter.get_finished_spans()] == ["late"]


def test_existing_tracer_provider_ignores_the_default_proxy():
    assert otel.existing_tracer_provider() is None or isinstance(otel.existing_tracer_provider(), TracerProvider)


def test_inject_and_extract_round_trip_without_baggage():
    exporter = InMemorySpanExporter()
    tracer = _provider(exporter).get_tracer("t")
    assert otel.inject_trace_context() == {}
    from opentelemetry import baggage, context

    token = context.attach(baggage.set_baggage("user", "secret"))
    try:
        with tracer.start_as_current_span("parent", kind=SpanKind.SERVER) as parent:
            carrier = otel.inject_trace_context()
    finally:
        context.detach(token)
    assert set(carrier) == {"traceparent"}
    extracted = otel.extract_trace_context(carrier)
    child = otel.start_span(
        otel.SwitchableTracerProvider(_provider(exporter)),
        "s",
        "1",
        "child",
        kind=otel.CONSUMER,
        parent=extracted,
        attributes={"a": "b"},
    )
    child.end()
    assert child.get_span_context().trace_id == parent.get_span_context().trace_id
    assert child.parent.span_id == parent.get_span_context().span_id


@pytest.mark.parametrize(
    "carrier", [None, "00-abc", ["traceparent"], {"traceparent": 5}, {"traceparent": "garbage"}, {}]
)
def test_extract_tolerates_foreign_carriers(carrier):
    ctx = otel.extract_trace_context(carrier)
    assert ctx is None or not trace.get_current_span(ctx).get_span_context().is_valid


def test_span_helpers_record_failure_and_activation():
    exporter = InMemorySpanExporter()
    provider = otel.SwitchableTracerProvider(_provider(exporter))
    span = otel.start_span(provider, "scope", "1", "job", kind=otel.CONSUMER, parent=None, attributes={"k": "v"})
    token = otel.activate(span)
    try:
        assert otel.current_span_is_recording() is True
        otel.annotate_current_span({"netbox.request_id": "r1"})
    finally:
        otel.deactivate(token)
    assert otel.current_span_is_recording() is False
    otel.record_failure(span, ValueError("bad"))
    span.end()
    (exported,) = exporter.get_finished_spans()
    assert exported.kind is SpanKind.CONSUMER
    assert exported.status.status_code is StatusCode.ERROR
    assert exported.attributes["netbox.request_id"] == "r1"
    assert exported.events[0].name == "exception"


def test_set_error_marks_status():
    exporter = InMemorySpanExporter()
    span = otel.start_span(
        otel.SwitchableTracerProvider(_provider(exporter)),
        "s",
        "1",
        "job",
        kind=otel.CONSUMER,
        parent=None,
        attributes={},
    )
    otel.set_error(span, "job failed")
    span.end()
    assert exporter.get_finished_spans()[0].status.description == "job failed"


def test_annotate_without_a_span_is_a_noop():
    otel.annotate_current_span({"a": "b"})


def test_load_instrumentor_returns_known_instrumentors():
    for name in ("django", "psycopg", "redis", "requests"):
        instrumentor = otel.load_instrumentor(name)
        assert hasattr(instrumentor, "is_instrumented_by_opentelemetry")
    with pytest.raises(KeyError):
        otel.load_instrumentor("celery")


def test_prefer_stable_http_semconv_respects_operator_choice(monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    otel.prefer_stable_http_semconv()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")
    otel.prefer_stable_http_semconv()
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"


def test_force_flush_on_switchable_is_thread_safe_enough():
    switchable = otel.SwitchableTracerProvider(_provider(InMemorySpanExporter()))
    threads = [threading.Thread(target=switchable.force_flush) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
