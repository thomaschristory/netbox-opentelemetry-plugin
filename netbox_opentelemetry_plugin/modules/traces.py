"""Instrumentation: Django, psycopg, redis and requests. The instrumentors produce spans (traces)
and the HTTP duration metrics (metrics); one set serves both.

The provider handed to the instrumentors is the plugin's SwitchableTracerProvider (or a provider
configured outside the plugin); the instrumentors keep it for the process lifetime, and bootstrap
replaces the SDK provider behind it after fork. The same holds for the SwitchableMeterProvider.

install also wraps the global propagator in otel.BaggageFreePropagator, so inbound baggage is
never made current and outbound calls carry the trace context only; the wrapper is kept after
shutdown.
"""

from __future__ import annotations

import logging

from .. import otel
from ..conf import Settings
from .base import Context

logger = logging.getLogger(otel.PLUGIN_LOGGER)

# The instrumentors that produce SPEC 6.4 HTTP metrics.
HTTP_METRIC_INSTRUMENTATIONS = ("django", "requests")


def instrumentations(ctx: Context) -> tuple[str, ...]:
    """traces.instrument when spans are recorded, plus the HTTP instrumentors when metrics are on."""
    names = list(ctx.settings.traces.instrument) if ctx.tracer_provider is not None else []
    if ctx.meter_provider is not None:
        names += [name for name in HTTP_METRIC_INSTRUMENTATIONS if name not in names]
    return tuple(names)


def instrument_kwargs(name: str, ctx: Context) -> dict:
    cfg = ctx.settings.traces
    traced = ctx.tracer_provider is not None and name in cfg.instrument
    # An instrumentor applied for metrics only gets a detached tracer: no spans from it, and an
    # inbound trace context is neither made current nor forwarded on outbound calls.
    kwargs: dict = {"tracer_provider": ctx.tracer_provider if traced else otel.detached_tracer_provider()}
    meter_provider = ctx.meter_provider if ctx.meter_provider is not None else otel.noop_meter_provider()
    if name == "django":
        # Excluded URLs get neither a span nor a duration measurement.
        kwargs["excluded_urls"] = ",".join(cfg.excluded_urls)
        kwargs["meter_provider"] = meter_provider
    elif name == "psycopg":
        # Explicit, so a future default change cannot start exporting bind parameters.
        kwargs["enable_commenter"] = False
        kwargs["capture_parameters"] = False
    elif name == "requests":
        kwargs["meter_provider"] = meter_provider
    return kwargs


class TracesModule:
    name = "instrumentation"

    def __init__(self) -> None:
        self._instrumented: list = []
        self._propagator_wrapped = False

    def enabled(self, settings: Settings) -> bool:
        return settings.traces.enabled or settings.metrics.enabled

    def install(self, ctx: Context) -> None:
        if self._instrumented or (ctx.tracer_provider is None and ctx.meter_provider is None):
            return
        # Before any instrumentor: if the configured propagator cannot be loaded, this raises and
        # bootstrap disables the whole module, so nothing is instrumented with baggage passthrough.
        otel.install_baggage_free_propagator()
        self._propagator_wrapped = True
        otel.prefer_stable_http_semconv()
        for name in instrumentations(ctx):
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
        if not self._propagator_wrapped:
            return
        # Re-wraps a propagator set with set_global_textmap between ready() and fork.
        try:
            otel.install_baggage_free_propagator()
        except Exception as exc:
            logger.warning(
                "OpenTelemetry: could not re-apply the baggage-free propagator after fork: %s", type(exc).__name__
            )

    def shutdown(self) -> None:
        # The propagator wrapper is not restored: a WSGI handler that is already built keeps
        # extracting, and restoring it would let baggage through for requests racing the shutdown.
        for instrumentor in reversed(self._instrumented):
            try:
                instrumentor.uninstrument()
            except Exception as exc:
                logger.warning("OpenTelemetry: uninstrument failed: %s", type(exc).__name__)
        self._instrumented.clear()
