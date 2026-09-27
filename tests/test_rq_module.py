import logging
from types import SimpleNamespace

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from rq.queue import Queue
from rq.worker.base import BaseWorker
from rq.worker.worker_classes import SimpleWorker, Worker

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.modules import rq as rq_module
from tests.otel_helpers import RecordingMetricExporter, data_points

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
    real_execute = Worker.__dict__["execute_job"]
    real_simple_execute = SimpleWorker.__dict__["execute_job"]
    calls = {"perform": [], "fork": [], "enqueue": [], "execute": []}

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

    def execute_job(self, job, queue):
        calls["execute"].append(job)
        if getattr(job, "explode", False):
            raise OSError("waitpid failed")

    BaseWorker.perform_job = perform_job
    Worker.execute_job = execute_job
    SimpleWorker.execute_job = execute_job
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
    Worker.execute_job = real_execute
    SimpleWorker.execute_job = real_simple_execute


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
    def __init__(
        self,
        func_name="app.tasks.work",
        meta=None,
        kwargs=None,
        broken=False,
        job_id="j-1",
        instance=None,
        status="finished",
        status_error=None,
    ):
        self.id = job_id
        self.meta = {} if meta is None else meta
        self._func_name, self._kwargs, self._broken = func_name, kwargs or {}, broken
        self._instance, self._status, self._status_error = instance, status, status_error

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

    @property
    def instance(self):
        if self._broken:
            raise ValueError("cannot unpickle")
        return self._instance

    def get_status(self, refresh=True):
        if self._status_error is not None:
            raise self._status_error
        return SimpleNamespace(value=self._status)


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
    from rq.worker.worker_classes import SimpleWorker as RealSimple
    from rq.worker.worker_classes import Worker as RealWorker

    for cls, attr in (
        (RealBase, "perform_job"),
        (RealWorker, "fork_work_horse"),
        (RealWorker, "execute_job"),
        (RealSimple, "execute_job"),
    ):
        assert tuple(inspect.signature(cls.__dict__[attr]).parameters) == rq_module.WORKER_PARAMS


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
    order = []
    recording_during_flush = []

    real_exit = rq_module._JobSpan.__exit__

    def exit_(self, exc_type, exc_value, traceback):
        result = real_exit(self, exc_type, exc_value, traceback)
        order.append("end")
        return result

    monkeypatch.setattr(rq_module._JobSpan, "__exit__", exit_)

    real_flush = bootstrap.force_flush

    def flush(timeout):
        order.append("flush")
        recording_during_flush.append(otel.current_span_is_recording())
        return real_flush(timeout)

    monkeypatch.setattr(bootstrap, "force_flush", flush)
    BaseWorker.perform_job(FakeWorker(), FakeJob(), SimpleNamespace(name="default"))
    assert order == ["end", "flush"]
    assert recording_during_flush == [False]


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


METRICS = {**USER, "metrics": {"enabled": True, "export_interval": 3600}}
NO_PATCH = {**METRICS, "rq": {"patch_worker": False}}


@pytest.fixture
def metrics(monkeypatch):
    exporter = RecordingMetricExporter()
    monkeypatch.setattr(otel, "build_metric_exporter", lambda cfg: exporter)
    return exporter


def _collect(exporter):
    """Export the current cumulative state and return that batch."""
    count = len(exporter.batches)
    bootstrap.force_flush(2.0)
    return exporter.batches[-1] if len(exporter.batches) > count else None


def _job_points(exporter, name):
    return {
        (
            p.attributes["messaging.destination.name"],
            p.attributes["code.function.name"],
            p.attributes["netbox.rq.job.outcome"],
        ): p
        for p in data_points(_collect(exporter), name)
    }


class ScriptJob:
    """Stands in for a NetBox JobRunner subclass (jobs enqueue the classmethod `handle`)."""


def test_job_function_qualifies_methods_and_keeps_functions():
    assert rq_module.job_function(FakeJob(func_name="extras.webhooks.send_webhook")) == "extras.webhooks.send_webhook"
    assert rq_module.job_function(FakeJob(func_name="handle", instance=ScriptJob)) == f"{__name__}.ScriptJob.handle"
    assert rq_module.job_function(FakeJob(func_name="run", instance=ScriptJob())) == f"{__name__}.ScriptJob.run"
    assert rq_module.job_function(FakeJob(broken=True)) is None
    script = FakeJob(func_name="handle", instance=ScriptJob)
    assert rq_module.job_span_name(script) == f"rq.job {__name__}.ScriptJob.handle"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("finished", "finished"),
        ("failed", "failed"),
        ("stopped", "stopped"),
        ("canceled", "canceled"),
        ("queued", "retried"),
        ("scheduled", "retried"),
        ("started", "unknown"),
        ("something-new", "unknown"),
    ],
)
def test_job_outcome_maps_rq_status(status, outcome):
    assert rq_module.job_outcome(FakeJob(status=status)) == outcome


