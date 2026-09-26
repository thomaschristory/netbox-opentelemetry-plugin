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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import conf, otel
from .modules.base import Context, Module
from .modules.logs import LogsModule
from .modules.rq import RqModule
from .version import __version__

logger = logging.getLogger(otel.PLUGIN_LOGGER)

ROLE_WEB = "web"
ROLE_RQWORKER = "rqworker"
ROLE_RQ_HORSE = "rq_horse"
ROLE_MANAGEMENT = "management"
ROLE_RUNSERVER_PARENT = "runserver_parent"

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
        # Remove handlers first so records emitted during shutdown do not hit a closed provider.
        for module in reversed(state.modules):
            try:
                module.shutdown()
            except Exception as exc:
                logger.warning("OpenTelemetry: %s module shutdown failed: %s", module.name, type(exc).__name__)
        ctx = state.context
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
    owned its LoggerProvider and the rebuild fails, it detaches the logging handler and exports
    nothing rather than keep using the inherited, now-orphaned provider.

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
            if state.owns_logger_provider:
                # This process could not build its own provider. The inherited one shares the parent's
                # exporter connection, so stop exporting from this process instead of using it.
                state.owns_logger_provider = False
                ctx.logger_provider = None
                for module in state.modules:
                    with contextlib.suppress(Exception):
                        module.after_fork(ctx)


def set_next_fork_role(role: str | None) -> None:
    """Label the child of the next fork (the RQ integration sets this around fork_work_horse).

    One-shot: the hint is consumed by the next fork and then cleared, in both the parent process
    (see _after_fork_in_parent) and the child (see reinit_after_fork), so it applies to exactly
    one fork.
    """
    global _next_fork_role
    _next_fork_role = role


def force_flush(timeout: float) -> bool:
    """Flush this process's log provider, waiting at most `timeout` seconds. Never raises.

    True means the flush call returned within the deadline, or there was nothing to flush: no
    installed state, a process whose PID does not match the installed state (treated as nothing
    to flush here), or no LoggerProvider. It does not mean the records were exported: a failure
    inside the provider's force_flush is swallowed and still reported as True, since the flush
    call itself did not hang past the deadline. False is returned when the deadline passed before
    the flush finished, or when the helper thread itself could not be started.

    The flush runs on a helper thread so a stuck export cannot hold the caller beyond the
    deadline.
    """
    state = _state
    if state is None or state.pid != os.getpid() or state.context is None:
        return True
    provider = state.context.logger_provider
    if provider is None:
        return True
    done = threading.Event()

    def run() -> None:
        try:
            provider.force_flush(timeout_millis=int(timeout * 1000))
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
    return done.wait(timeout)


def _rebuild_for_child(ctx: Context, state: _State, role_hint: str | None) -> None:
    """Build a new role, Resource and (if we own it) LoggerProvider for this process.

    Nothing is assigned onto ctx until the new LoggerProvider has been built successfully, so a
    failure here (for example the exporter config referencing a now-unreadable certificate file)
    leaves ctx.role, ctx.resource and ctx.logger_provider exactly as they were: still consistent
    with each other, still the values inherited from the parent at fork time.

    The role changes only when the parent announced the fork (see `set_next_fork_role`); an
    unannounced fork of an rqworker process (for example the RQ scheduler's own child) keeps the
    rqworker role.

    The old provider (if we owned one) is never shut down here: see reinit_after_fork for why.
    """
    role = role_hint or ctx.role
    resource = _build_resource(ctx.settings, role, state.netbox_version)
    if state.owns_logger_provider and ctx.logger_provider is not None:
        # A fresh exporter gives the child its own HTTP session or gRPC channel instead of
        # sharing the parent's keep-alive connections.
        exporter = otel.build_log_exporter(ctx.settings.log_exporter)
        ctx.logger_provider = otel.build_logger_provider(resource, exporter)
    ctx.role = role
    ctx.resource = resource
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
        ctx.logger_provider = otel.build_logger_provider(ctx.resource, exporter)
        state.owns_logger_provider = True
    except Exception as exc:
        logger.warning("OpenTelemetry: log export disabled: could not build exporter: %s", _describe(exc, ctx.settings))


def _candidate_modules(ctx: Context) -> list[Module]:
    modules: list[Module] = []
    if ctx.logger_provider is not None:
        modules.append(LogsModule())
    if ctx.role == ROLE_RQWORKER:
        modules.append(RqModule())
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
        exporter = settings.log_exporter
        if exporter is not None:
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
