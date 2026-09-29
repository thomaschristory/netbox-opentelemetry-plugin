"""RQ integration: flush the work-horse before it exits, and label its fork.

rq runs each job in a forked work-horse that leaves with os._exit, so atexit never runs there
and buffered log records would be lost. perform_job is wrapped with a bounded flush in a finally
block, and fork_work_horse announces the fork so the child is labelled rq_horse. Other forks of
the worker process (such as the RQ scheduler) keep the rqworker role.

With tracing, Queue.enqueue_job stores the W3C trace context of the current span in job.meta, and
perform_job runs each job inside a CONSUMER span whose parent is that context. The span ends
before the horse flush.

While exports keep failing (a Collector outage), horses skip that flush instead of each waiting up to
rq.flush_timeout, so the worker keeps its normal pace; see FlushBreaker.

With metrics, the worker parent records netbox.rq.job.duration and netbox.rq.jobs around execute_job
(the horse records no metrics), and every rqworker process reports netbox.rq.queue.depth.
"""

from __future__ import annotations

import functools
import inspect
import logging
import mmap
import os
import struct
import time
from typing import NamedTuple

from .. import otel
from ..conf import Settings
from ..version import __version__
from .base import Context

WRAPPED_ATTR = "_netbox_otel_wrapped"
WORKER_PARAMS = ("self", "job", "queue")
ENQUEUE_PARAMS = ("self", "job", "pipeline", "at_front", "unique")
CONTEXT_META_KEY = "netbox_otel_context"
JOB_SCOPE = "netbox_opentelemetry_plugin.rq"
BREAKER_SIGNALS = (otel.SIGNAL_LOGS, otel.SIGNAL_TRACES)
SIGNAL_LABELS = {otel.SIGNAL_LOGS: "log records", otel.SIGNAL_TRACES: "spans"}
JOB_DURATION = "netbox.rq.job.duration"
JOBS = "netbox.rq.jobs"
QUEUE_DEPTH = "netbox.rq.queue.depth"
# Seconds; from sub-second webhooks to hour-long scripts.
JOB_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
UNKNOWN = "unknown"
# rq's job status once the horse has exited. Queued or scheduled after a run means rq's Retry put
# the job back; anything else (or a failed lookup, for example a job deleted by result_ttl=0) is unknown.
OUTCOMES = {
    "finished": "finished",
    "failed": "failed",
    "stopped": "stopped",
    "canceled": "canceled",
    "queued": "retried",
    "scheduled": "retried",
}

logger = logging.getLogger(otel.PLUGIN_LOGGER)
# One warning per key per process: a forked child (for example the rq scheduler) inherits the
# parent's set, so the set is reset when the PID changes.
_warned: set[str] = set()
_warned_pid: int | None = None


def _warn_once(key: str, message: str, *args) -> None:
    global _warned_pid
    pid = os.getpid()
    if pid != _warned_pid:
        _warned.clear()
        _warned_pid = pid
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


