import logging
from types import SimpleNamespace

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from rq.queue import Queue
from rq.worker.base import BaseWorker
from rq.worker.worker_classes import Worker

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.modules import rq as rq_module

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.rq"]}}
TRACES = {**USER, "traces": {"enabled": True, "instrument": []}}
ARGV_RQ = ["/opt/netbox/netbox/manage.py", "rqworker"]
ARGV_WEB = ["granian", "netbox.granian:application"]


@pytest.fixture
def stubs(monkeypatch):
    """Replace the real rq methods with stubs for the test, restoring them explicitly afterwards.

    Restoration is explicit (not monkeypatch) so it runs after bootstrap.shutdown() has unwrapped.
    """
    real_perform = BaseWorker.__dict__["perform_job"]
    real_fork = Worker.__dict__["fork_work_horse"]
    real_enqueue = Queue.__dict__["enqueue_job"]
    calls = {"perform": [], "fork": [], "enqueue": []}

    def perform_job(self, job, queue):
        calls["perform"].append(job)
        if job == "raise":
            raise RuntimeError("job failed")
        return True

    def fork_work_horse(self, job, queue):
        calls["fork"].append(bootstrap._next_fork_role)

    def enqueue_job(self, job, pipeline=None, at_front=False, unique=False):
        calls["enqueue"].append(dict(job.meta) if isinstance(job.meta, dict) else job.meta)
        return job

    BaseWorker.perform_job = perform_job
    Worker.fork_work_horse = fork_work_horse
    Queue.enqueue_job = enqueue_job
    exporter = InMemoryLogRecordExporter()
    monkeypatch.setattr(otel, "build_log_exporter", lambda cfg: exporter)
    bootstrap.shutdown()
    bootstrap._state = None
    rq_module._warned.clear()
    yield calls
    bootstrap.shutdown()
    bootstrap._state = None
    BaseWorker.perform_job = real_perform
    Worker.fork_work_horse = real_fork
    Queue.enqueue_job = real_enqueue


@pytest.fixture
def flushes(monkeypatch):
    recorded = []

    def fake_flush(timeout):
        recorded.append(timeout)
        return True

    monkeypatch.setattr(bootstrap, "force_flush", fake_flush)
    return recorded


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(otel, "build_span_exporter", lambda cfg: exporter)
    return exporter


def _horse():
    return SimpleNamespace(is_horse=True)


def _flush(spans):
    bootstrap.force_flush(2.0)
    return spans.get_finished_spans()


class FakeJob:
    def __init__(self, func_name="app.tasks.work", meta=None, kwargs=None, broken=False, job_id="j-1"):
        self.id = job_id
        self.meta = {} if meta is None else meta
        self._func_name, self._kwargs, self._broken = func_name, kwargs or {}, broken

    @property
    def func_name(self):
        if self._broken:
            raise ValueError("cannot unpickle")
        return self._func_name

    @property
    def kwargs(self):
        if self._broken:
            raise ValueError("cannot unpickle")
        return self._kwargs


class FakeWorker:
    is_horse = True

    def __init__(self):
        self._exc_handlers = []

    def push_exc_handler(self, handler):
        self._exc_handlers.append(handler)

    def pop_exc_handler(self):
        return self._exc_handlers.pop()


def test_not_installed_outside_rqworker(stubs):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)
    assert not getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False)


def test_installed_in_rqworker(stubs):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False) is True
    assert getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False) is True


