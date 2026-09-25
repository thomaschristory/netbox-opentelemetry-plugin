"""Single import point for the OpenTelemetry SDK.

The logs SDK lives in underscore modules (opentelemetry.sdk._logs) and its API changes between
releases. Keeping every SDK import here means such changes are fixed in one place.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Mapping

from opentelemetry._logs import get_logger_provider
from opentelemetry.instrumentation.logging.handler import LoggingHandler
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


class AllowlistLoggingHandler(LoggingHandler):
    """LoggingHandler that exports only allowlisted attributes.

    Extends the handler from opentelemetry-instrumentation-logging to filter attributes.
    The base handler exports every non-reserved LogRecord attribute, including anything passed via
    extra=. That would send arbitrary data off the process, so attributes are filtered here.
    """

    def _get_attributes(self, record: logging.LogRecord):
        attributes = super()._get_attributes(record)
        allowed = {key: value for key, value in attributes.items() if key in LOG_ATTRIBUTE_ALLOWLIST}
        allowed["logger.name"] = record.name
        if record.threadName:
            allowed["thread.name"] = record.threadName
        return allowed


def build_logging_handler(provider: LoggerProvider, level: int) -> AllowlistLoggingHandler:
    handler = AllowlistLoggingHandler(level=level, logger_provider=provider, log_code_attributes=True)
    handler.addFilter(ExcludeLoggersFilter())
    return handler
