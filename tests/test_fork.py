"""Real os.fork() tests for the after-fork re-initialisation.

Each child runs a probe function, sends a JSON result back over a pipe and leaves with os._exit,
so no pytest machinery runs in the child.
"""

import contextlib
import json
import logging
import os
import select
import signal
import threading
import time

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from netbox_opentelemetry_plugin import bootstrap, conf, otel
from tests.otel_helpers import RecordingMetricExporter, all_batches_points

pytestmark = [
    pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork"),
    # Python 3.12+ warns when forking a process that has threads (the batch worker). Expected here.
    pytest.mark.filterwarnings("ignore:.*use of fork\\(\\) may lead to deadlocks:DeprecationWarning"),
]

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.fork"]}}
ARGV_WEB = ["gunicorn", "netbox.wsgi"]
ARGV_RQ = ["/opt/netbox/netbox/manage.py", "rqworker"]
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
def exporters(monkeypatch):
    created = []

    def factory(cfg):
        exporter = InMemoryLogRecordExporter()
        created.append(exporter)
        return exporter

    monkeypatch.setattr(otel, "build_log_exporter", factory)
    return created


def _otel_handlers(name):
    return [h for h in logging.getLogger(name).handlers if isinstance(h, otel.AllowlistLoggingHandler)]


CHILD_TIMEOUT = 30


def _run_in_child(probe):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                result = {"ok": True, **probe()}
            except BaseException as exc:
                result = {"ok": False, "error": repr(exc)}
            try:
                with os.fdopen(write_fd, "w") as fh:
                    json.dump(result, fh)
            except BaseException:
                pass
        finally:
            os._exit(0)

    os.close(write_fd)
    reaped = False
    try:
        chunks = []
        deadline = time.monotonic() + CHILD_TIMEOUT
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    reaped = True
                    pytest.fail(
                        f"child did not finish within {CHILD_TIMEOUT} s (possible deadlock in after-fork hooks)"
                    )
                ready, _, _ = select.select([read_fd], [], [], remaining)
                if not ready:
                    continue
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(read_fd)

        _, status = os.waitpid(pid, 0)
        reaped = True
        payload = b"".join(chunks).decode()
        if not payload:
            pytest.fail(f"child produced no output (status={status!r}); it likely crashed before writing its result")
        if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
            pytest.fail(f"child did not exit cleanly: status={status!r}, payload={payload!r}")
        result = json.loads(payload)
        assert result.pop("ok"), result.get("error")
        return result
    finally:
        # If select() or read() raised instead of the timeout/EOF paths above running, the child
        # may still be alive and unreaped at this point. Never leave it behind.
        if not reaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


def test_after_fork_in_parent_without_before_does_not_raise():
    """An embedder that runs only the parent hook (skipping _before_fork) must not crash NetBox.

    Fork hooks must never raise. If some embedder invokes after_in_parent without first calling
    the before hook, _lock.release() on an unheld RLock raises RuntimeError; that must be
    swallowed, not propagated.
    """
    bootstrap._after_fork_in_parent()


