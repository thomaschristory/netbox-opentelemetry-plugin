import logging
import threading
import uuid
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from netbox_opentelemetry_plugin import middleware, otel


def _tracer():
    exporter = InMemorySpanExporter()
    provider = otel.build_tracer_provider(
        Resource.create({}), exporter, otel.build_sampler("always_on", 1.0), synchronous=True
    )
    return provider.get_tracer("t"), exporter


class _User:
    def __init__(self, name, authenticated=True):
        self._name, self.is_authenticated = name, authenticated

    def get_username(self):
        return self._name


def _run(request, response="ok"):
    mw = middleware.RequestSpanMiddleware(lambda req: response)
    tracer, exporter = _tracer()
    with tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER):
        result = mw(request)
    return result, exporter.get_finished_spans()[0]


def test_sets_request_id_and_enduser():
    rid = uuid.uuid4()
    result, span = _run(SimpleNamespace(id=rid, user=_User("alice")))
    assert result == "ok"
    assert span.attributes["netbox.request_id"] == str(rid)
    assert span.attributes["enduser.id"] == "alice"


def test_anonymous_user_gets_no_enduser():
    _, span = _run(SimpleNamespace(id=uuid.uuid4(), user=_User("", authenticated=False)))
    assert "enduser.id" not in span.attributes
    assert "netbox.request_id" in span.attributes


def test_user_is_read_after_the_view_ran():
    request = SimpleNamespace(id=uuid.uuid4(), user=_User("", authenticated=False))

    def view(req):
        req.user = _User("token-user")  # DRF authenticates during the view
        return "ok"

    mw = middleware.RequestSpanMiddleware(view)
    tracer, exporter = _tracer()
    with tracer.start_as_current_span("GET /api/", kind=SpanKind.SERVER):
        mw(request)
    assert exporter.get_finished_spans()[0].attributes["enduser.id"] == "token-user"


class _ExplodingUser:
    @property
    def is_authenticated(self):
        raise RuntimeError("session backend down")


def test_never_breaks_the_request_and_warns_once(caplog, monkeypatch):
    monkeypatch.setattr(middleware, "_warned", False)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        result, _ = _run(SimpleNamespace(id=uuid.uuid4(), user=_ExplodingUser()))
        _run(SimpleNamespace(id=uuid.uuid4(), user=_ExplodingUser()))
    assert result == "ok"
    warnings = [r for r in caplog.records if "request span" in r.getMessage()]
    assert len(warnings) == 1
    assert "session backend down" not in warnings[0].getMessage()


def test_no_recording_span_does_not_touch_the_user():
    class _Untouchable:
        @property
        def user(self):
            raise AssertionError("request.user must not be evaluated without a recording span")

        id = uuid.uuid4()

    mw = middleware.RequestSpanMiddleware(lambda req: "ok")
    assert mw(_Untouchable()) == "ok"


def test_middleware_logger_uses_the_plugin_logger_name():
    from netbox_opentelemetry_plugin import conf, middleware

    assert middleware.logger.name == conf.PLUGIN_LOGGER


def test_view_sees_the_server_span_context_on_the_request():
    seen = {}

    def view(req):
        seen["stored"] = getattr(req, otel.REQUEST_SPAN_CONTEXT_ATTR)
        return "ok"

    mw = middleware.RequestSpanMiddleware(view)
    tracer, exporter = _tracer()
    with tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER):
        mw(SimpleNamespace(id=uuid.uuid4(), user=_User("alice")))
    span = exporter.get_finished_spans()[0]
    assert seen["stored"] == span.get_span_context()


def test_sampled_out_span_context_is_stored_without_touching_the_user():
    sampled_out = trace.SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=False,
        trace_flags=trace.TraceFlags(trace.TraceFlags.DEFAULT),
    )

    class _Untouchable:
        @property
        def user(self):
            raise AssertionError("request.user must not be evaluated without a recording span")

    request = _Untouchable()
    mw = middleware.RequestSpanMiddleware(lambda req: "ok")
    with trace.use_span(trace.NonRecordingSpan(sampled_out)):
        assert mw(request) == "ok"
    assert getattr(request, otel.REQUEST_SPAN_CONTEXT_ATTR) == sampled_out


def test_no_current_span_stores_nothing():
    request = SimpleNamespace(id=uuid.uuid4())
    mw = middleware.RequestSpanMiddleware(lambda req: "ok")
    assert mw(request) == "ok"
    assert not hasattr(request, otel.REQUEST_SPAN_CONTEXT_ATTR)


class _SlottedRequest:
    __slots__ = ("id", "user")

    def __init__(self):
        self.id = uuid.uuid4()
        self.user = _User("alice")


def test_capture_failure_never_breaks_the_request_and_warns_once(caplog, monkeypatch):
    monkeypatch.setattr(middleware, "_warned", False)
    monkeypatch.setattr(middleware, "_capture_warned", False)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        result, span = _run(_SlottedRequest())
        _run(_SlottedRequest())
    assert result == "ok"
    assert span.attributes["enduser.id"] == "alice"
    warnings = [r for r in caplog.records if "trace context" in r.getMessage()]
    assert len(warnings) == 1
    assert "AttributeError" in warnings[0].getMessage()
    assert "_netbox_otel_span_context" not in warnings[0].getMessage()
    assert [r for r in caplog.records if "request span" in r.getMessage()] == []


def test_exception_from_the_view_propagates():
    def view(req):
        raise ValueError("view failed")

    mw = middleware.RequestSpanMiddleware(view)
    tracer, _ = _tracer()
    with tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER), pytest.raises(ValueError, match="view failed"):
        mw(SimpleNamespace(id=uuid.uuid4()))


def test_concurrent_requests_keep_their_own_span_context():
    tracer, _ = _tracer()
    barrier = threading.Barrier(2)
    results = {}

    def view(req):
        barrier.wait(timeout=5)
        return "ok"

    def worker(name):
        request = SimpleNamespace(id=uuid.uuid4())
        with tracer.start_as_current_span(name, kind=SpanKind.SERVER) as span:
            middleware.RequestSpanMiddleware(view)(request)
            results[name] = (span.get_span_context(), getattr(request, otel.REQUEST_SPAN_CONTEXT_ATTR))

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results["a"][0] == results["a"][1]
    assert results["b"][0] == results["b"][1]
    assert results["a"][1] != results["b"][1]