class RqModule:
    name = "rq"

    def __init__(self, queue_source=None) -> None:
        self._patched: list[tuple[type, str, object]] = []
        # None: netbox_queues, looked up at collection time.
        self._queue_source = queue_source
        self._queue_cache: tuple[int | None, list] = (None, [])
        self._depth_active = False

    def enabled(self, settings: Settings) -> bool:
        return settings.rq.enabled

    def install(self, ctx: Context) -> None:
        from rq.queue import Queue
        from rq.worker.base import BaseWorker
        from rq.worker.worker_classes import SimpleWorker, Worker

        from .. import bootstrap

        cfg = ctx.settings.rq
        if ctx.tracer_provider is not None and cfg.propagate_context:
            self._wrap(Queue, "enqueue_job", _enqueue_job_wrapper(), ENQUEUE_PARAMS)
        if ctx.role != bootstrap.ROLE_RQWORKER:
            return
        if ctx.meter_provider is not None:
            self._register_queue_depth(ctx)
        if not cfg.patch_worker:
            return
        breakers = {}
        if cfg.flush_breaker_threshold > 0:
            # One per signal: logs and traces can go to different endpoints, and an outage of one
            # must not make horses drop the other.
            breakers = {
                signal: FlushBreaker(cfg.flush_breaker_threshold, cfg.flush_breaker_cooldown)
                for signal in BREAKER_SIGNALS
            }
        if isinstance(inspect.getattr_static(BaseWorker, "is_horse", None), property):
            self._wrap(BaseWorker, "perform_job", _perform_job_wrapper(ctx, breakers), WORKER_PARAMS)
        else:
            logger.warning(
                "OpenTelemetry: rq BaseWorker.is_horse is not a property; the work-horse flush and job "
                "spans are disabled"
            )
        self._wrap(Worker, "fork_work_horse", _fork_work_horse_wrapper(breakers), WORKER_PARAMS)
        if ctx.meter_provider is not None:
            job_metrics = _JobMetrics(ctx.meter_provider)
            self._wrap(Worker, "execute_job", _execute_job_wrapper(job_metrics), WORKER_PARAMS)
            self._wrap(SimpleWorker, "execute_job", _execute_job_wrapper(job_metrics), WORKER_PARAMS)

    def after_fork(self, ctx: Context) -> None:
        pass

    def _register_queue_depth(self, ctx: Context) -> None:
        self._depth_active = True
        ctx.meter_provider.get_meter(JOB_SCOPE, __version__).create_observable_gauge(
            QUEUE_DEPTH,
            callbacks=[self._observe_queue_depth],
            unit="{job}",
            description="Jobs waiting in each NetBox RQ queue.",
        )

    def _observe_queue_depth(self, options) -> list:
        # Called by the metrics export thread, once per collection: one LLEN per queue.
        if not self._depth_active:
            return []
        try:
            pid = os.getpid()
            cached_pid, queues = self._queue_cache
            if cached_pid != pid:
                # Built once per process: django_rq.get_queue creates a Redis client per call. A
                # forked child (the rq scheduler) builds its own rather than share the parent's.
                source = self._queue_source or netbox_queues
                queues = list(source())
                self._queue_cache = (pid, queues)
        except Exception as exc:
            _warn_once("queue-source", "OpenTelemetry: could not list the RQ queues: %s", type(exc).__name__)
            return []
        observations = []
        for queue in queues:
            try:
                observations.append(otel.observation(queue.count, {"messaging.destination.name": queue.name}))
            except Exception as exc:
                _warn_once("queue-depth", "OpenTelemetry: could not read an RQ queue depth: %s", type(exc).__name__)
        return observations

    def shutdown(self) -> None:
        # The gauge cannot be unregistered from the meter; it reports nothing from now on.
        self._depth_active = False
        for cls, attr, original in reversed(self._patched):
            setattr(cls, attr, original)
        self._patched.clear()

    def _wrap(self, cls: type, attr: str, make_wrapper, expected: tuple[str, ...]) -> None:
        original = cls.__dict__.get(attr)
        if original is None:
            logger.warning(
                "OpenTelemetry: rq %s.%s is missing; this part of the RQ integration is disabled",
                cls.__name__,
                attr,
            )
            return
        if getattr(original, WRAPPED_ATTR, False):
            return
        params = tuple(inspect.signature(original).parameters)
        if params != expected:
            logger.warning(
                "OpenTelemetry: rq %s.%s has an unexpected signature %s; this part of the RQ integration is disabled",
                cls.__name__,
                attr,
                params,
            )
            return
        wrapper = functools.update_wrapper(make_wrapper(original), original)
        setattr(wrapper, WRAPPED_ATTR, True)
        setattr(cls, attr, wrapper)
        self._patched.append((cls, attr, original))


def _enqueue_job_wrapper():
    def make(original):
        def enqueue_job(self, job, *args, **kwargs):
            _store_context(job)
            return original(self, job, *args, **kwargs)

        return enqueue_job

    return make


def _store_context(job) -> None:
    try:
        meta = job.meta
        if not isinstance(meta, dict) or CONTEXT_META_KEY in meta:
            return
        carrier = otel.inject_trace_context()
        if carrier:
            meta[CONTEXT_META_KEY] = carrier
    except Exception as exc:
        _warn_once("enqueue", "OpenTelemetry: could not store trace context on an RQ job: %s", type(exc).__name__)


def job_function(job) -> str | None:
    """Qualified name of the job's callable, or None when the job data cannot be deserialised.

    rq stores only the method name for a method (and the object in `instance`). Every NetBox job is
    the JobRunner classmethod `handle`, so the class is what tells them apart.
    """
    try:
        func_name = job.func_name
        instance = job.instance
    except Exception:
        return None
    if not isinstance(func_name, str) or not func_name:
        return None
    if instance is None:
        return func_name
    cls = instance if isinstance(instance, type) else type(instance)
    return f"{cls.__module__}.{cls.__qualname__}.{func_name}"


