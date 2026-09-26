import logging

from netbox.plugins import PluginConfig

from .version import __version__

logger = logging.getLogger("netbox_opentelemetry_plugin")


def _django_settings():
    from django.conf import settings

    return settings


class NetBoxOpenTelemetryConfig(PluginConfig):
    name = "netbox_opentelemetry_plugin"
    verbose_name = "NetBox OpenTelemetry"
    description = "Export NetBox logs, audit records, traces and metrics over OTLP"
    version = __version__
    base_url = "opentelemetry"
    min_version = "4.7.0"
    max_version = "4.7.99"
    # Always registered: NetBox reads it while loading settings, before ready(). Without a
    # recording span (traces off) it returns immediately.
    middleware = ["netbox_opentelemetry_plugin.middleware.RequestSpanMiddleware"]
    # Defaults live in conf.DEFAULTS. NetBox merges default_settings one level deep only, and
    # pre-filled defaults would hide which values were set explicitly (needed for OTEL_* fallback).
    default_settings = {}

    def ready(self):
        super().ready()
        try:
            from . import bootstrap

            settings = _django_settings()
            release = getattr(settings, "RELEASE", None)
            bootstrap.install(
                settings.PLUGINS_CONFIG.get(self.name, {}),
                netbox_version=getattr(release, "version", "unknown"),
            )
        except Exception:
            # The plugin must never prevent NetBox from starting.
            logger.warning("OpenTelemetry setup failed; export disabled", exc_info=True)


config = NetBoxOpenTelemetryConfig
