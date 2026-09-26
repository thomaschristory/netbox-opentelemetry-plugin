"""Tracing instrumentation: Django, psycopg, redis and requests, with the plugin's TracerProvider.

The provider handed to the instrumentors is the plugin's SwitchableTracerProvider (or a provider
configured outside the plugin); the instrumentors keep it for the process lifetime, and bootstrap
replaces the SDK provider behind it after fork.
"""

from __future__ import annotations

import logging

from .. import otel
from ..conf import Settings
from .base import Context

logger = logging.getLogger(otel.PLUGIN_LOGGER)


def instrument_kwargs(name: str, ctx: Context) -> dict:
    cfg = ctx.settings.traces
    kwargs: dict = {"tracer_provider": ctx.tracer_provider}
    if name == "django":
        kwargs["excluded_urls"] = ",".join(cfg.excluded_urls)
        # No HTTP metrics until the metrics module passes its own provider.
        kwargs["meter_provider"] = otel.noop_meter_provider()
    elif name == "psycopg":
        # Explicit, so a future default change cannot start exporting bind parameters.
        kwargs["enable_commenter"] = False
        kwargs["capture_parameters"] = False
    elif name == "requests":
        kwargs["meter_provider"] = otel.noop_meter_provider()
    return kwargs


class TracesModule:
    name = "traces"

    def __init__(self) -> None:
        self._instrumented: list = []

    def enabled(self, settings: Settings) -> bool:
        return settings.traces.enabled

    def install(self, ctx: Context) -> None:
        if self._instrumented or ctx.tracer_provider is None:
            return
        otel.prefer_stable_http_semconv()
        for name in ctx.settings.traces.instrument:
            try:
                instrumentor = otel.load_instrumentor(name)
            except Exception as exc:
                logger.warning("OpenTelemetry: %s instrumentation unavailable: %s", name, type(exc).__name__)
                continue
            if instrumentor.is_instrumented_by_opentelemetry:
                logger.info("OpenTelemetry: %s is already instrumented outside the plugin; left as is", name)
                continue
            try:
                instrumentor.instrument(**instrument_kwargs(name, ctx))
            except Exception as exc:
                logger.warning("OpenTelemetry: %s instrumentation failed: %s", name, type(exc).__name__)
                continue
            if not instrumentor.is_instrumented_by_opentelemetry:
                # BaseInstrumentor.instrument() returns without instrumenting on a dependency conflict.
                logger.warning("OpenTelemetry: %s instrumentation was not applied (dependency check failed)", name)
                continue
            self._instrumented.append(instrumentor)

    def after_fork(self, ctx: Context) -> None:
        pass

    def shutdown(self) -> None:
        for instrumentor in reversed(self._instrumented):
            try:
                instrumentor.uninstrument()
            except Exception as exc:
                logger.warning("OpenTelemetry: uninstrument failed: %s", type(exc).__name__)
        self._instrumented.clear()