def test_child_rebuilds_provider_resource_and_repoints_handler(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    parent_provider = ctx.logger_provider
    parent_instance = ctx.resource.attributes["service.instance.id"]
    logging.getLogger("t.fork").setLevel(logging.INFO)

    def probe():
        logging.getLogger("t.fork").info("from child")
        ctx.logger_provider.force_flush()
        handlers = _otel_handlers("t.fork")
        records = exporters[-1].get_finished_logs()
        return {
            "pid": os.getpid(),
            "new_provider": ctx.logger_provider is not parent_provider,
            "handler_count": len(handlers),
            "handler_uses_new_provider": handlers[0]._logger_provider is ctx.logger_provider,
            "instance_id": ctx.resource.attributes["service.instance.id"],
            "record_instance_ids": [r.resource.attributes["service.instance.id"] for r in records],
            "bodies": [r.log_record.body for r in records],
            "exporter_count": len(exporters),
        }

    result = _run_in_child(probe)
    assert result["new_provider"] is True
    assert result["handler_count"] == 1
    assert result["handler_uses_new_provider"] is True
    assert result["instance_id"].endswith(f"-{result['pid']}")
    assert result["instance_id"] != parent_instance
    assert result["bodies"] == ["from child"]
    assert result["record_instance_ids"] == [result["instance_id"]]
    assert result["exporter_count"] == 2
    assert ctx.logger_provider is parent_provider


def test_unflushed_parent_records_are_not_exported_by_child(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    lg = logging.getLogger("t.fork")
    lg.setLevel(logging.INFO)
    lg.info("before fork")

    def probe():
        lg.info("child only")
        ctx.logger_provider.force_flush()
        return {"bodies": [r.log_record.body for r in exporters[-1].get_finished_logs()]}

    assert _run_in_child(probe) == {"bodies": ["child only"]}
    ctx.logger_provider.force_flush()
    assert [r.log_record.body for r in exporters[0].get_finished_logs()] == ["before fork"]


def test_fork_with_horse_hint_becomes_rq_horse(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_RQ)
    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:
        result = _run_in_child(
            lambda: {"role": ctx.role, "resource_role": ctx.resource.attributes["netbox.process.role"]}
        )
    finally:
        bootstrap.set_next_fork_role(None)
    assert result == {"role": "rq_horse", "resource_role": "rq_horse"}


def test_other_rqworker_fork_keeps_rqworker_role(exporters):
    # For example the RQ scheduler, which is a forked multiprocessing.Process.
    ctx = bootstrap.install(USER, env={}, argv=ARGV_RQ)

    def probe():
        return {
            "role": ctx.role,
            "resource_role": ctx.resource.attributes["netbox.process.role"],
            "pid_in_id": ctx.resource.attributes["service.instance.id"].endswith(f"-{os.getpid()}"),
        }

    assert _run_in_child(probe) == {"role": "rqworker", "resource_role": "rqworker", "pid_in_id": True}


def test_horse_hint_is_consumed_in_the_child(exporters):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:
        result = _run_in_child(lambda: {"hint": bootstrap._next_fork_role})
    finally:
        bootstrap.set_next_fork_role(None)
    assert result == {"hint": None}


def test_hint_is_cleared_in_the_parent_after_a_fork():
    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:
        _run_in_child(lambda: {})
        assert bootstrap._next_fork_role is None
    finally:
        bootstrap.set_next_fork_role(None)


def test_hint_is_cleared_in_the_child_even_without_installed_state():
    # No bootstrap.install() call: _state is None, exercising reinit_after_fork's early return.
    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:
        result = _run_in_child(lambda: {"hint": bootstrap._next_fork_role})
    finally:
        bootstrap.set_next_fork_role(None)
    assert result == {"hint": None}


def test_reinit_is_idempotent_within_a_process(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    parent_provider = ctx.logger_provider
    bootstrap.reinit_after_fork()
    assert ctx.logger_provider is parent_provider
    assert len(exporters) == 1

    def probe():
        child_provider = ctx.logger_provider
        bootstrap.reinit_after_fork()
        return {"same_provider": ctx.logger_provider is child_provider, "exporter_count": len(exporters)}

    assert _run_in_child(probe) == {"same_provider": True, "exporter_count": 2}


def test_external_provider_is_not_rebuilt(monkeypatch, exporters):
    external = otel.build_logger_provider(
        otel.build_resource("ext", {}, service_version="x", plugin_version="x", role="web"),
        InMemoryLogRecordExporter(),
        synchronous=True,
    )
    monkeypatch.setattr(otel, "existing_logger_provider", lambda: external)
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def probe():
        return {"still_external": ctx.logger_provider is external, "exporter_count": len(exporters)}

    assert _run_in_child(probe) == {"still_external": True, "exporter_count": 0}


def test_rebuild_failure_detaches_handler_and_drops_ownership(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def probe():
        messages = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        logging.getLogger("netbox_opentelemetry_plugin").addHandler(ListHandler())

        def boom(cfg):
            raise RuntimeError("no exporter in child")

        otel.build_log_exporter = boom
        bootstrap._state.pid = -1  # force a second re-init as if we had just forked
        bootstrap.reinit_after_fork()
        return {
            "provider_is_none": ctx.logger_provider is None,
            "owns": bootstrap._state.owns_logger_provider,
            "handlers": len(_otel_handlers("t.fork")),
            "messages": messages,
        }

    result = _run_in_child(probe)
    assert result["provider_is_none"] is True
    assert result["owns"] is False
    assert result["handlers"] == 0
    assert any("re-initialisation after fork failed" in m for m in result["messages"])


def test_rebuild_failure_in_rqworker_child_keeps_old_role_and_resource(monkeypatch, exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert ctx.role == bootstrap.ROLE_RQWORKER
    assert ctx.resource.attributes["netbox.process.role"] == "rqworker"

    def boom(cfg):
        raise RuntimeError("no exporter in child")

    # Patched here, in the parent, before forking: the automatic at-fork hook (which runs the
    # first, real rebuild attempt in the child) inherits this and fails.
    monkeypatch.setattr(otel, "build_log_exporter", boom)

    def probe():
        return {"role": ctx.role, "resource_role": ctx.resource.attributes["netbox.process.role"]}

    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:
        result = _run_in_child(probe)
    finally:
        bootstrap.set_next_fork_role(None)
    assert result == {"role": "rqworker", "resource_role": "rqworker"}


def test_lock_is_free_in_child(exporters):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def probe():
        acquired = bootstrap._lock.acquire(timeout=1)
        if acquired:
            bootstrap._lock.release()
        return {"acquired": acquired}

    assert _run_in_child(probe) == {"acquired": True}


def test_fork_while_batch_worker_lock_is_held(exporters):
    """Forking while a parent thread holds the batch processor's condition lock must not hang.

    The lock (and any mutex backing it) is copied into the child by fork() in whatever state it
    was in at that instant. If a hook in the child tries to acquire or otherwise touch that copied
    lock, and the parent thread that owned it does not exist in the child to release it, the child
    deadlocks forever. This reproduces the exact condition the SDK's own batch worker creates
    roughly once a second.
    """
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    provider = ctx.logger_provider
    try:
        batch_processor = provider._multi_log_record_processor._log_record_processors[0]._batch_processor
        cond = batch_processor._worker_awaken._cond
    except (AttributeError, IndexError):
        pytest.skip("SDK internals changed")

    started = threading.Event()

    def holder():
        with cond:
            started.set()
            cond.notify_all()
            time.sleep(2)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    try:
        assert started.wait(timeout=5), "helper thread never acquired the condition lock"

        def probe():
            return {}

        result = _run_in_child(probe)
    finally:
        t.join(timeout=5)

    assert result == {}


def test_fork_waits_for_bootstrap_lock_holder(exporters):
    """_before_fork must block until whoever holds bootstrap._lock releases it.

    _after_fork_in_child always swaps in a brand new, unlocked RLock, so the child being able to
    acquire bootstrap._lock proves nothing on its own: that would succeed even if _before_fork did
    nothing at all. Instead the holder thread sets a marker in a plain dict immediately before it
    releases the lock, still while holding it. If _before_fork really blocks until release, the
    fork() call cannot return (in either process) until that marker is already True in memory, so
    the child inherits it as True. If _before_fork does not block, the fork can go ahead while the
    holder still holds the lock, with the marker still False, and the child inherits that instead.
    """
    bootstrap.install(USER, env={}, argv=ARGV_WEB)

    started = threading.Event()
    state = {"released": False}

    def holder():
        with bootstrap._lock:
            started.set()
            time.sleep(0.3)
            state["released"] = True

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert started.wait(timeout=5), "helper thread never acquired bootstrap._lock"

        def probe():
            acquired = bootstrap._lock.acquire(timeout=1)
            if acquired:
                bootstrap._lock.release()
            return {"acquired": acquired, "released": state["released"]}

        result = _run_in_child(probe)
    finally:
        t.join(timeout=5)

    assert result == {"acquired": True, "released": True}


def test_grpc_exporter_survives_fork():
    user = {
        "exporter": {"endpoint": "http://127.0.0.1:9", "protocol": "grpc", "timeout": 1},
        "logs": {"loggers": ["t.fork"]},
    }
    ctx = bootstrap.install(user, env={}, argv=ARGV_WEB)
    lg = logging.getLogger("t.fork")
    lg.setLevel(logging.INFO)
    lg.info("parent record")

    def probe():
        lg.info("child record")
        # Nothing listens on port 9: the export fails, but it must fail within the deadline, not hang.
        ctx.logger_provider.force_flush(timeout_millis=3000)
        return {"done": True}

    assert _run_in_child(probe) == {"done": True}


@pytest.fixture
def span_exporters(monkeypatch):
    created = []

    def factory(cfg):
        exporter = InMemorySpanExporter()
        created.append(exporter)
        return exporter

    monkeypatch.setattr(otel, "build_span_exporter", factory)
    return created


def test_child_rebuilds_tracer_provider_and_keeps_the_switchable(exporters, span_exporters):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    switchable = ctx.tracer_provider
    parent_delegate = switchable.delegate
    tracer = switchable.get_tracer("t")  # an instrumentor would hold this tracer across the fork
    with tracer.start_as_current_span("parent-unflushed", kind=SpanKind.SERVER):
        pass

    def probe():
        with tracer.start_as_current_span("child", kind=SpanKind.SERVER):
            pass
        ctx.tracer_provider.force_flush()
        spans = span_exporters[-1].get_finished_spans()
        return {
            "same_switchable": ctx.tracer_provider is switchable,
            "new_delegate": switchable.delegate is not parent_delegate,
            "names": [s.name for s in spans],
            "instance_ids": [s.resource.attributes["service.instance.id"] for s in spans],
            "pid": os.getpid(),
            "exporter_count": len(span_exporters),
        }

    result = _run_in_child(probe)
    assert result["same_switchable"] and result["new_delegate"]
    assert result["names"] == ["child"]
    assert result["instance_ids"] == [f"{result['instance_ids'][0].rsplit('-', 1)[0]}-{result['pid']}"]
    assert result["exporter_count"] == 2
    ctx.tracer_provider.force_flush()
    assert [s.name for s in span_exporters[0].get_finished_spans()] == ["parent-unflushed"]
    assert switchable.delegate is parent_delegate


def test_horse_spans_carry_the_rq_horse_role(exporters, span_exporters):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_RQ)
    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:

        def probe():
            with ctx.tracer_provider.get_tracer("t").start_as_current_span("job", kind=SpanKind.CONSUMER):
                pass
            ctx.tracer_provider.force_flush()
            (span,) = span_exporters[-1].get_finished_spans()
            return {"role": span.resource.attributes["netbox.process.role"]}

        result = _run_in_child(probe)
    finally:
        bootstrap.set_next_fork_role(None)
    assert result == {"role": "rq_horse"}


def test_tracer_rebuild_failure_stops_span_export_in_the_child(exporters, span_exporters, monkeypatch):
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    tracer = ctx.tracer_provider.get_tracer("t")

    def boom(cfg):
        raise OSError("certificate unreadable")

    monkeypatch.setattr(otel, "build_span_exporter", boom)

    def probe():
        with tracer.start_as_current_span("child", kind=SpanKind.SERVER) as span:
            recording = span.is_recording()
        return {
            "recording": recording,
            "owns": bootstrap._state.owns_tracer_provider,
            "parent_exporter_spans": len(span_exporters[0].get_finished_spans()),
            "logger_provider": ctx.logger_provider is None,
        }

    result = _run_in_child(probe)
    assert result == {"recording": False, "owns": False, "parent_exporter_spans": 0, "logger_provider": True}


def test_external_tracer_provider_is_not_rebuilt(exporters, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider

    external = TracerProvider()
    monkeypatch.setattr(otel, "existing_tracer_provider", lambda: external)
    ctx = bootstrap.install(TRACES_USER, env={}, argv=ARGV_WEB)
    result = _run_in_child(lambda: {"same": ctx.tracer_provider is external})
    assert result == {"same": True}


METRICS_USER = {**USER, "metrics": {"enabled": True, "export_interval": 3600}}


@pytest.fixture
def metric_exporters(monkeypatch):
    created = []

    def factory(cfg):
        exporter = RecordingMetricExporter()
        created.append(exporter)
        return exporter

    monkeypatch.setattr(otel, "build_metric_exporter", factory)
    return created


def test_child_rebuilds_the_metrics_pipeline_and_keeps_the_switchable(exporters, metric_exporters):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    switchable = ctx.meter_provider
    parent_pipeline = bootstrap._state.metrics_pipeline
    counter = switchable.get_meter("t").create_counter("netbox.object_changes")

    def probe():
        state = bootstrap._state
        counter.add(1)
        bootstrap.force_flush(2.0)
        child_exporter = metric_exporters[-1]
        points = all_batches_points(child_exporter, "netbox.object_changes")
        resource = child_exporter.batches[-1].resource_metrics[0].resource.attributes if child_exporter.batches else {}
        return {
            "same_switchable": state.context.meter_provider is switchable,
            "new_pipeline": state.metrics_pipeline is not parent_pipeline and state.metrics_pipeline is not None,
            "delegate_is_new": switchable.delegate is state.metrics_pipeline.provider,
            "exporters": len(metric_exporters),
            "points": [p.value for p in points],
            "parent_exported_in_child": len(metric_exporters[0].batches),
            "instance": resource.get("service.instance.id", ""),
            "pid": os.getpid(),
            "threads": [t.name for t in threading.enumerate()],
        }

    result = _run_in_child(probe)
    assert result["same_switchable"] and result["new_pipeline"] and result["delegate_is_new"]
    assert result["exporters"] == 2
    assert result["points"] == [1]
    assert result["parent_exported_in_child"] == 0
    assert result["instance"].endswith(f"-{result['pid']}")
    assert "otel-metrics" in result["threads"]


def test_horse_records_into_a_noop_and_never_exports(exporters, metric_exporters, monkeypatch):
    monkeypatch.setattr(conf, "MIN_EXPORT_INTERVAL", 0.01)  # a short parent interval, below the 1 s floor
    user = {**METRICS_USER, "metrics": {"enabled": True, "export_interval": 0.05}}
    ctx = bootstrap.install(user, env={}, argv=ARGV_RQ)
    counter = ctx.meter_provider.get_meter("t").create_counter("netbox.object_changes")
    gauge_calls = []
    ctx.meter_provider.get_meter("t").create_observable_gauge(
        "netbox.rq.queue.depth", callbacks=[lambda options: gauge_calls.append(os.getpid()) or []]
    )

    def probe():
        state = bootstrap._state
        batches_before = len(metric_exporters[0].batches)
        counter.add(5)
        time.sleep(0.4)  # several parent intervals: nothing may export or collect in the child
        flushed = bootstrap.force_flush(1.0)
        return {
            "role": state.context.role,
            "pipeline": state.metrics_pipeline is None,
            "noop": type(state.context.meter_provider.delegate).__name__,
            "exporters": len(metric_exporters),
            "batches_delta": len(metric_exporters[0].batches) - batches_before,
            "flushed": flushed,
            "threads": [t.name for t in threading.enumerate()],
            "child_callbacks": [pid for pid in gauge_calls if pid == os.getpid()],
        }

    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    result = _run_in_child(probe)
    assert result["role"] == bootstrap.ROLE_RQ_HORSE
    assert result["pipeline"] is True
    assert result["noop"] == "NoOpMeterProvider"
    assert result["exporters"] == 1  # no exporter was built in the horse
    assert result["batches_delta"] == 0
    assert result["flushed"] is True
    assert "otel-metrics" not in result["threads"]
    assert "OtelPeriodicExportingMetricReader" not in result["threads"]
    assert result["child_callbacks"] == []


def test_horse_forked_while_the_parent_exports_does_not_deadlock(exporters, monkeypatch):
    block = threading.Event()
    blocked = RecordingMetricExporter(block=block)
    monkeypatch.setattr(otel, "build_metric_exporter", lambda cfg: blocked)
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_RQ)
    counter = ctx.meter_provider.get_meter("t").create_counter("netbox.object_changes")
    counter.add(1)
    # The parent's export thread is now inside export(), holding the reader's export lock.
    flusher = threading.Thread(target=bootstrap._state.metrics_pipeline.force_flush, daemon=True)
    flusher.start()
    time.sleep(0.1)

    def probe():
        counter.add(1)
        return {"flushed": bootstrap.force_flush(1.0)}

    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    try:
        result = _run_in_child(probe)
    finally:
        block.set()
        flusher.join(2)
    assert result["flushed"] is True


def test_parent_keeps_exporting_after_a_fork(exporters, metric_exporters):
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)
    counter = ctx.meter_provider.get_meter("t").create_counter("netbox.object_changes")
    _run_in_child(lambda: {})
    counter.add(3)
    bootstrap.force_flush(2.0)
    assert [p.value for p in all_batches_points(metric_exporters[0], "netbox.object_changes")] == [3]


def test_metrics_rebuild_failure_switches_the_child_to_noop(exporters, monkeypatch):
    calls = []

    def factory(cfg):
        calls.append(os.getpid())
        if len(calls) > 1:
            raise RuntimeError("certificate gone")
        return RecordingMetricExporter()

    monkeypatch.setattr(otel, "build_metric_exporter", factory)
    bootstrap.install(METRICS_USER, env={}, argv=ARGV_WEB)

    def probe():
        state = bootstrap._state
        return {
            "pipeline": state.metrics_pipeline is None,
            "noop": type(state.context.meter_provider.delegate).__name__,
            "logger_provider": state.context.logger_provider is None,
        }

    result = _run_in_child(probe)
    assert result["pipeline"] is True
    assert result["noop"] == "NoOpMeterProvider"
    assert result["logger_provider"] is True


def test_horse_rebuild_failure_switches_the_horse_to_noop(metric_exporters, monkeypatch):
    calls = []

    def factory(cfg):
        calls.append(os.getpid())
        if len(calls) > 1:
            raise RuntimeError("certificate gone")
        return InMemoryLogRecordExporter()

    monkeypatch.setattr(otel, "build_log_exporter", factory)
    bootstrap.install(METRICS_USER, env={}, argv=ARGV_RQ)

    def probe():
        state = bootstrap._state
        return {
            "calls_in_child": len(calls),
            "role": state.context.role,
            "pipeline": state.metrics_pipeline is None,
            "noop": type(state.context.meter_provider.delegate).__name__,
            "logger_provider": state.context.logger_provider is None,
            "threads": [t.name for t in threading.enumerate()],
        }

    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    result = _run_in_child(probe)
    assert result["calls_in_child"] == 2  # the child's rebuild reached the failing build
    assert result["role"] == bootstrap.ROLE_RQWORKER  # a failed rebuild keeps the inherited role
    assert result["pipeline"] is True
    assert result["noop"] == "NoOpMeterProvider"
    assert result["logger_provider"] is True
    assert "otel-metrics" not in result["threads"]
    assert len(metric_exporters) == 1  # no exporter was built in the horse


def test_external_meter_provider_is_kept_in_a_web_child_and_switched_off_in_a_horse(exporters, monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider

    external = MeterProvider(shutdown_on_exit=False)
    monkeypatch.setattr(otel, "existing_meter_provider", lambda: external)
    ctx = bootstrap.install(METRICS_USER, env={}, argv=ARGV_RQ)
    switchable = ctx.meter_provider

    unannounced = _run_in_child(lambda: {"external": switchable.delegate is external})
    assert unannounced["external"] is True
    bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
    horse = _run_in_child(lambda: {"noop": type(switchable.delegate).__name__})
    assert horse["noop"] == "NoOpMeterProvider"


def test_a_failed_delegate_swap_shuts_down_the_new_metrics_pipeline(
    exporters, span_exporters, metric_exporters, monkeypatch
):
    # Called directly (no fork): the swap of the tracer delegate fails after the child's metrics
    # pipeline was built. That pipeline's export thread must not be left running.
    user = {**TRACES_USER, "metrics": {"enabled": True, "export_interval": 3600}}
    ctx = bootstrap.install(user, env={}, argv=ARGV_WEB)
    state = bootstrap._state
    parent_pipeline = state.metrics_pipeline

    def fail(self, provider):
        raise RuntimeError("swap failed")

    monkeypatch.setattr(otel.SwitchableTracerProvider, "set_delegate", fail)
    with pytest.raises(RuntimeError):
        bootstrap._rebuild_for_child(ctx, state, None)
    assert len(metric_exporters) == 2  # the new pipeline was built
    assert metric_exporters[1].shutdown_called is True
    assert state.metrics_pipeline is parent_pipeline
    assert [t.name for t in threading.enumerate()].count("otel-metrics") == 1
