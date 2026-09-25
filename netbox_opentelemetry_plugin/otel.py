"""Single import point for the OpenTelemetry SDK.

The logs SDK lives in underscore modules (opentelemetry.sdk._logs) and its API changes between
releases. Keeping every SDK import here means such changes are fixed in one place.

AllowlistLoggingHandler below is implemented directly on the stable opentelemetry._logs API
rather than subclassing opentelemetry-instrumentation-logging's handler: that package registers
an opentelemetry_instrumentor entry point which, under `opentelemetry-instrument`, installs an
unfiltered root handler and would duplicate exports and bypass the attribute allowlist.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import socket
import threading
import time
import traceback
from collections.abc import Mapping

import opentelemetry.context
from opentelemetry._logs import LogRecord, SeverityNumber, get_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource

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


def build_log_exporter(cfg: ExporterConfig) -> LogRecordExporter:
    if cfg.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as GrpcLogExporter,
        )

        credentials = None
        if cfg.certificate:
            import grpc

            with open(cfg.certificate, "rb") as fh:
                credentials = grpc.ssl_channel_credentials(fh.read())
        return GrpcLogExporter(
            endpoint=cfg.endpoint,
            insecure=cfg.insecure,
            credentials=credentials,
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
    resource: Resource, exporter: LogRecordExporter, *, synchronous: bool = False
) -> LoggerProvider:
    # shutdown_on_exit=False: bootstrap owns shutdown ordering (remove handlers first, then flush).
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    processor = SimpleLogRecordProcessor(exporter) if synchronous else BatchLogRecordProcessor(exporter)
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
