"""Process runtime metrics (metrics.runtime) from opentelemetry-instrumentation-system-metrics.

Only process-level metrics are collected. Host-wide system.* metrics would be reported identically
by every NetBox process on a host (host metrics belong to the Collector's hostmetrics receiver),
and process.runtime.* are deprecated duplicates of process.*.
"""

from __future__ import annotations

import logging

from .. import otel
from ..conf import Settings
from .base import Context

logger = logging.getLogger(otel.PLUGIN_LOGGER)

RUNTIME_METRICS: dict[str, list[str] | None] = {
    "process.cpu.time": ["user", "system"],
    "process.cpu.utilization": ["user", "system"],
    "process.context_switches": ["involuntary", "voluntary"],
    "process.memory.usage": None,
    "process.memory.virtual": None,
    "process.open_file_descriptor.count": None,
    "process.thread.count": None,
    "cpython.gc.collections": None,
    "cpython.gc.collected_objects": None,
    "cpython.gc.uncollectable_objects": None,
}


class RuntimeModule:
    name = "runtime"

    def __init__(self) -> None:
        self._instrumentor = None

    def enabled(self, settings: Settings) -> bool:
        return settings.metrics.enabled and settings.metrics.runtime

    def install(self, ctx: Context) -> None:
        if self._instrumentor is not None or ctx.meter_provider is None:
            return
        instrumentor = otel.load_system_metrics_instrumentor(RUNTIME_METRICS)
        if instrumentor.is_instrumented_by_opentelemetry:
            logger.info("OpenTelemetry: system metrics are already instrumented outside the plugin; left as is")
            return
        instrumentor.instrument(meter_provider=ctx.meter_provider)
        if not instrumentor.is_instrumented_by_opentelemetry:
            logger.warning("OpenTelemetry: runtime metrics were not applied (dependency check failed)")
            return
        self._instrumentor = instrumentor

    def after_fork(self, ctx: Context) -> None:
        # The instrumentor holds a psutil handle on the process that created it; without this a
        # forked worker would report its parent's CPU and memory.
        if self._instrumentor is not None and not otel.repoint_system_metrics_process(self._instrumentor):
            logger.warning("OpenTelemetry: runtime metrics could not follow the fork and describe the parent process")

    def shutdown(self) -> None:
        if self._instrumentor is None:
            return
        try:
            # A no-op for the observable callbacks in 0.65b0; it clears the instrumented flag so a
            # later install can instrument again. Bootstrap switches the meter provider to a no-op.
            self._instrumentor.uninstrument()
        except Exception as exc:
            logger.warning("OpenTelemetry: uninstrument failed: %s", type(exc).__name__)
        self._instrumentor = None