def test_job_outcome_is_unknown_when_the_lookup_fails():
    assert rq_module.job_outcome(FakeJob(status_error=ConnectionError("redis down"))) == "unknown"


def test_execute_job_records_duration_and_count_in_the_worker_parent(stubs, metrics):
    bootstrap.install(METRICS, env={}, argv=ARGV_RQ)
    queue = SimpleNamespace(name="default")
    Worker.execute_job(SimpleNamespace(), FakeJob(func_name="app.tasks.work"), queue)
    Worker.execute_job(SimpleNamespace(), FakeJob(func_name="app.tasks.work", status="failed"), queue)
    counts = _job_points(metrics, rq_module.JOBS)
    assert counts[("default", "app.tasks.work", "finished")].value == 1
    assert counts[("default", "app.tasks.work", "failed")].value == 1
    durations = _job_points(metrics, rq_module.JOB_DURATION)
    assert durations[("default", "app.tasks.work", "finished")].count == 1


def test_simple_worker_execute_job_is_wrapped_too(stubs, metrics):
    bootstrap.install(METRICS, env={}, argv=ARGV_RQ)
    SimpleWorker.execute_job(SimpleNamespace(), FakeJob(), SimpleNamespace(name="low"))
    assert ("low", "app.tasks.work", "finished") in _job_points(metrics, rq_module.JOBS)


def test_a_raising_execute_job_is_still_counted_and_propagates(stubs, metrics):
    bootstrap.install(METRICS, env={}, argv=ARGV_RQ)
    job = FakeJob(status="started")
    job.explode = True
    with pytest.raises(OSError):
        Worker.execute_job(SimpleNamespace(), job, SimpleNamespace(name="default"))
    assert ("default", "app.tasks.work", "unknown") in _job_points(metrics, rq_module.JOBS)


def test_undeserialisable_job_is_counted_as_unknown_function(stubs, metrics):
    bootstrap.install(METRICS, env={}, argv=ARGV_RQ)
    Worker.execute_job(SimpleNamespace(), FakeJob(broken=True), SimpleNamespace(name="default"))
    assert ("default", "unknown", "finished") in _job_points(metrics, rq_module.JOBS)


