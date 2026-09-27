"""Single import point for the OpenTelemetry SDK.

The logs SDK lives in underscore modules (opentelemetry.sdk._logs) and its API changes between
releases. Keeping every SDK import here means such changes are fixed in one place. This file also
holds every tracing and metrics import (SDK trace and metrics, propagators, instrumentors).

AllowlistLoggingHandler below is implemented directly on the stable opentelemetry._logs API
rather than subclassing opentelemetry-instrumentation-logging's handler: that package registers
an opentelemetry_instrumentor entry point which, under `opentelemetry-instrument`, installs an
unfiltered root handler and would duplicate exports and bypass the attribute allowlist.
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib
import logging
import math
import os
import re
import socket
import threading
import time
import traceback
from collections.abc import Mapping

import opentelemetry.context
from opentelemetry import metrics as metrics_api
from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber, get_logger_provider
from opentelemetry.context import Context
from opentelemetry.metrics import MeterProvider, NoOpMeterProvider, Observation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider, sampling
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .conf import PLUGIN_LOGGER, ExporterConfig

logger = logging.getLogger(PLUGIN_LOGGER)

# Records from these loggers are never exported: the plugin's own warnings, the OTel SDK, and the
# HTTP/gRPC client libraries used by the exporters. Exporting them could loop back into the exporter.
EXCLUDED_LOGGER_PREFIXES = (PLUGIN_LOGGER, "opentelemetry", "urllib3", "grpc")

LOG_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "code.file.path",
        "code.function.name",
        "code.line.number",
        "exception.type",
        "exception.message",
        "exception.stacktrace",
        "logger.name",
        "thread.name",
    }
)


def build_resource(
    service_name: str,
    resource_attributes: Mapping[str, str | bool | int | float],
    *,
    service_version: str,
    plugin_version: str,
    role: str,
) -> Resource:
    attributes = dict(resource_attributes)
    attributes.update(
        {
            "service.name": service_name,
            "service.version": service_version,
            "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
            "netbox.plugin.version": plugin_version,
            "netbox.process.role": role,
        }
    )
    return Resource.create(attributes)


def _grpc_credentials(cfg: ExporterConfig):
    if not cfg.certificate:
        return None
    import grpc

    with open(cfg.certificate, "rb") as fh:
        return grpc.ssl_channel_credentials(fh.read())


def build_log_exporter(cfg: ExporterConfig) -> LogRecordExporter:
    if cfg.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as GrpcLogExporter,
        )

        return GrpcLogExporter(
            endpoint=cfg.endpoint,
            insecure=cfg.insecure,
            credentials=_grpc_credentials(cfg),
            headers=dict(cfg.headers),
            timeout=cfg.timeout,
        )

    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as HttpLogExporter,
    )

    return HttpLogExporter(
        endpoint=cfg.endpoint,
        headers=dict(cfg.headers),
        timeout=cfg.timeout,
        certificate_file=cfg.certificate,
    )


def build_logger_provider(
    resource: Resource,
    exporter: LogRecordExporter,
    *,
    synchronous: bool = False,
    max_queue_size: int | None = None,
) -> LoggerProvider:
    # shutdown_on_exit=False: bootstrap owns shutdown ordering (remove handlers first, then flush).
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    if synchronous:
        processor = SimpleLogRecordProcessor(exporter)
    else:
        kwargs = {} if max_queue_size is None else {"max_queue_size": max_queue_size}
        processor = BatchLogRecordProcessor(exporter, **kwargs)
    provider.add_log_record_processor(processor)
    return provider


def existing_logger_provider() -> LoggerProvider | None:
    """Return the global SDK LoggerProvider if something (for example opentelemetry-instrument) set one."""
    provider = get_logger_provider()
    return provider if isinstance(provider, LoggerProvider) else None


class ExcludeLoggersFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        return not any(name == prefix or name.startswith(prefix + ".") for prefix in EXCLUDED_LOGGER_PREFIXES)


# Severity text as defined by the OTel logs data model: WARNING/CRITICAL are spelled differently
# than the Python stdlib levelname.
_SEVERITY_TEXT = {"WARNING": "WARN", "CRITICAL": "FATAL"}


def _severity_number(levelno: int) -> SeverityNumber:
    if levelno < 10:
        return SeverityNumber.TRACE
    if levelno < 20:
        return SeverityNumber.DEBUG
    if levelno < 30:
        return SeverityNumber.INFO
    if levelno < 40:
        return SeverityNumber.WARN
    if levelno < 50:
        return SeverityNumber.ERROR
    return SeverityNumber.FATAL


class AllowlistLoggingHandler(logging.Handler):
    """Exports stdlib LogRecords over the OTel logs API, restricted to an attribute allowlist.

    Implemented locally (not a subclass of opentelemetry-instrumentation-logging's handler) so
    that only LOG_ATTRIBUTE_ALLOWLIST attributes are ever built, and so that emit() can never
    raise into NetBox: any failure is handed to logging.Handler.handleError, which is the
    standard library's own "print to stderr and keep going" behaviour.
    """

    # Class-level so recursion is guarded across every instance and every thread: if building or
    # exporting a record causes another record to be logged (for example a warning from inside
    # the OTel SDK), that reentrant call returns immediately instead of looping or deadlocking.
    _emitting: contextvars.ContextVar[bool] = contextvars.ContextVar("_otel_emitting", default=False)

    def __init__(self, level: int, logger_provider: LoggerProvider) -> None:
        super().__init__(level=level)
        self._logger_provider = logger_provider

    def set_logger_provider(self, provider: LoggerProvider) -> None:
        """Point the handler at another provider (used in a forked child)."""
        self._logger_provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        if self._emitting.get():
            return
        token = self._emitting.set(True)
        try:
            self._logger_provider.get_logger(record.name).emit(self._translate(record))
        except Exception:
            self.handleError(record)
        finally:
            self._emitting.reset(token)

    def flush(self) -> None:
        force_flush = getattr(self._logger_provider, "force_flush", None)
        if force_flush is None:
            return
        # Same approach as the SDK's own handler (opentelemetry-python PR 4636): logging.shutdown()
        # calls flush() while holding this handler's lock. A synchronous force_flush would wait on
        # the batch worker, and a worker that logs through this handler would wait on the lock.
        threading.Thread(target=_call_quietly, args=(force_flush,), name="otel-log-flush", daemon=True).start()

    def _translate(self, record: logging.LogRecord) -> LogRecord:
        body = self.format(record) if self.formatter else record.getMessage()
        return LogRecord(
            timestamp=int(record.created * 1e9),
            observed_timestamp=time.time_ns(),
            context=opentelemetry.context.get_current(),
            severity_number=_severity_number(record.levelno),
            severity_text=_SEVERITY_TEXT.get(record.levelname, record.levelname),
            body=body,
            attributes=self._attributes(record),
        )

    @staticmethod
    def _attributes(record: logging.LogRecord) -> dict[str, object]:
        # Built only from the allowlist. Nothing from extra= is ever read, so arbitrary data
        # (including keys that shadow these names, such as extra={"exception.type": "Fake"})
        # cannot spoof what is exported.
        attributes: dict[str, object] = {
            "code.file.path": record.pathname,
            "code.function.name": record.funcName,
            "code.line.number": record.lineno,
            "logger.name": record.name,
        }
        if record.threadName:
            attributes["thread.name"] = record.threadName
        exc_info = record.exc_info
        if exc_info and exc_info[0] is not None:
            exc_type, exc_value, _ = exc_info
            attributes["exception.type"] = exc_type.__name__
            if exc_value is not None and exc_value.args:
                attributes["exception.message"] = str(exc_value.args[0])
            attributes["exception.stacktrace"] = "".join(traceback.format_exception(*exc_info))
        return attributes


def _call_quietly(func) -> None:
    with contextlib.suppress(Exception):
        func()


def build_logging_handler(provider: LoggerProvider, level: int) -> AllowlistLoggingHandler:
    handler = AllowlistLoggingHandler(level, provider)
    handler.addFilter(ExcludeLoggersFilter())
    return handler


# --- Traces ---------------------------------------------------------------------------------

CONSUMER = SpanKind.CONSUMER
REDACTED_QUERY = "REDACTED"

# Header attributes appear when an operator sets OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*; the
# instrumentors read those variables themselves and set the attributes after span start.
_HEADER_ATTRIBUTE_PREFIXES = ("http.request.header.", "http.response.header.")
_QUERY_ONLY_ATTRIBUTES = frozenset({"url.query"})
_URL_ATTRIBUTES = frozenset({"url.full", "http.url", "http.target"})
# A "?" followed by a run of non-space characters, as in "url: /hook?token=abc" inside an
# exception message. No lookbehind and no excluded punctuation: over-redacting free text (for
# example swallowing a trailing ")" or a quote) is the safe failure, unlike leaving a secret in.
# "why? because" still does not match since a space follows the "?". Linear: no nested quantifiers.
_QUERY_IN_TEXT = re.compile(r"\?\S+")

# A span's status description and its "exception" event can carry the offending values straight
# from the underlying error (for example a PostgreSQL "DETAIL: Key (name)=(...) already exists.",
# raised on a psycopg query span, or surfacing unhandled on the request's SERVER span or a job's
# CONSUMER span once Django or rq lets the exception propagate). Only the exception type is safe to
# keep, for every span; see SPEC 7.
_EXCEPTION_DROP_ATTRIBUTES = frozenset({"exception.message", "exception.stacktrace"})


def build_span_exporter(cfg: ExporterConfig) -> SpanExporter:
    if cfg.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcSpanExporter,
        )

        return GrpcSpanExporter(
            endpoint=cfg.endpoint,
            insecure=cfg.insecure,
            credentials=_grpc_credentials(cfg),
            headers=dict(cfg.headers),
            timeout=cfg.timeout,
        )

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpSpanExporter,
    )

    return HttpSpanExporter(
        endpoint=cfg.endpoint,
        headers=dict(cfg.headers),
        timeout=cfg.timeout,
        certificate_file=cfg.certificate,
    )


class RootClientSpanFilter(sampling.Sampler):
    """Drops CLIENT spans that have no parent, then defers to the configured sampler.

    Outside a request or job, the psycopg and redis instrumentations would otherwise create one
    trace per query or command: startup queries, and in the RQ worker every poll and heartbeat.
    """

    def __init__(self, delegate: sampling.Sampler) -> None:
        self._delegate = delegate

    def should_sample(
        self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None
    ) -> sampling.SamplingResult:
        # parent_context None means the current context, as in the SDK's own start_span.
        if kind == SpanKind.CLIENT and not trace.get_current_span(parent_context).get_span_context().is_valid:
            return sampling.SamplingResult(sampling.Decision.DROP)
        return self._delegate.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return f"RootClientSpanFilter{{{self._delegate.get_description()}}}"


def build_sampler(name: str, arg: float) -> sampling.Sampler:
    ratio = sampling.TraceIdRatioBased(arg)
    base = {
        "always_on": sampling.ALWAYS_ON,
        "always_off": sampling.ALWAYS_OFF,
        "traceidratio": ratio,
        "parentbased_always_on": sampling.ParentBased(sampling.ALWAYS_ON),
        "parentbased_always_off": sampling.ParentBased(sampling.ALWAYS_OFF),
        "parentbased_traceidratio": sampling.ParentBased(ratio),
    }[name]
    return RootClientSpanFilter(base)


def redact_url_query(url: str) -> str:
    """Replace everything after the first "?" (query and fragment) with REDACTED."""
    base, sep, rest = url.partition("?")
    if not sep or not rest:
        return url
    return f"{base}?{REDACTED_QUERY}"


def scrub_query_strings(text: str) -> str:
    return _QUERY_IN_TEXT.sub(f"?{REDACTED_QUERY}", text)


def _redact_attributes(attributes: Mapping) -> dict | None:
    """Redacted copy of span attributes, or None when nothing needs to change."""
    changed = False
    result = {}
    for key, value in attributes.items():
        if key.startswith(_HEADER_ATTRIBUTE_PREFIXES):
            changed = True
            continue
        if key in _QUERY_ONLY_ATTRIBUTES and value:
            value = REDACTED_QUERY
            changed = True
        elif key in _URL_ATTRIBUTES and isinstance(value, str):
            redacted = redact_url_query(value)
            changed = changed or redacted != value
            value = redacted
        result[key] = value
    return result if changed else None


def _scrub_event(event: Event) -> Event | None:
    attributes = event.attributes or {}
    scrubbed = {k: scrub_query_strings(v) if isinstance(v, str) else v for k, v in attributes.items()}
    if scrubbed == dict(attributes):
        return None
    return Event(event.name, scrubbed, event.timestamp)


def _drop_exception_details(event: Event) -> Event | None:
    """For an "exception" event, drop exception.message/exception.stacktrace, keep the rest."""
    if event.name != "exception":
        return None
    attributes = event.attributes or {}
    if not any(key in attributes for key in _EXCEPTION_DROP_ATTRIBUTES):
        return None
    kept = {k: v for k, v in attributes.items() if k not in _EXCEPTION_DROP_ATTRIBUTES}
    return Event(event.name, kept, event.timestamp)


def _reduce_status_to_exception_type(status: Status) -> Status | None:
    """Replace the status description with only the text before its first ":".

    A description with no colon (a bare exception type name, or a plugin-set description such as
    "job failed") is left unchanged: there is nothing after a colon to drop, and blanking it would
    destroy information the plugin itself chose to record.
    """
    if not status.description:
        return None
    exception_type, sep, _ = status.description.partition(":")
    if not sep or exception_type == status.description:
        return None
    return Status(status.status_code, exception_type)


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """Return span with headers, query strings and URL queries in free text removed (see SPEC 7).

    Every span additionally has its status description reduced to the bare exception type, and
    exception.message/exception.stacktrace dropped from any "exception" event: an unhandled error
    (a PostgreSQL error surfacing on a psycopg query span, or on the request's SERVER span or a
    job's CONSUMER span once it propagates unhandled) can carry the offending values.

    Returns the same object when nothing needs to change, so clean spans cost one attribute scan.
    """
    attributes = _redact_attributes(span.attributes or {})
    events = list(span.events)
    events_changed = False
    for index, event in enumerate(events):
        working = event
        changed = False
        dropped = _drop_exception_details(working)
        if dropped is not None:
            working = dropped
            changed = True
        scrubbed = _scrub_event(working)
        if scrubbed is not None:
            working = scrubbed
            changed = True
        if changed:
            events[index] = working
            events_changed = True
    status = span.status
    reduced = _reduce_status_to_exception_type(status)
    if reduced is not None:
        status = reduced
    if status.description:
        description = scrub_query_strings(status.description)
        if description != status.description:
            status = Status(status.status_code, description)
    if attributes is None and not events_changed and status is span.status:
        return span
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes if attributes is not None else dict(span.attributes or {}),
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanProcessor(SpanProcessor):
    """Redacts every span before handing it to the exporting processor.

    Redaction runs at span end because the instrumentors add header attributes and error details
    after span start. A span that cannot be redacted is dropped rather than exported as is.
    """

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate
        self._warned = False

    def on_start(self, span, parent_context=None) -> None:
        self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            span = redact_span(span)
        except Exception as exc:
            if not self._warned:
                self._warned = True
                # Exception type only: redact_span operates on span data that may itself be sensitive.
                logger.warning("OpenTelemetry: could not redact a span; it was dropped: %s", type(exc).__name__)
            return
        self._delegate.on_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def build_tracer_provider(
    resource: Resource, exporter: SpanExporter, sampler: sampling.Sampler, *, synchronous: bool = False
) -> TracerProvider:
    # shutdown_on_exit=False: bootstrap owns shutdown ordering, as for the LoggerProvider.
    provider = TracerProvider(sampler=sampler, resource=resource, shutdown_on_exit=False)
    inner = SimpleSpanProcessor(exporter) if synchronous else BatchSpanProcessor(exporter)
    provider.add_span_processor(RedactingSpanProcessor(inner))
    return provider


class _SwitchableTracer(trace.Tracer):
    def __init__(self, owner: SwitchableTracerProvider, args: tuple) -> None:
        self._owner = owner
        self._args = args
        self._cached: tuple[object, trace.Tracer | None] = (None, None)

    def _tracer(self) -> trace.Tracer:
        delegate = self._owner.delegate
        cached_for, tracer = self._cached
        if cached_for is not delegate or tracer is None:
            tracer = delegate.get_tracer(*self._args)
            # One tuple assignment, so a concurrent reader sees either the old or the new pair.
            self._cached = (delegate, tracer)
        return tracer

    def start_span(self, *args, **kwargs):
        return self._tracer().start_span(*args, **kwargs)

    def start_as_current_span(self, *args, **kwargs):
        return self._tracer().start_as_current_span(*args, **kwargs)


class SwitchableTracerProvider(trace.TracerProvider):
    """The TracerProvider handed to instrumentors, which cache their tracers for the process lifetime.

    Every tracer it returns resolves the current delegate on each span, so bootstrap can replace
    the SDK provider in a forked child (or with a no-op provider) without re-instrumenting.
    """

    def __init__(self, delegate: trace.TracerProvider) -> None:
        self._delegate = delegate

    @property
    def delegate(self) -> trace.TracerProvider:
        return self._delegate

    def set_delegate(self, provider: trace.TracerProvider) -> None:
        self._delegate = provider

    def get_tracer(
        self, instrumenting_module_name, instrumenting_library_version=None, schema_url=None, attributes=None
    ) -> trace.Tracer:
        return _SwitchableTracer(
            self, (instrumenting_module_name, instrumenting_library_version, schema_url, attributes)
        )

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self._delegate, "force_flush", None)
        return True if flush is None else flush(timeout_millis)

    def shutdown(self) -> None:
        shutdown = getattr(self._delegate, "shutdown", None)
        if shutdown is not None:
            shutdown()
        # Later spans (for example from a request that races the shutdown) get a detached tracer:
        # no span, and an inbound trace context is neither continued nor forwarded.
        self._delegate = detached_tracer_provider()


class _DetachedTracer(trace.NoOpTracer):
    """A tracer whose spans are always INVALID_SPAN, whatever the parent.

    The API NoOpTracer returns a non-recording span carrying the parent's span context, so an
    instrumentor using it would still make an inbound traceparent current (log records would carry
    its ids) and forward it on outbound calls. With INVALID_SPAN current, neither happens.
    start_as_current_span is inherited and activates the span returned here.
    """

    def start_span(self, *args, **kwargs) -> trace.Span:
        return trace.INVALID_SPAN


class _DetachedTracerProvider(trace.NoOpTracerProvider):
    def get_tracer(self, *args, **kwargs) -> trace.Tracer:
        return _DetachedTracer()


def detached_tracer_provider() -> trace.TracerProvider:
    """For instrumentors applied for metrics only: no spans, no trace context continued or forwarded."""
    return _DetachedTracerProvider()


def noop_meter_provider() -> MeterProvider:
    return NoOpMeterProvider()


def existing_tracer_provider() -> TracerProvider | None:
    """Return the global SDK TracerProvider if something (for example opentelemetry-instrument) set one."""
    provider = trace.get_tracer_provider()
    return provider if isinstance(provider, TracerProvider) else None


# W3C trace context only: baggage can carry arbitrary request data and is not copied into jobs.
_TRACECONTEXT = TraceContextTextMapPropagator()


def inject_trace_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    _TRACECONTEXT.inject(carrier)
    return carrier


def extract_trace_context(carrier: object) -> Context | None:
    if not isinstance(carrier, Mapping):
        return None
    clean = {k: v for k, v in carrier.items() if isinstance(k, str) and isinstance(v, str)}
    if not clean:
        return None
    return _TRACECONTEXT.extract(clean)


def start_span(provider, scope: str, version: str, name: str, *, kind, parent, attributes: Mapping):
    tracer = provider.get_tracer(scope, version)
    return tracer.start_span(name, context=parent, kind=kind, attributes=dict(attributes))


def activate(span) -> object:
    return opentelemetry.context.attach(trace.set_span_in_context(span))


def deactivate(token) -> None:
    opentelemetry.context.detach(token)


def record_failure(span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def set_error(span, description: str) -> None:
    span.set_status(Status(StatusCode.ERROR, description))


def current_span_is_recording() -> bool:
    return trace.get_current_span().is_recording()


def annotate_current_span(attributes: Mapping[str, str]) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attributes(dict(attributes))


def load_instrumentor(name: str):
    """Import and return the instrumentor singleton for one of conf.INSTRUMENTATIONS. KeyError if unknown."""
    loaders = {
        "django": ("opentelemetry.instrumentation.django", "DjangoInstrumentor"),
        "psycopg": ("opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
        "redis": ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
        "requests": ("opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
    }
    module_name, class_name = loaders[name]
    return getattr(importlib.import_module(module_name), class_name)()


def prefer_stable_http_semconv() -> None:
    """Select the stable HTTP semantic conventions (url.full, http.request.method, ...).

    The instrumentations read this once, on first use. An operator's own value is kept.
    """
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")


def emit_event(
    provider: LoggerProvider,
    scope: str,
    *,
    event_name: str,
    body: str,
    attributes: Mapping[str, object],
    timestamp_ns: int | None = None,
) -> None:
    now = time.time_ns()
    provider.get_logger(scope).emit(
        LogRecord(
            timestamp=timestamp_ns if timestamp_ns is not None else now,
            observed_timestamp=now,
            context=opentelemetry.context.get_current(),
            severity_number=SeverityNumber.INFO,
            severity_text="INFO",
            body=body,
            attributes=dict(attributes),
            event_name=event_name,
        )
    )


# --- Metrics --------------------------------------------------------------------------------

_JOB_ATTRIBUTES = frozenset({"messaging.destination.name", "code.function.name", "netbox.rq.job.outcome"})

# SPEC 7: nothing leaves the process unless it is listed. The plugin-owned MeterProvider exports
# only these instruments, each with only these attribute keys. None (runtime wildcards only) keeps
# the instrumentor's own attributes, which are low-cardinality process states.
METRIC_ALLOWLIST: Mapping[str, frozenset[str] | None] = {
    "http.server.request.duration": frozenset(
        {"http.request.method", "http.route", "http.response.status_code", "error.type"}
    ),
    "http.client.request.duration": frozenset(
        {"http.request.method", "server.address", "http.response.status_code", "error.type"}
    ),
    "netbox.rq.job.duration": _JOB_ATTRIBUTES,
    "netbox.rq.jobs": _JOB_ATTRIBUTES,
    "netbox.rq.queue.depth": frozenset({"messaging.destination.name"}),
    "netbox.object_changes": frozenset({"netbox.change.action", "netbox.change.object_type"}),
    "process.*": None,
    "cpython.gc.*": None,
}


def metric_views() -> list[View]:
    """Drop every instrument, then add one View per allowlisted name.

    An instrument matching the catch-all and a named View gets one dropped stream and one exported
    stream; an instrument matching only the catch-all is not exported at all.
    """
    views = [View(instrument_name="*", aggregation=DropAggregation())]
    for name, keys in METRIC_ALLOWLIST.items():
        if keys is None:
            views.append(View(instrument_name=name))
        else:
            views.append(View(instrument_name=name, attribute_keys=set(keys)))
    return views


def build_metric_exporter(cfg: ExporterConfig) -> MetricExporter:
    if cfg.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as GrpcMetricExporter,
        )

        return GrpcMetricExporter(
            endpoint=cfg.endpoint,
            insecure=cfg.insecure,
            credentials=_grpc_credentials(cfg),
            headers=dict(cfg.headers),
            timeout=cfg.timeout,
        )

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter as HttpMetricExporter,
    )

    return HttpMetricExporter(
        endpoint=cfg.endpoint,
        headers=dict(cfg.headers),
        timeout=cfg.timeout,
        certificate_file=cfg.certificate,
    )


def build_meter_provider(resource: Resource, readers) -> SdkMeterProvider:
    # shutdown_on_exit=False: bootstrap owns shutdown ordering, as for the other providers.
    return SdkMeterProvider(
        metric_readers=list(readers), resource=resource, shutdown_on_exit=False, views=metric_views()
    )


# Seconds left for MeterProvider.shutdown when MetricsPipeline.shutdown's deadline is spent.
_SHUTDOWN_FLOOR = 0.05


class MetricsPipeline:
    """A plugin-owned MeterProvider and the thread that exports it every `interval` seconds.

    The SDK's PeriodicExportingMetricReader starts its own ticker thread and restarts it in every
    forked child through an at-fork hook, so each RQ work-horse would export the parent's
    cumulative state through the parent's exporter connection. The reader here is created with an
    infinite interval (no thread, no at-fork hook) and this class drives collection instead. Its
    thread does not survive fork: bootstrap builds a new pipeline in a child only where metrics
    belong (never in a horse).
    """

    def __init__(self, resource: Resource, exporter: MetricExporter, *, interval: float, timeout: float) -> None:
        self._timeout_millis = timeout * 1000
        self._reader: MetricReader = PeriodicExportingMetricReader(
            exporter, export_interval_millis=math.inf, export_timeout_millis=self._timeout_millis
        )
        self.provider = build_meter_provider(resource, [self._reader])
        self._interval = interval
        self._stop = threading.Event()
        self._warned = False
        self._thread = threading.Thread(target=self._run, name="otel-metrics", daemon=True)
        try:
            self._thread.start()
        except Exception:
            with contextlib.suppress(Exception):
                self.provider.shutdown()
            raise

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._collect()

    def _collect(self) -> None:
        try:
            # Export errors are caught and logged by the reader itself; this catches collection
            # errors (for example a timeout) so the thread keeps its interval.
            self._reader.collect(timeout_millis=self._timeout_millis)
        except Exception as exc:
            if not self._warned:
                self._warned = True
                logger.warning("OpenTelemetry: metric collection failed: %s", type(exc).__name__)

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        return self.provider.force_flush(timeout_millis)

    def shutdown(self, timeout: float) -> None:
        """Stop the thread, export once more (the reader has no thread of its own to do it), shut down.

        The reader serializes every export through one lock (SPEC: "The configured exporter's
        export method will not be called concurrently"), held for as long as the exporter's own
        export() call takes. If some export is stuck there (a hung exporter or an unresponsive
        Collector), a synchronous force_flush would block on that same lock for as long as the
        hang lasts, regardless of the timeout passed to it. The final flush therefore runs on its
        own thread; a still-blocked flush is left running (it is a daemon thread) and the method
        moves on to provider.shutdown() regardless. Joining the export thread and waiting for the
        flush share one deadline of `timeout` seconds. provider.shutdown() does not need the export
        lock: this reader keeps no daemon thread of its own (export_interval_millis=inf), so
        PeriodicExportingMetricReader.shutdown calls the exporter's shutdown() directly. The whole
        call is therefore bounded by about `timeout` plus the exporter's own shutdown() (the OTLP
        exporters only close their session or channel there).
        """
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            return max(deadline - time.monotonic(), 0.0)

        self._stop.set()
        self._thread.join(remaining())
        flushed = threading.Event()
        flush_millis = remaining() * 1000

        def _flush() -> None:
            with contextlib.suppress(Exception):
                self.provider.force_flush(flush_millis)
            flushed.set()

        threading.Thread(target=_flush, name="otel-metrics-flush", daemon=True).start()
        flushed.wait(remaining())
        # A small floor: with an exhausted deadline, MeterProvider.shutdown would skip the reader
        # (and so the exporter's shutdown) altogether.
        self.provider.shutdown(max(remaining(), _SHUTDOWN_FLOOR) * 1000)


class _SwitchableInstrument:
    """A synchronous instrument that resolves the provider's current delegate on each measurement."""

    def __init__(self, meter: _SwitchableMeter, factory: str, args: tuple, kwargs: dict) -> None:
        self._meter, self._factory, self._args, self._kwargs = meter, factory, args, kwargs
        self._cached: tuple[object, object] = (None, None)

    def _instrument(self):
        meter = self._meter.delegate_meter()
        cached_for, instrument = self._cached
        if cached_for is not meter or instrument is None:
            instrument = getattr(meter, self._factory)(*self._args, **self._kwargs)
            # One tuple assignment, so a concurrent reader sees either the old or the new pair.
            self._cached = (meter, instrument)
        return instrument

    def add(self, *args, **kwargs) -> None:
        self._instrument().add(*args, **kwargs)

    def record(self, *args, **kwargs) -> None:
        self._instrument().record(*args, **kwargs)

    def set(self, *args, **kwargs) -> None:
        self._instrument().set(*args, **kwargs)


class _SwitchableMeter(metrics_api.Meter):
    def __init__(self, owner: SwitchableMeterProvider, args: tuple) -> None:
        super().__init__(args[0], args[1], args[2])
        self._owner = owner
        self._args = args
        self._cached: tuple[object, metrics_api.Meter | None] = (None, None)

    def delegate_meter(self) -> metrics_api.Meter:
        delegate = self._owner.delegate
        cached_for, meter = self._cached
        if cached_for is not delegate or meter is None:
            meter = delegate.get_meter(*self._args)
            self._cached = (delegate, meter)
        return meter

    def create_counter(self, *args, **kwargs):
        return _SwitchableInstrument(self, "create_counter", args, kwargs)

    def create_up_down_counter(self, *args, **kwargs):
        return _SwitchableInstrument(self, "create_up_down_counter", args, kwargs)

    def create_histogram(self, *args, **kwargs):
        return _SwitchableInstrument(self, "create_histogram", args, kwargs)

    def create_gauge(self, *args, **kwargs):
        return _SwitchableInstrument(self, "create_gauge", args, kwargs)

    def create_observable_counter(self, *args, **kwargs):
        return self._owner._observe(self, "create_observable_counter", args, kwargs)

    def create_observable_up_down_counter(self, *args, **kwargs):
        return self._owner._observe(self, "create_observable_up_down_counter", args, kwargs)

    def create_observable_gauge(self, *args, **kwargs):
        return self._owner._observe(self, "create_observable_gauge", args, kwargs)


class SwitchableMeterProvider(metrics_api.MeterProvider):
    """The MeterProvider handed to every instrument; instruments are cached by their callers for the
    process lifetime (the Django middleware keeps them as class attributes).

    Synchronous instruments resolve the delegate on each measurement. Observable instruments are
    pulled by the delegate's readers, so they are registered on the delegate at creation and again
    on every new delegate. Bootstrap swaps the delegate after fork: a new SDK provider in a web or
    worker child, a no-op provider in an RQ work-horse.
    """

    def __init__(self, delegate: metrics_api.MeterProvider) -> None:
        self._delegate = delegate
        self._observables: list[tuple[_SwitchableMeter, str, tuple, dict]] = []

    @property
    def delegate(self) -> metrics_api.MeterProvider:
        return self._delegate

    def get_meter(self, name, version=None, schema_url=None, attributes=None) -> metrics_api.Meter:
        return _SwitchableMeter(self, (name, version, schema_url, attributes))

    def _observe(self, meter: _SwitchableMeter, factory: str, args: tuple, kwargs: dict):
        self._observables.append((meter, factory, args, kwargs))
        return getattr(meter.delegate_meter(), factory)(*args, **kwargs)

    def set_delegate(self, provider: metrics_api.MeterProvider) -> None:
        self._delegate = provider
        for meter, factory, args, kwargs in list(self._observables):
            try:
                getattr(meter.delegate_meter(), factory)(*args, **kwargs)
            except Exception as exc:
                logger.warning("OpenTelemetry: could not register an observable metric: %s", type(exc).__name__)

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        flush = getattr(self._delegate, "force_flush", None)
        return True if flush is None else flush(timeout_millis)


def existing_meter_provider() -> SdkMeterProvider | None:
    """Return the global SDK MeterProvider if something (for example opentelemetry-instrument) set one."""
    provider = metrics_api.get_meter_provider()
    return provider if isinstance(provider, SdkMeterProvider) else None


def observation(value: int | float, attributes: Mapping[str, str]) -> Observation:
    return Observation(value, dict(attributes))


def load_system_metrics_instrumentor(config: Mapping[str, list[str] | None]):
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

    return SystemMetricsInstrumentor(config={name: (list(v) if v is not None else None) for name, v in config.items()})


def repoint_system_metrics_process(instrumentor) -> bool:
    """Point the system-metrics instrumentor's process handle at the current process.

    The instrumentor stores psutil.Process(os.getpid()) when it is created; in a forked child that
    handle still describes the parent. `_proc` is private to opentelemetry-instrumentation-system-metrics
    0.65b0 (pinned; a test fails if it moves). Returns False when the attribute is not there.
    """
    if not hasattr(instrumentor, "_proc"):
        return False
    import psutil

    instrumentor._proc = psutil.Process(os.getpid())
    return True
