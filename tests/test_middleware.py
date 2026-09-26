import logging
import uuid
from types import SimpleNamespace

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