def job_span_name(job) -> str:
    name = job_function(job)
    return f"rq.job {name}" if name else "rq.job"


def job_outcome(job) -> str:
    try:
        status = job.get_status(refresh=True)
    except Exception:
        return UNKNOWN
    return OUTCOMES.get(getattr(status, "value", status), UNKNOWN)


def netbox_queues() -> list:
    """Every queue in django-rq's RQ_QUEUES: NetBox's own and those of plugins."""
    import django_rq
    from django_rq.settings import QUEUES

    return [django_rq.get_queue(name) for name in QUEUES]


class _JobMetrics:
    def __init__(self, meter_provider) -> None:
        meter = meter_provider.get_meter(JOB_SCOPE, __version__)
        self._duration = meter.create_histogram(
            JOB_DURATION,
            unit="s",
            description="Time the RQ worker spent on a job, including the work-horse's run.",
            explicit_bucket_boundaries_advisory=JOB_DURATION_BUCKETS,
        )
        self._jobs = meter.create_counter(JOBS, unit="{job}", description="RQ jobs run, by outcome.")

    def record(self, job, queue, seconds: float) -> None:
        queue_name = getattr(queue, "name", None)
        attributes = {
            "messaging.destination.name": queue_name if isinstance(queue_name, str) else UNKNOWN,
            # Deserialises the job data (func_name, instance) after the horse exits; unknown if that fails.
            "code.function.name": job_function(job) or UNKNOWN,
            "netbox.rq.job.outcome": job_outcome(job),
        }
        self._duration.record(seconds, attributes)
        self._jobs.add(1, attributes)


def _execute_job_wrapper(metrics: _JobMetrics):
    def make(original):
        def execute_job(self, job, queue):
            # Runs in the worker parent: a forked horse never returns into execute_job.
            start = time.monotonic()
            try:
                return original(self, job, queue)
            finally:
                try:
                    metrics.record(job, queue, time.monotonic() - start)
                except Exception as exc:
                    _warn_once("job-metrics", "OpenTelemetry: could not record RQ job metrics: %s", type(exc).__name__)

        return execute_job

    return make


def job_attributes(job, queue) -> dict:
    attributes: dict = {"messaging.system": "rq"}
    queue_name = getattr(queue, "name", None)
    if isinstance(queue_name, str):
        attributes["messaging.destination.name"] = queue_name
    job_id = getattr(job, "id", None)
    if isinstance(job_id, str):
        attributes["messaging.message.id"] = job_id
    try:
        kwargs = job.kwargs
    except Exception:
        return attributes
    netbox_job = kwargs.get("job") if isinstance(kwargs, dict) else None
    meta = getattr(netbox_job, "_meta", None)
    if getattr(meta, "label_lower", None) != "core.job":
        return attributes
    pk = getattr(netbox_job, "pk", None)
    if isinstance(pk, int) and not isinstance(pk, bool):
        attributes["netbox.job.id"] = pk
    name = getattr(netbox_job, "name", None)
    if isinstance(name, str) and name:
        attributes["netbox.job.name"] = name
    return attributes