def test_perform_job_flushes_in_horse(stubs, flushes):
    bootstrap.install({**USER, "rq": {"flush_timeout": 3}}, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job(_horse(), "job", "queue") is True
    assert flushes == [3.0]


def test_perform_job_does_not_flush_outside_horse(stubs, flushes):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    BaseWorker.perform_job(SimpleNamespace(is_horse=False), "job", "queue")
    assert flushes == []


def test_perform_job_flushes_when_job_raises(stubs, flushes):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    with pytest.raises(RuntimeError):
        BaseWorker.perform_job(_horse(), "raise", "queue")
    assert flushes == [5.0]


def test_flush_timeout_logs_a_warning(stubs, monkeypatch, caplog):
    monkeypatch.setattr(bootstrap, "force_flush", lambda timeout: False)
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        BaseWorker.perform_job(_horse(), "job", "queue")
    assert any("did not finish" in r.getMessage() for r in caplog.records)


def test_fork_work_horse_sets_and_clears_the_hint(stubs):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    Worker.fork_work_horse(SimpleNamespace(), "job", "queue")
    assert stubs["fork"] == [bootstrap.ROLE_RQ_HORSE]
    assert bootstrap._next_fork_role is None


def test_signature_mismatch_skips_wrap(stubs, caplog):
    def perform_job(self, job):
        return True

    BaseWorker.perform_job = perform_job
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job is perform_job
    assert getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False) is True
    assert any("unexpected signature" in r.getMessage() for r in caplog.records)


def test_missing_target_skips_wrap_and_warns(stubs, caplog):
    stub_perform = BaseWorker.__dict__["perform_job"]
    del BaseWorker.perform_job
    try:
        with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
            bootstrap.install(USER, env={}, argv=ARGV_RQ)
        assert "perform_job" not in BaseWorker.__dict__
        assert getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False) is True
        assert any("perform_job" in r.getMessage() and "missing" in r.getMessage() for r in caplog.records)
    finally:
        BaseWorker.perform_job = stub_perform


def test_patch_worker_false_disables(stubs):
    bootstrap.install({**USER, "rq": {"patch_worker": False}}, env={}, argv=ARGV_RQ)
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)


def test_not_wrapped_twice_and_shutdown_restores(stubs):
    original = BaseWorker.__dict__["perform_job"]
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    module = rq_module.RqModule()
    module.install(bootstrap._state.context)
    assert BaseWorker.__dict__["perform_job"].__wrapped__ is original
    module.shutdown()
    bootstrap.shutdown()
    bootstrap._state = None
    assert BaseWorker.__dict__["perform_job"] is original


def test_real_rq_signatures_match():
    # Guards against an rq upgrade changing the methods we wrap.
    import inspect

    from rq.worker.base import BaseWorker as RealBase
    from rq.worker.worker_classes import Worker as RealWorker

    assert tuple(inspect.signature(RealBase.__dict__["perform_job"]).parameters) == rq_module.EXPECTED_PARAMS
    assert tuple(inspect.signature(RealWorker.__dict__["fork_work_horse"]).parameters) == rq_module.EXPECTED_PARAMS


def test_enqueue_stores_trace_context_inside_a_span(stubs, spans):
    ctx = bootstrap.install(TRACES, env={}, argv=ARGV_WEB)
    job = FakeJob()
    with ctx.tracer_provider.get_tracer("t").start_as_current_span("request", kind=SpanKind.SERVER) as span:
        Queue.enqueue_job(object.__new__(Queue), job)
    carrier = job.meta[rq_module.CONTEXT_META_KEY]
    assert set(carrier) == {"traceparent"}
    assert f"{span.get_span_context().trace_id:032x}" in carrier["traceparent"]


def test_enqueue_outside_a_span_or_with_existing_context_leaves_meta(stubs, spans):
    bootstrap.install(TRACES, env={}, argv=ARGV_WEB)
    job = FakeJob()
    Queue.enqueue_job(object.__new__(Queue), job)
    assert rq_module.CONTEXT_META_KEY not in job.meta
    existing = FakeJob(meta={rq_module.CONTEXT_META_KEY: {"traceparent": "keep"}})
    Queue.enqueue_job(object.__new__(Queue), existing)
    assert existing.meta[rq_module.CONTEXT_META_KEY] == {"traceparent": "keep"}