def test_metric_recording_failure_never_breaks_the_worker(stubs, metrics, monkeypatch, caplog):
    bootstrap.install(METRICS, env={}, argv=ARGV_RQ)
    monkeypatch.setattr(rq_module, "job_outcome", lambda job: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        Worker.execute_job(SimpleNamespace(), FakeJob(), SimpleNamespace(name="default"))
        Worker.execute_job(SimpleNamespace(), FakeJob(), SimpleNamespace(name="default"))
    assert len(stubs["execute"]) == 2
    assert len([r for r in caplog.records if "job metrics" in r.getMessage()]) == 1


def test_no_job_metrics_without_metrics_or_with_patch_worker_off(stubs, metrics):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert not getattr(Worker.execute_job, rq_module.WRAPPED_ATTR, False)
    bootstrap.shutdown()
    bootstrap._state = None
    bootstrap.install(NO_PATCH, env={}, argv=ARGV_RQ)
    assert not getattr(Worker.execute_job, rq_module.WRAPPED_ATTR, False)


def test_web_role_gets_no_job_metrics(stubs, metrics):
    bootstrap.install(METRICS, env={}, argv=ARGV_WEB)
    assert not getattr(Worker.execute_job, rq_module.WRAPPED_ATTR, False)


class FakeQueue:
    def __init__(self, name, count=0, error=None):
        self.name, self._count, self._error = name, count, error

    @property
    def count(self):
        if self._error is not None:
            raise self._error
        return self._count


def _install_with_queues(monkeypatch, source):
    # The SDK keeps the first observable instrument per name and scope, so the gauge under test is
    # the one bootstrap's own RqModule registers; it reads rq_module.netbox_queues at collection time.
    monkeypatch.setattr(rq_module, "netbox_queues", source)
    bootstrap.install(NO_PATCH, env={}, argv=ARGV_RQ)


def _depths(exporter):
    return {
        p.attributes["messaging.destination.name"]: p.value
        for p in data_points(_collect(exporter), rq_module.QUEUE_DEPTH)
    }


def test_queue_depth_reports_every_configured_queue(stubs, metrics, monkeypatch):
    _install_with_queues(monkeypatch, lambda: [FakeQueue("high", 0), FakeQueue("default", 3)])
    assert _depths(metrics) == {"high": 0, "default": 3}


def test_queue_depth_skips_a_failing_queue_and_warns_once(stubs, metrics, monkeypatch, caplog):
    _install_with_queues(
        monkeypatch, lambda: [FakeQueue("high", error=ConnectionError("redis down")), FakeQueue("low", 2)]
    )
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        assert _depths(metrics) == {"low": 2}
        assert _depths(metrics) == {"low": 2}
    assert len([r for r in caplog.records if "queue depth" in r.getMessage()]) == 1


def test_queue_source_failure_yields_nothing_and_does_not_break_export(stubs, metrics, monkeypatch, caplog):
    _install_with_queues(monkeypatch, lambda: (_ for _ in ()).throw(ImportError("no django_rq")))
    ctx = bootstrap._state.context
    ctx.meter_provider.get_meter("t").create_counter("netbox.object_changes").add(1)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        batch = _collect(metrics)
        _collect(metrics)
    assert data_points(batch, rq_module.QUEUE_DEPTH) == []
    assert data_points(batch, "netbox.object_changes")
    assert len([r for r in caplog.records if "could not list the RQ queues" in r.getMessage()]) == 1


def test_default_queue_source_without_django_rq_is_caught(stubs, metrics, caplog):
    # Unit tests have no django_rq: the real netbox_queues raises ImportError inside the callback.
    bootstrap.install(NO_PATCH, env={}, argv=ARGV_RQ)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        assert _depths(metrics) == {}
    assert len([r for r in caplog.records if "could not list the RQ queues" in r.getMessage()]) == 1


def test_queues_are_built_once_per_process(stubs, metrics, monkeypatch):
    built = []

    def source():
        built.append(1)
        return [FakeQueue("default", 1)]

    _install_with_queues(monkeypatch, source)
    _collect(metrics)
    _collect(metrics)
    assert built == [1]
    # A forked scheduler child. Bootstrap's force_flush skips a PID it did not install in, so the
    # export thread's call into the gauge callback is made directly.
    (module,) = [m for m in bootstrap._state.modules if m.name == "rq"]
    # Scoped, so bootstrap.shutdown() in the fixture teardown runs with the real PID.
    with monkeypatch.context() as patch:
        patch.setattr(rq_module.os, "getpid", lambda: -1)
        assert [o.value for o in module._observe_queue_depth(None)] == [1]
    assert built == [1, 1]


def test_queue_depth_stops_after_shutdown(stubs, metrics, monkeypatch):
    _install_with_queues(monkeypatch, lambda: [FakeQueue("default", 1)])
    assert _depths(metrics) == {"default": 1}
    (module,) = [m for m in bootstrap._state.modules if m.name == "rq"]
    module.shutdown()
    assert _depths(metrics) == {}


def test_install_registers_queue_depth_in_rqworker_only(stubs, metrics, monkeypatch):
    registered = []
    monkeypatch.setattr(rq_module.RqModule, "_register_queue_depth", lambda self, ctx: registered.append(ctx.role))
    bootstrap.install(NO_PATCH, env={}, argv=ARGV_RQ)
    bootstrap.shutdown()
    bootstrap._state = None
    bootstrap.install({**METRICS, "traces": {"enabled": True, "instrument": []}}, env={}, argv=ARGV_WEB)
    assert registered == ["rqworker"]


def test_metric_names_are_allowlisted():
    for name in (rq_module.JOB_DURATION, rq_module.JOBS, rq_module.QUEUE_DEPTH):
        assert name in otel.METRIC_ALLOWLIST


def test_job_span_does_not_push_a_handler_when_exc_handlers_is_not_a_list(stubs, spans):
    class TupleWorker(FakeWorker):
        def __init__(self):
            self._exc_handlers = ()
            self.pushed = []

        def push_exc_handler(self, handler):
            self.pushed.append(handler)

    worker = TupleWorker()
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    ctx = bootstrap._state.context
    with rq_module._JobSpan(ctx, worker, FakeJob(), SimpleNamespace(name="q")):
        pass
    # Pushed without a way to pop it again, the handler would outlive the job span.
    assert worker.pushed == []
    assert len(_flush(spans)) == 1


def test_job_span_ends_even_when_handler_removal_fails(stubs, spans):
    class Handlers(list):
        def remove(self, item):
            raise RuntimeError("cannot remove")

    worker = FakeWorker()
    worker._exc_handlers = Handlers()
    bootstrap.install(TRACES, env={}, argv=ARGV_RQ)
    with rq_module._JobSpan(bootstrap._state.context, worker, FakeJob(), SimpleNamespace(name="q")):
        pass
    assert len(_flush(spans)) == 1


def test_warn_once_is_per_process(monkeypatch, caplog):
    # A forked child (for example the rq scheduler) inherits the parent's set of warned keys; it
    # must still get its own first warning.
    rq_module._warned.clear()
    monkeypatch.setattr(rq_module.os, "getpid", lambda: 1000)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        rq_module._warn_once("queue-depth", "warned %s", "a")
        rq_module._warn_once("queue-depth", "warned %s", "b")
        monkeypatch.setattr(rq_module.os, "getpid", lambda: 1001)
        rq_module._warn_once("queue-depth", "warned %s", "c")
        rq_module._warn_once("queue-depth", "warned %s", "d")
    assert [r.getMessage() for r in caplog.records] == ["warned a", "warned c"]