class _JobSpan:
    """CONSUMER span around one job. Never raises; without a tracer provider it does nothing."""

    def __init__(self, ctx: Context, worker, job, queue) -> None:
        self._ctx, self._worker, self._job, self._queue = ctx, worker, job, queue
        self._span = None
        self._token = None
        self._failed = False

    def __enter__(self):
        provider = self._ctx.tracer_provider
        if provider is None:
            return self
        try:
            parent = None
            if self._ctx.settings.rq.propagate_context:
                meta = getattr(self._job, "meta", None)
                if isinstance(meta, dict):
                    parent = otel.extract_trace_context(meta.get(CONTEXT_META_KEY))
            self._span = otel.start_span(
                provider,
                JOB_SCOPE,
                __version__,
                job_span_name(self._job),
                kind=otel.CONSUMER,
                parent=parent,
                attributes=job_attributes(self._job, self._queue),
            )
            self._token = otel.activate(self._span)
            push = getattr(self._worker, "push_exc_handler", None)
            if push is not None and isinstance(getattr(self._worker, "_exc_handlers", None), list):
                push(self._on_exception)
        except Exception as exc:
            _warn_once("span", "OpenTelemetry: could not start the RQ job span: %s", type(exc).__name__)
        return self

    def _on_exception(self, job, exc_type, exc_value, traceback) -> None:
        # rq calls this from handle_exception inside perform_job, while the span is current.
        try:
            if self._span is not None and exc_value is not None:
                otel.record_failure(self._span, exc_value)
                self._failed = True
        except Exception:
            pass
        return None  # None tells rq to keep walking its handler stack

    def finished(self, result) -> None:
        try:
            if self._span is not None and result is False and not self._failed:
                otel.set_error(self._span, "job failed")
        except Exception:
            pass

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            handlers = getattr(self._worker, "_exc_handlers", None)
            if isinstance(handlers, list) and self._on_exception in handlers:
                handlers.remove(self._on_exception)
        except Exception as exc:
            _warn_once(
                "span-handler", "OpenTelemetry: could not remove the RQ job exception handler: %s", type(exc).__name__
            )
        try:
            if self._span is not None:
                if exc_value is not None and not self._failed:
                    otel.record_failure(self._span, exc_value)
                if self._token is not None:
                    otel.deactivate(self._token)
                self._span.end()
        except Exception as exc:
            _warn_once("span-end", "OpenTelemetry: could not end the RQ job span: %s", type(exc).__name__)
        return False


def _perform_job_wrapper(ctx: Context, breakers: dict[str, FlushBreaker] | None = None):
    breakers = breakers or {}

    def make(original):
        def perform_job(self, job, queue):
            before = _outcomes_or_none() if breakers and getattr(self, "is_horse", False) else None
            try:
                with _JobSpan(ctx, self, job, queue) as span:
                    result = original(self, job, queue)
                    span.finished(result)
                    return result
            finally:
                # Only a forked work-horse exits (os._exit) right after this returns. A SimpleWorker
                # runs jobs in the long-lived worker process, whose batch processors flush normally.
                if getattr(self, "is_horse", False):
                    _flush_horse(ctx, breakers, before)

        return perform_job

    return make


def _outcomes_or_none() -> dict[str, otel.ExportOutcomes] | None:
    try:
        return {signal: otel.export_outcomes(signal) for signal in BREAKER_SIGNALS}
    except Exception:
        return None


def _flush_horse(
    ctx: Context,
    breakers: dict[str, FlushBreaker] | None = None,
    before: dict[str, otel.ExportOutcomes] | None = None,
) -> None:
    from .. import bootstrap

    cfg = ctx.settings.rq
    breakers = dict(breakers or {})
    skip = set()
    for signal, breaker in list(breakers.items()):
        try:
            if breaker.should_skip():
                breaker.record_skip(otel.buffered_records(signal))
                skip.add(signal)
        except Exception as exc:
            _warn_once("breaker", "OpenTelemetry: RQ work-horse flush breaker failed: %s", type(exc).__name__)
            breakers.pop(signal)
    results: dict[str, bool] = {}
    try:
        results = bootstrap.flush_signals(cfg.flush_timeout, frozenset(skip))
        if not all(results.values()):
            logger.warning("OpenTelemetry: flush in the RQ work-horse did not finish within %s s", cfg.flush_timeout)
    except Exception as exc:
        _warn_once("flush", "OpenTelemetry: flush in the RQ work-horse failed: %s", type(exc).__name__)
    if before is None:
        return
    for signal, completed in results.items():
        breaker = breakers.get(signal)
        if breaker is None or signal not in before:
            continue
        try:
            _record_flush_outcome(breaker, signal, cfg, completed, before[signal], otel.export_outcomes(signal))
        except Exception as exc:
            _warn_once("breaker", "OpenTelemetry: RQ work-horse flush breaker failed: %s", type(exc).__name__)


def _record_flush_outcome(breaker: FlushBreaker, signal: str, cfg, completed: bool, before, after) -> None:
    label = SIGNAL_LABELS[signal]
    if not completed or after.failed > before.failed:
        if breaker.record_failure():
            logger.warning(
                "OpenTelemetry: the RQ work-horse flush of %s did not finish or failed %s times in a row; "
                "work-horses now skip it, with one full attempt every %s s until an export succeeds. "
                "The %s buffered in a work-horse that skips it are dropped",
                label,
                cfg.flush_breaker_threshold,
                cfg.flush_breaker_cooldown,
                label,
            )
        return
    if after.succeeded == before.succeeded:
        # Nothing was exported: no evidence either way about the endpoint.
        return
    summary = breaker.record_success()
    if summary is None:
        return
    uncounted = f"; they could not be counted in {summary.uncounted} of those flushes" if summary.uncounted else ""
    logger.warning(
        "OpenTelemetry: the RQ work-horse flush of %s succeeded again; work-horses flush them normally. "
        "%s flushes were skipped, dropping %s buffered %s%s",
        label,
        summary.skipped,
        summary.dropped,
        label,
        uncounted,
    )


