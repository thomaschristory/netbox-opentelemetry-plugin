import logging

from netbox.plugins import PluginConfig

from .version import __version__

logger = logging.getLogger("netbox_opentelemetry_plugin")


class NetBoxOpenTelemetryConfig(PluginConfig):
    name = "netbox_opentelemetry_plugin"
    verbose_name = "NetBox OpenTelemetry"
    description = "Export NetBox logs, audit records, traces and metrics over OTLP"
    version = __version__
    base_url = "opentelemetry"
    min_version = "4.7.0"
    max_version = "4.7.99"
    # Defaults live in conf.DEFAULTS. NetBox merges default_settings one level deep only, and
    # pre-filled defaults would hide which values were set explicitly (needed for OTEL_* fallback).
    default_settings = {}

    def ready(self):
        super().ready()


config = NetBoxOpenTelemetryConfig