def test_propagate_context_false_does_not_wrap_enqueue(stubs, spans):
    bootstrap.install({**TRACES, "rq": {"propagate_context": False}}, env={}, argv=ARGV_WEB)
    assert not getattr(Queue.enqueue_job, rq_module.WRAPPED_ATTR, False)


def test_web_role_wraps_enqueue_but_not_the_worker(stubs, spans):
    bootstrap.install(TRACES, env={}, argv=ARGV_WEB)
    assert getattr(Queue.enqueue_job, rq_module.WRAPPED_ATTR, False)
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)


def test_job_span_is_a_child_of_the_enqueuing_span(stubs, spans):
    ctx = bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    tracer = ctx.tracer_provider.get_tracer("t")
    job = FakeJob(func_name="extras.webhooks.send_webhook")
    with tracer.start_as_current_span("request", kind=SpanKind.SERVER) as parent:
        Queue.enqueue_job(object.__new__(Queue), job)
    BaseWorker.perform_job(FakeWorker(), job, SimpleNamespace(name="default"))
    consumer = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert len(consumer) == 1
    span = consumer[0]
    assert span.name == "rq.job extras.webhooks.send_webhook"
    assert span.instrumentation_scope.name == rq_module.JOB_SCOPE
    assert span.context.trace_id == parent.get_span_context().trace_id
    assert span.parent.span_id == parent.get_span_context().span_id
    assert dict(span.attributes) == {
        "messaging.system": "rq",
        "messaging.destination.name": "default",
        "messaging.message.id": "j-1",
    }
    assert span.status.status_code is StatusCode.UNSET


def test_job_span_is_ended_before_the_horse_flush(stubs, spans, monkeypatch):
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    seen = []
    real_flush = bootstrap.force_flush

    def flush(timeout):
        seen.append(len(spans.get_finished_spans()))
        return real_flush(timeout)

    monkeypatch.setattr(bootstrap, "force_flush", flush)
    BaseWorker.perform_job(FakeWorker(), FakeJob(), SimpleNamespace(name="default"))
    assert seen and spans.get_finished_spans()


def test_failed_job_records_exception_via_the_exc_handler(stubs, spans):
    worker = FakeWorker()

    def perform_job(self, job, queue):
        # What rq does on failure: call the exception handlers, return False.
        try:
            raise KeyError("missing")
        except KeyError as exc:
            for handler in list(self._exc_handlers):
                handler(job, type(exc), exc, exc.__traceback__)
        return False

    # Replace the stub before install, so the wrapper wraps this one (the stubs fixture restores rq's).
    BaseWorker.perform_job = perform_job
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job(worker, FakeJob(), SimpleNamespace(name="low")) is False
    (span,) = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert span.status.status_code is StatusCode.ERROR
    assert span.events[0].name == "exception"
    assert worker._exc_handlers == []


def test_false_return_without_exception_sets_error(stubs, spans):
    BaseWorker.perform_job = lambda self, job, queue: False
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    BaseWorker.perform_job(FakeWorker(), FakeJob(), SimpleNamespace(name="q"))
    (span,) = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "job failed"


def test_raising_perform_job_is_recorded_and_propagates(stubs, spans):
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    with pytest.raises(RuntimeError):
        BaseWorker.perform_job(FakeWorker(), "raise", SimpleNamespace(name="q"))
    (span,) = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert span.status.status_code is StatusCode.ERROR


@pytest.mark.parametrize(
    "foreign",
    ["00-abc", ["x"], {"traceparent": "not-a-traceparent"}, {"traceparent": 42}, None],
)
def test_foreign_meta_gives_a_root_span_and_the_job_runs(stubs, spans, foreign):
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    job = FakeJob(meta={rq_module.CONTEXT_META_KEY: foreign})
    assert BaseWorker.perform_job(FakeWorker(), job, SimpleNamespace(name="q")) is True
    (span,) = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert span.parent is None


