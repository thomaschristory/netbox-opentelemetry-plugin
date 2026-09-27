# Mounted over /etc/netbox/config/plugins.py in the netbox-docker image.
PLUGINS = ["netbox_opentelemetry_plugin"]

PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        # Endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT in dev/env/netbox.env.
        "logs": {
            "level": "INFO",
            # NetBox's default LOGGING is empty, so the netbox logger sits at WARNING without this.
            "set_logger_levels": True,
        },
        "traces": {"enabled": True},
        # Short interval so e2e does not wait a minute for the Collector to see metrics.
        "metrics": {"enabled": True, "export_interval": 5, "runtime": True},
    },
}
