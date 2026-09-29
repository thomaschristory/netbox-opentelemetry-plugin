"""Per-process installation of the plugin's OTel pipeline.

install() is safe to call more than once: the second call in the same process returns the
existing context. Nothing in here raises for configuration or exporter problems; those are
logged as warnings on the plugin logger and NetBox keeps running.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import conf, otel
from .modules.audit import AuditModule
from .modules.base import Context, Module
from .modules.logs import LogsModule
from .modules.rq import RqModule
from .modules.runtime import RuntimeModule
from .modules.traces import TracesModule
from .version import __version__

logger = logging.getLogger(otel.PLUGIN_LOGGER)

ROLE_WEB = "web"
ROLE_RQWORKER = "rqworker"
ROLE_RQ_HORSE = "rq_horse"
ROLE_MANAGEMENT = "management"
ROLE_RUNSERVER_PARENT = "runserver_parent"

# Short management commands never start trace exporter threads (SPEC 4.1). The RQ work-horse
# inherits the rqworker's provider and rebuilds it after fork.
TRACE_ROLES = frozenset({ROLE_WEB, ROLE_RQWORKER})

# Metrics run only in the long-lived web and rqworker processes (SPEC 4.1, 6.4). A forked RQ
# work-horse records into a no-op provider and never exports; job metrics come from its parent.
METRIC_ROLES = frozenset({ROLE_WEB, ROLE_RQWORKER})

# A bulk edit or bulk import can write thousands of ObjectChange rows inside a single commit; the
# default BatchLogRecordProcessor queue (2048) is sized for scattered log lines, not that burst.
# Sized generously so a single bulk operation cannot overrun it and silently drop audit records.
AUDIT_QUEUE_SIZE = 20_000

# user:password@ in URLs or host strings, including percent-encoded credentials. Deliberately
# favours false positives: any "word:word@" shape is masked, not just valid userinfo. Only ever
# applied to a message already bounded by _MESSAGE_LIMIT (see _describe) because this pattern can
# backtrack catastrophically on long inputs with a ":" but no "@" (for example "a" * n + ":" + "b" * n).
_USERINFO = re.compile(r"[A-Za-z0-9._~%!$&'()*+,;=-]+:[^\s/@'\"]+@")

# scheme://TOKEN@host userinfo with no colon (a bare token, not a user:password pair). Also only
# ever applied to a message already bounded by _MESSAGE_LIMIT, for the same reason as _USERINFO.
_TOKEN_USERINFO = re.compile(r"(//)[^\s/@'\"]+@")

_MESSAGE_LIMIT = 2000

UWSGI_THREADS_WARNING = (
    "OpenTelemetry: uWSGI is running without thread support, so the exporter's background thread "
    "cannot run and nothing will be exported. Set `enable-threads = true` in the uWSGI configuration."
)


@dataclass
class _State:
    pid: int
    context: Context | None
    modules: list[Module] = field(default_factory=list)
    owns_logger_provider: bool = False
    owns_tracer_provider: bool = False
    metrics_pipeline: otel.MetricsPipeline | None = None
    netbox_version: str = "unknown"


_state: _State | None = None
_lock = threading.RLock()
_atexit_registered = False
_next_fork_role: str | None = None


def detect_role(argv: Sequence[str], env: Mapping[str, str]) -> str:
    if len(argv) >= 2 and os.path.basename(argv[0]) == "manage.py":
        command = argv[1]
        if command == "rqworker":
            return ROLE_RQWORKER
        if command == "runserver":
            # The autoreloader parent only watches files; the child it spawns has RUN_MAIN=true.
            if "--noreload" in argv or env.get("RUN_MAIN") == "true":
                return ROLE_WEB
            return ROLE_RUNSERVER_PARENT
        return ROLE_MANAGEMENT
    return ROLE_WEB


def install(
    user_config: Mapping | None,
    *,
    env: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
    netbox_version: str = "unknown",
) -> Context | None:
    global _state, _atexit_registered
    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv

    with _lock:
        if _state is not None and _state.pid == os.getpid():
            return _state.context

        settings = conf.resolve(user_config, env)
        for message in settings.warnings:
            logger.warning("OpenTelemetry: %s", message)

        role = detect_role(argv, env)
        if not settings.enabled or role == ROLE_RUNSERVER_PARENT:
            _state = _State(pid=os.getpid(), context=None)
            return None

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("OpenTelemetry resolved config: %s", settings.redacted())

        resource = _build_resource(settings, role, netbox_version)
        ctx = Context(settings=settings, role=role, resource=resource)
        state = _State(pid=os.getpid(), context=ctx, netbox_version=netbox_version)

        if settings.log_exporter is not None:
            _setup_logger_provider(ctx, state)

        if settings.traces.enabled and role in TRACE_ROLES:
            _setup_tracer_provider(ctx, state)

        if settings.metrics.enabled and role in METRIC_ROLES:
            _setup_meter_provider(ctx, state)

        for module in _candidate_modules(ctx):
            if not module.enabled(settings):
                continue
            try:
                module.install(ctx)
            except Exception as exc:
                logger.warning("OpenTelemetry: %s module disabled: %s", module.name, _describe(exc, settings))
                continue
            state.modules.append(module)

        uwsgi_module = _uwsgi_module()
        if uwsgi_module is not None:
            try:
                _integrate_uwsgi(uwsgi_module)
            except Exception as exc:
                logger.warning("OpenTelemetry: uWSGI integration failed: %s", _describe(exc, settings))

        _state = state
        if not _atexit_registered:
            atexit.register(shutdown)
            _atexit_registered = True
        return ctx


def _build_resource(settings: conf.Settings, role: str, netbox_version: str):
    return otel.build_resource(
        settings.service_name,
        settings.resource_attributes,
        service_version=netbox_version,
        plugin_version=__version__,
        role=role,
    )


def shutdown() -> None:
    global _state
    with _lock:
        state = _state
        if state is None or state.pid != os.getpid():
            return
        _state = None
        ctx = state.context
        # Remove handlers first so records emitted during shutdown do not hit a closed provider.
        for module in reversed(state.modules):
            try:
                module.shutdown()
            except Exception as exc:
                logger.warning("OpenTelemetry: %s module shutdown failed: %s", module.name, type(exc).__name__)
        if state.metrics_pipeline is not None:
            pipeline, state.metrics_pipeline = state.metrics_pipeline, None
            try:
                pipeline.shutdown(ctx.settings.metrics.exporter.timeout)
            except Exception as exc:
                logger.warning("OpenTelemetry: meter provider shutdown failed: %s", type(exc).__name__)
        if ctx is not None and ctx.meter_provider is not None:
            # Later measurements (for example from a request racing the shutdown) go nowhere.
            with contextlib.suppress(Exception):
                ctx.meter_provider.set_delegate(otel.noop_meter_provider())
        if state.owns_tracer_provider and ctx is not None and ctx.tracer_provider is not None:
            try:
                ctx.tracer_provider.shutdown()
            except Exception as exc:
                logger.warning("OpenTelemetry: tracer provider shutdown failed: %s", type(exc).__name__)
        if state.owns_logger_provider and ctx is not None and ctx.logger_provider is not None:
            try:
                ctx.logger_provider.shutdown()
            except Exception as exc:
                logger.warning("OpenTelemetry: logger provider shutdown failed: %s", type(exc).__name__)


def reinit_after_fork() -> None:
    """Rebuild per-process OTel state in a forked child.

    The SDK restarts its batch threads after fork but keeps the parent's service.instance.id and
    shares the parent's exporter connection. This gives the child its own Resource, exporter and
    LoggerProvider. Idempotent: a second call in the same process does nothing. If this process
    owned its LoggerProvider and the rebuild fails, it detaches the logging handler and points the
    tracer provider at a detached provider rather than keep using the inherited, now-orphaned
    providers. The module after-fork hooks run whether or not the rebuild succeeded, so the
    traces module re-applies the baggage-free propagator in every child.

    This never calls shutdown() (or anything else) on an object inherited from the parent: any
    lock inside such an object (a threading.Lock, a Condition, an SSL/urllib3 connection pool
    mutex, a gRPC channel's internal state) was copied by fork() in whatever state it happened to
    be in at that instant. If some other parent thread held that lock at fork time, the copy in
    the child is born locked with no owner able to release it, and touching it deadlocks the
    child forever. We simply stop referencing the inherited provider and let its worker thread and
    exporter connection sit idle and unused; that idle thread/connection is the accepted cost.
    """
    global _next_fork_role
    with _lock:
        role_hint, _next_fork_role = _next_fork_role, None
        state = _state
        if state is None or state.pid == os.getpid():
            return
        state.pid = os.getpid()
        ctx = state.context
        if ctx is None:
            return
        try:
            _rebuild_for_child(ctx, state, role_hint)
        except Exception as exc:
            logger.warning("OpenTelemetry: re-initialisation after fork failed: %s", _describe(exc, ctx.settings))
            if state.owns_tracer_provider and ctx.tracer_provider is not None:
                # Same reason as below: never export through the inherited exporter connection.
                state.owns_tracer_provider = False
                with contextlib.suppress(Exception):
                    ctx.tracer_provider.set_delegate(otel.detached_tracer_provider())
            role = role_hint or ctx.role
            if ctx.meter_provider is not None and (state.metrics_pipeline is not None or role not in METRIC_ROLES):
                # No exporter of our own in this process: record nowhere rather than into inherited state.
                state.metrics_pipeline = None
                with contextlib.suppress(Exception):
                    ctx.meter_provider.set_delegate(otel.noop_meter_provider())
            if state.owns_logger_provider:
                # This process could not build its own provider. The inherited one shares the parent's
                # exporter connection, so stop exporting from this process instead of using it.
                state.owns_logger_provider = False
                ctx.logger_provider = None
            # Whatever the plugin owns: the logs module detaches its handler when the provider was
            # dropped above, and the traces module re-wraps a propagator replaced before the fork.
            _run_modules_after_fork(ctx, state)


def set_next_fork_role(role: str | None) -> None:
    """Label the child of the next fork (the RQ integration sets this around fork_work_horse).

    One-shot: the hint is consumed by the next fork and then cleared, in both the parent process
    (see _after_fork_in_parent) and the child (see reinit_after_fork), so it applies to exactly
    one fork.
    """
    global _next_fork_role
    _next_fork_role = role


def force_flush(timeout: float) -> bool:
    """Flush every provider of this process (logs, traces and, where this process owns one, the
    metrics pipeline) in parallel, waiting at most `timeout` seconds in total. Never raises.

    True means every flush call returned within the deadline, or there was nothing to flush: no
    installed state, a process whose PID does not match the installed state (treated as nothing
    to flush here), or no providers. It does not mean the records were exported: a failure inside
    a provider's force_flush is swallowed and still counts as that flush having returned, since
    the flush call itself did not hang past the deadline. False is returned when the deadline
    passed before every flush finished, or when a helper thread itself could not be started.

    Each provider's flush runs on its own helper thread, in parallel, so a stuck export cannot
    hold the caller beyond the deadline.
    """
    state = _state
    if state is None or state.pid != os.getpid() or state.context is None:
        return True
    ctx = state.context
    flushables = [p for p in (ctx.logger_provider, ctx.tracer_provider) if p is not None]
    if state.metrics_pipeline is not None:
        # Never in a work-horse: it has no pipeline (SPEC 6.4).
        flushables.append(state.metrics_pipeline)
    if not flushables:
        return True
    deadline = time.monotonic() + timeout
    done_events = []
    for flushable in flushables:
        done = threading.Event()

        def run(flushable=flushable, done=done) -> None:
            try:
                flushable.force_flush(timeout_millis=int(timeout * 1000))
            except Exception:
                pass
            finally:
                done.set()

        try:
            threading.Thread(target=run, name="otel-flush", daemon=True).start()
        except Exception:
            # Thread creation can fail (resource limits, interpreter finalization). Nothing was
            # started, so there is nothing to wait on: report the flush as not completed.
            return False
        done_events.append(done)
    return all(done.wait(max(0.0, deadline - time.monotonic())) for done in done_events)


def _rebuild_for_child(ctx: Context, state: _State, role_hint: str | None) -> None:
    """Build a new role, Resource and (if we own them) LoggerProvider, tracer SDK provider and metrics
    pipeline for this process. The metrics pipeline is rebuilt only in the web and rqworker roles; a
    work-horse gets a no-op meter provider instead.

    Nothing is assigned onto ctx until every new provider has been built successfully, so a
    failure here (for example the exporter config referencing a now-unreadable certificate file)
    leaves ctx.role, ctx.resource, ctx.logger_provider and ctx.tracer_provider exactly as they
    were: still consistent with each other, still the values inherited from the parent at fork
    time.

    The role changes only when the parent announced the fork (see `set_next_fork_role`); an
    unannounced fork of an rqworker process (for example the RQ scheduler's own child) keeps the
    rqworker role.

    The old logger provider (if we owned one) is never shut down here: see reinit_after_fork for
    why. The tracer provider is different: we own the SwitchableTracerProvider handed to
    instrumentors for the process lifetime, and only replace the SDK provider behind it, so there
    is nothing of ours to shut down; the old SDK provider is simply dropped, for the same
    never-touch-inherited-state reason. The metrics pipeline is handled the same way: the inherited
    one (export thread gone, locks copied) is dropped, never flushed or shut down, and only the
    delegate of the SwitchableMeterProvider changes.
    """
    role = role_hint or ctx.role
    resource = _build_resource(ctx.settings, role, state.netbox_version)
    # Build everything first, assign afterwards: a failure leaves ctx exactly as inherited.
    new_logger_provider = None
    if state.owns_logger_provider and ctx.logger_provider is not None:
        # A fresh exporter gives the child its own HTTP session or gRPC channel instead of
        # sharing the parent's keep-alive connections.
        exporter = otel.build_log_exporter(ctx.settings.log_exporter)
        max_queue_size = AUDIT_QUEUE_SIZE if ctx.settings.audit.enabled else None
        new_logger_provider = otel.build_logger_provider(resource, exporter, max_queue_size=max_queue_size)
    # Child-owned objects not yet referenced anywhere else: shut them down if a later build fails or
    # they leak silently, unlike the inherited providers still on ctx, which this function never touches.
    built = []
    if new_logger_provider is not None:
        built.append(new_logger_provider)
    new_tracer_provider = None
    new_pipeline = None
    try:
        if state.owns_tracer_provider and ctx.tracer_provider is not None:
            new_tracer_provider = _build_tracer_provider(ctx.settings, resource)
            built.append(new_tracer_provider)
        if state.metrics_pipeline is not None and role in METRIC_ROLES:
            new_pipeline = _build_metrics_pipeline(ctx.settings, resource)
    except Exception:
        for obj in built:
            with contextlib.suppress(Exception):
                obj.shutdown()
        raise
    try:
        if new_logger_provider is not None:
            ctx.logger_provider = new_logger_provider
        if new_tracer_provider is not None:
            # Instrumentors hold the switchable provider; only the SDK provider behind it changes.
            ctx.tracer_provider.set_delegate(new_tracer_provider)
        if ctx.meter_provider is not None:
            if role not in METRIC_ROLES:
                # An RQ work-horse exports no metrics (SPEC 6.4). It must not record into the inherited
                # SDK provider either: the parent's export thread may have held its locks at fork time.
                state.metrics_pipeline = None
                with contextlib.suppress(Exception):
                    ctx.meter_provider.set_delegate(otel.noop_meter_provider())
            elif new_pipeline is not None:
                # The inherited pipeline (thread gone, locks copied) is dropped, never shut down.
                ctx.meter_provider.set_delegate(new_pipeline.provider)
                state.metrics_pipeline = new_pipeline
    except Exception:
        # A swap failed. The caller's failure path switches this process to no-op providers and
        # drops the new ones, so stop their threads here (all child-owned, never inherited).
        for obj in built:
            with contextlib.suppress(Exception):
                obj.shutdown()
        if new_pipeline is not None:
            with contextlib.suppress(Exception):
                new_pipeline.shutdown(0)
        raise
    ctx.role = role
    ctx.resource = resource
    _run_modules_after_fork(ctx, state)


def _run_modules_after_fork(ctx: Context, state: _State) -> None:
    for module in state.modules:
        try:
            module.after_fork(ctx)
        except Exception as exc:
            logger.warning("OpenTelemetry: %s module after-fork failed: %s", module.name, _describe(exc, ctx.settings))


def _before_fork() -> None:
    # Holding the lock across fork() means no other thread can be half way through install()
    # or shutdown() when the child's copy of the state is taken.
    _lock.acquire()


def _after_fork_in_parent() -> None:
    global _next_fork_role
    # Some embedders run the parent hook without the before hook. Fork hooks must never raise,
    # so releasing a lock we may not hold is tolerated rather than propagated.
    with contextlib.suppress(RuntimeError):
        _lock.release()
    # The hint is a copy-on-write page shared with the child at fork time; clearing it here only
    # affects this (parent) process's own memory. It makes the hint apply to exactly one fork in
    # the parent too, matching the child-side clear in reinit_after_fork.
    _next_fork_role = None


def _after_fork_in_child() -> None:
    global _lock
    # The child must not reuse a lock whose state was copied from the parent.
    _lock = threading.RLock()
    reinit_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


def _setup_logger_provider(ctx: Context, state: _State) -> None:
    existing = otel.existing_logger_provider()
    if existing is not None:
        logger.info("OpenTelemetry: reusing the LoggerProvider configured outside the plugin")
        ctx.logger_provider = existing
        return
    try:
        exporter = otel.build_log_exporter(ctx.settings.log_exporter)
        max_queue_size = AUDIT_QUEUE_SIZE if ctx.settings.audit.enabled else None
        ctx.logger_provider = otel.build_logger_provider(ctx.resource, exporter, max_queue_size=max_queue_size)
        state.owns_logger_provider = True
    except Exception as exc:
        logger.warning("OpenTelemetry: log export disabled: could not build exporter: %s", _describe(exc, ctx.settings))


def _build_tracer_provider(settings: conf.Settings, resource):
    cfg = settings.traces
    return otel.build_tracer_provider(
        resource, otel.build_span_exporter(cfg.exporter), otel.build_sampler(cfg.sampler, cfg.sampler_arg)
    )


def _setup_tracer_provider(ctx: Context, state: _State) -> None:
    existing = otel.existing_tracer_provider()
    if existing is not None:
        logger.info("OpenTelemetry: reusing the TracerProvider configured outside the plugin")
        ctx.tracer_provider = existing
        return
    try:
        provider = _build_tracer_provider(ctx.settings, ctx.resource)
    except Exception as exc:
        logger.warning(
            "OpenTelemetry: trace export disabled: could not build exporter: %s", _describe(exc, ctx.settings)
        )
        return
    ctx.tracer_provider = otel.SwitchableTracerProvider(provider)
    state.owns_tracer_provider = True


def _build_metrics_pipeline(settings: conf.Settings, resource) -> otel.MetricsPipeline:
    cfg = settings.metrics
    return otel.MetricsPipeline(
        resource, otel.build_metric_exporter(cfg.exporter), interval=cfg.export_interval, timeout=cfg.exporter.timeout
    )


def _setup_meter_provider(ctx: Context, state: _State) -> None:
    existing = otel.existing_meter_provider()
    if existing is not None:
        logger.info("OpenTelemetry: reusing the MeterProvider configured outside the plugin")
        # Wrapped all the same, so the plugin's instruments can be switched off in a forked work-horse.
        ctx.meter_provider = otel.SwitchableMeterProvider(existing)
        return
    try:
        pipeline = _build_metrics_pipeline(ctx.settings, ctx.resource)
    except Exception as exc:
        logger.warning(
            "OpenTelemetry: metric export disabled: could not build exporter: %s", _describe(exc, ctx.settings)
        )
        return
    ctx.meter_provider = otel.SwitchableMeterProvider(pipeline.provider)
    state.metrics_pipeline = pipeline


def _candidate_modules(ctx: Context) -> list[Module]:
    modules: list[Module] = []
    if ctx.logger_provider is not None:
        modules.append(LogsModule())
    # The audit receiver also counts changes, which works without a log pipeline.
    if ctx.logger_provider is not None or ctx.meter_provider is not None:
        modules.append(AuditModule())
    # One set of instrumentors serves spans and HTTP metrics.
    if ctx.tracer_provider is not None or ctx.meter_provider is not None:
        modules.append(TracesModule())
    # RQ: worker wraps in the rqworker process; the enqueue wrap wherever spans are recorded.
    if ctx.role == ROLE_RQWORKER or ctx.tracer_provider is not None:
        modules.append(RqModule())
    if ctx.meter_provider is not None:
        modules.append(RuntimeModule())
    return modules


def _describe(exc: BaseException, settings: conf.Settings) -> str:
    """Format exc for a log message, redacting exporter header values. Never raises.

    Called from inside except blocks, including in the at-fork hook, so a badly behaved exception
    (for example one whose __str__ itself raises) must never turn into an exception escaping the
    hook that is handling it.
    """
    try:
        message = str(exc)
        # Run the known-literal replacements on the FULL message first: str.replace is linear, so
        # this is safe on arbitrarily long input, and it means a secret that would straddle the
        # truncation cut below is still matched and redacted in full.
        for exporter in settings.exporters():
            message = message.replace(exporter.endpoint, conf._redact_userinfo(exporter.endpoint))
            for value in exporter.headers.values():
                if value:
                    message = message.replace(value, conf.REDACTED)
        if len(message) > _MESSAGE_LIMIT:
            message = message[:_MESSAGE_LIMIT]
            # Drop a partial token left dangling at the cut (for example the prefix of a secret
            # that was not caught above, such as URL userinfo) by cutting back to the last
            # whitespace in the kept text. If the kept text has no whitespace at all, we cannot
            # tell whether it ends mid-token, so drop it entirely rather than risk leaking a
            # prefix of a secret.
            cut = None
            for i in range(len(message) - 1, -1, -1):
                if message[i].isspace():
                    cut = i
                    break
            message = message[:cut] if cut is not None else ""
            message += " [truncated]"
        # Only applied to the now-bounded text: these patterns can backtrack catastrophically on
        # long input containing ":" or "//" but no "@".
        if "@" in message:
            message = _USERINFO.sub(f"{conf.REDACTED}@", message)
            message = _TOKEN_USERINFO.sub(rf"\1{conf.REDACTED}@", message)
        return f"{type(exc).__name__}: {message}"
    except Exception:
        return type(exc).__name__


def _uwsgi_module():
    """Return the uwsgi module when running inside uWSGI, otherwise None.

    The module is provided by the uWSGI runtime itself and never exists as an installed package.
    """
    try:
        import uwsgi
    except Exception:
        return None
    return uwsgi


def _integrate_uwsgi(uwsgi_module) -> None:
    # uWSGI forks workers in C and only runs Python's at-fork hooks with py-call-osafterfork.
    # post_fork_hook is called in every worker after fork. If both fire, the second
    # re-initialisation is a no-op because it is keyed on the PID.
    previous = getattr(uwsgi_module, "post_fork_hook", None)
    if not getattr(previous, "_netbox_otel", False):

        def post_fork_hook():
            try:
                if previous is not None:
                    previous()
            finally:
                _after_fork_in_child()

        post_fork_hook._netbox_otel = True
        uwsgi_module.post_fork_hook = post_fork_hook
    opt = getattr(uwsgi_module, "opt", None) or {}
    if uwsgi_threads_disabled(opt, "pyuwsgi" in sys.modules):
        logger.warning(UWSGI_THREADS_WARNING)


def uwsgi_threads_disabled(opt: Mapping, embedded_in_python: bool) -> bool:
    """True when uWSGI will not run threads started by the application.

    pyuwsgi (the PyPI package: uWSGI embedded in an already running interpreter) always has
    thread support. The classic uwsgi binary needs enable-threads, which --threads implies.
    """
    if embedded_in_python:
        return False
    return not (_truthy(opt.get("enable-threads")) or _truthy(opt.get("threads")))


def _truthy(value) -> bool:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)