def test_non_dict_meta_is_tolerated(stubs, spans):
    ctx = bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    job = FakeJob()
    job.meta = "unserialized"
    with ctx.tracer_provider.get_tracer("t").start_as_current_span("r", kind=SpanKind.SERVER):
        Queue.enqueue_job(object.__new__(Queue), job)
    assert job.meta == "unserialized"
    assert BaseWorker.perform_job(FakeWorker(), job, SimpleNamespace(name="q")) is True


def test_undeserialisable_job_gets_a_generic_name(stubs, spans):
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job(FakeWorker(), FakeJob(broken=True), SimpleNamespace(name="q")) is True
    (span,) = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert span.name == "rq.job"
    assert not any(k.startswith("netbox.job.") for k in span.attributes)


def test_propagate_context_false_ignores_stored_context(stubs, spans):
    ctx = bootstrap.install({**TRACES, "rq": {"propagate_context": False}}, env={}, argv=ARGV_RQ)
    job = FakeJob()
    with ctx.tracer_provider.get_tracer("t").start_as_current_span("r", kind=SpanKind.SERVER):
        job.meta[rq_module.CONTEXT_META_KEY] = otel.inject_trace_context()
    BaseWorker.perform_job(FakeWorker(), job, SimpleNamespace(name="q"))
    (span,) = [s for s in _flush(spans) if s.kind is SpanKind.CONSUMER]
    assert span.parent is None


class _NetBoxJob:
    class _meta:  # noqa: N801 (mimics a Django model's _meta)
        label_lower = "core.job"

    def __init__(self, pk, name):
        self.pk, self.name = pk, name


def test_netbox_job_attributes():
    job = FakeJob(func_name="extras.jobs.ScriptJob.handle", kwargs={"job": _NetBoxJob(7, "Sync devices")})
    attributes = rq_module.job_attributes(job, SimpleNamespace(name="default"))
    assert attributes["netbox.job.id"] == 7
    assert attributes["netbox.job.name"] == "Sync devices"
    other = FakeJob(kwargs={"job": SimpleNamespace(pk=1, name="x")})
    assert "netbox.job.id" not in rq_module.job_attributes(other, SimpleNamespace(name="q"))
    unnamed = FakeJob(kwargs={"job": _NetBoxJob(8, "")})
    attributes = rq_module.job_attributes(unnamed, SimpleNamespace(name="q"))
    assert attributes["netbox.job.id"] == 8 and "netbox.job.name" not in attributes


def test_no_job_span_without_traces(stubs, flushes):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job(_horse(), "job", SimpleNamespace(name="q")) is True
    assert flushes == [5.0]


def test_flush_error_never_replaces_the_job_result(stubs, monkeypatch):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)

    def broken(timeout):
        raise RuntimeError("flush exploded")

    monkeypatch.setattr(bootstrap, "force_flush", broken)
    assert BaseWorker.perform_job(_horse(), "job", SimpleNamespace(name="q")) is True


def test_rq_disabled_installs_nothing(stubs):
    bootstrap.install({**USER, "rq": {"enabled": False}}, env={}, argv=ARGV_RQ)
    assert "rq" not in [m.name for m in bootstrap._state.modules]
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)


def test_rq_missing_disables_the_module_with_one_warning(stubs, monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("rq"):
            raise ImportError("No module named 'rq'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert [r.getMessage() for r in caplog.records if "rq module disabled" in r.getMessage()]
    assert "rq" not in [m.name for m in bootstrap._state.modules]


def test_is_horse_must_be_a_property(stubs, monkeypatch, caplog):
    monkeypatch.setattr(BaseWorker, "is_horse", False)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)
    assert any("is_horse" in r.getMessage() for r in caplog.records)
    assert getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False)


def test_real_rq_enqueue_job_signature_matches():
    import inspect

    assert tuple(inspect.signature(Queue.__dict__["enqueue_job"]).parameters) == rq_module.ENQUEUE_PARAMS
    assert isinstance(inspect.getattr_static(BaseWorker, "is_horse"), property)
