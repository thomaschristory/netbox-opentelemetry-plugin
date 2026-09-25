"""Per-process installation of the plugin's OTel pipeline.

install() is safe to call more than once: the second call in the same process returns the
existing context. Nothing in here raises for configuration or exporter problems; those are
logged as warnings on the plugin logger and NetBox keeps running.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import conf, otel
from .modules.base import Context, Module
from .modules.logs import LogsModule
from .version import __version__

logger = logging.getLogger(otel.PLUGIN_LOGGER)

ROLE_WEB = "web"
ROLE_RQWORKER = "rqworker"
ROLE_MANAGEMENT = "management"
ROLE_RUNSERVER_PARENT = "runserver_parent"


@dataclass
class _State:
    pid: int
    context: Context | None
    modules: list[Module] = field(default_factory=list)
    owns_logger_provider: bool = False


_state: _State | None = None
_lock = threading.RLock()
_atexit_registered = False


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

        logger.debug("OpenTelemetry resolved config: %s", settings.redacted())

        resource = otel.build_resource(
            settings.service_name,
            settings.resource_attributes,
            service_version=netbox_version,
            plugin_version=__version__,
            role=role,
        )
        ctx = Context(settings=settings, role=role, resource=resource)
        state = _State(pid=os.getpid(), context=ctx)

        if settings.logs.enabled:
            _setup_logger_provider(ctx, state)

        for module in _candidate_modules(ctx):
            if not module.enabled(settings):
                continue
            try:
                module.install(ctx)
            except Exception as exc:
                logger.warning("OpenTelemetry: %s module disabled: %s: %s", module.name, type(exc).__name__, exc)
                continue
            state.modules.append(module)

        _state = state
        if not _atexit_registered:
            atexit.register(shutdown)
            _atexit_registered = True
        return ctx


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


def _setup_logger_provider(ctx: Context, state: _State) -> None:
    existing = otel.existing_logger_provider()
    if existing is not None:
        logger.info("OpenTelemetry: reusing the LoggerProvider configured outside the plugin")
        ctx.logger_provider = existing
        return
    try:
        exporter = otel.build_log_exporter(ctx.settings.logs.exporter)
        ctx.logger_provider = otel.build_logger_provider(ctx.resource, exporter)
        state.owns_logger_provider = True
    except Exception as exc:
        logger.warning("OpenTelemetry: logs disabled: could not build exporter: %s: %s", type(exc).__name__, exc)


def _candidate_modules(ctx: Context) -> list[Module]:
    modules: list[Module] = []
    if ctx.logger_provider is not None:
        modules.append(LogsModule())
    return modules
