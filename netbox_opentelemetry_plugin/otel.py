"""Single import point for the OpenTelemetry SDK.

The logs SDK lives in underscore modules (opentelemetry.sdk._logs) and its API changes between
releases. Keeping every SDK import here means such changes are fixed in one place. This file also
holds every tracing import (SDK trace, propagators, instrumentors).

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
import os
import re
import socket
import threading
import time
import traceback
from collections.abc import Mapping

import opentelemetry.context
from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber, get_logger_provider
from opentelemetry.context import Context
from opentelemetry.metrics import MeterProvider, NoOpMeterProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider, sampling
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .conf import ExporterConfig

PLUGIN_LOGGER = "netbox_opentelemetry_plugin"

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


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """Return span with headers, query strings and URL queries in free text removed (see SPEC 7).

    Returns the same object when nothing needs to change, so clean spans cost one attribute scan.
    """
    attributes = _redact_attributes(span.attributes or {})
    events = list(span.events)
    events_changed = False
    for index, event in enumerate(events):
        scrubbed = _scrub_event(event)
        if scrubbed is not None:
            events[index] = scrubbed
            events_changed = True
    status = span.status
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

    def on_start(self, span, parent_context=None) -> None:
        self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            span = redact_span(span)
        except Exception:
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


def noop_tracer_provider() -> trace.TracerProvider:
    return trace.NoOpTracerProvider()


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
