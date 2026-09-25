# netbox-opentelemetry-plugin

NetBox plugin that exports NetBox telemetry over OTLP to an OpenTelemetry Collector, from inside the NetBox processes. Each signal is a module that can be enabled or disabled in configuration.

Status: under development. Currently implemented: application logs, with support for forking web servers.

## Web servers

The plugin works with the web servers NetBox documents:

- Granian (netbox-docker): nothing to configure.
- gunicorn, with or without `preload_app`: nothing to configure.
- uWSGI: nothing to configure with pyuwsgi. With the classic uwsgi binary, set `enable-threads = true`, otherwise nothing is exported (the plugin logs a warning).

Each worker process exports with its own `service.instance.id`.

## Compatibility

| Plugin | NetBox |
|---|---|
| 0.1.x | 4.7.x |

## Installation

Install the package into the NetBox virtual environment and enable it in `configuration.py`:

```python
PLUGINS = ["netbox_opentelemetry_plugin"]
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://otel-collector:4318"},
    },
}
```

The endpoint can also be set with `OTEL_EXPORTER_OTLP_ENDPOINT`.