class SkipSummary(NamedTuple):
    skipped: int
    dropped: int
    uncounted: int


# failures (consecutive), retry_at (monotonic seconds), skipped, dropped, uncounted
_BREAKER_STATE = struct.Struct("=qdqqq")


class FlushBreaker:
    """Circuit breaker for the work-horse flush of one signal, shared by a worker and its horses.

    The worker parent allocates an anonymous shared memory page (MAP_SHARED, so writes are seen
    across fork) before its first fork; each horse reads and updates it, and the worker itself
    never touches it after that. rq runs one horse at a time per worker and waits for it to exit
    before forking the next, so there is never more than one writer and no lock is needed. A
    horse that is SIGKILLed mid-update can leave one field stale; the next outcome corrects it.

    After `threshold` consecutive failed flushes (the flush hit rq.flush_timeout, or an export
    failed), horses skip their flush. Once `cooldown` seconds have passed, the next horse flushes
    in full: a success closes the breaker, a failure keeps it open for another `cooldown`. Only a
    horse of the process that prepared the breaker uses it (os.getppid() check), so another fork
    of the worker (the rq scheduler), or a process forked by a job, is unaffected; each worker of
    an rq worker pool allocates its own page.
    """

    def __init__(self, threshold: int, cooldown: float, clock=None) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._mem: mmap.mmap | None = None
        self._owner: int | None = None

    def prepare(self) -> None:
        """Worker parent, before each fork: allocate the shared page once per process. May raise."""
        pid = os.getpid()
        if self._owner == pid and self._mem is not None:
            return
        self._mem, self._owner = None, None
        mem = mmap.mmap(-1, _BREAKER_STATE.size)
        mem[:] = _BREAKER_STATE.pack(0, 0.0, 0, 0, 0)
        self._mem, self._owner = mem, pid

    def _now(self) -> float:
        return (self._clock or time.monotonic)()

    def _active(self) -> bool:
        return self._mem is not None and self._owner == os.getppid()

    def _read(self) -> list:
        return list(_BREAKER_STATE.unpack(self._mem[:]))

    def _write(self, state: list) -> None:
        self._mem[:] = _BREAKER_STATE.pack(*state)

    def should_skip(self) -> bool:
        if not self._active():
            return False
        failures, retry_at, *_ = self._read()
        return failures >= self._threshold and self._now() < retry_at

    def record_skip(self, dropped: int | None) -> None:
        if not self._active():
            return
        state = self._read()
        state[2] += 1
        if dropped is None:
            state[4] += 1
        else:
            state[3] += dropped
        self._write(state)

    def record_failure(self) -> bool:
        """True when this failure opened the breaker."""
        if not self._active():
            return False
        state = self._read()
        was_open = state[0] >= self._threshold
        state[0] += 1
        if state[0] >= self._threshold:
            state[1] = self._now() + self._cooldown
        self._write(state)
        return not was_open and state[0] >= self._threshold

    def record_success(self) -> SkipSummary | None:
        """Close the breaker. Returns what was skipped when it had been open, otherwise None."""
        if not self._active():
            return None
        failures, _, skipped, dropped, uncounted = self._read()
        self._write([0, 0.0, 0, 0, 0])
        if failures < self._threshold and not skipped:
            return None
        return SkipSummary(skipped, dropped, uncounted)


def _fork_work_horse_wrapper(breakers: dict[str, FlushBreaker] | None = None):
    breakers = breakers or {}

    def make(original):
        def fork_work_horse(self, job, queue):
            from .. import bootstrap

            for breaker in breakers.values():
                try:
                    breaker.prepare()
                except Exception as exc:
                    _warn_once(
                        "breaker-prepare",
                        "OpenTelemetry: could not set up the RQ work-horse flush breaker, horses always flush "
                        "in full: %s",
                        type(exc).__name__,
                    )
            bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
            try:
                return original(self, job, queue)
            finally:
                bootstrap.set_next_fork_role(None)

        return fork_work_horse

    return make
