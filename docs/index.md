# netbox-opentelemetry-plugin

netbox-opentelemetry-plugin exports NetBox telemetry over OTLP to an OpenTelemetry Collector, directly from inside the NetBox processes (web workers, RQ workers, RQ work-horses). Each signal is an independent module, switched on or off in configuration.

## Signals

| Signal | Default | Description |
|---|---|---|
| [Logs](signals/logs.md) | on | NetBox's Python logs, exported through the OTel Logs API. |
| [Audit records](signals/audit.md) | on | One structured record per `ObjectChange` (create, update, delete). |
| [Traces](signals/traces.md) | off | Spans for Django requests, PostgreSQL, Redis, outbound HTTP and RQ jobs. |
| [Metrics](signals/metrics.md) | off | HTTP and job metrics, change counters, optional process runtime metrics. |

## Quick start

Install the package into the NetBox virtual environment (see [Installation](installation.md)), then add this to `configuration.py`:

```python
PLUGINS = ["netbox_opentelemetry_plugin"]
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://otel-collector:4318"},
    },
}
```

Restart NetBox and the RQ workers to pick up the change.

## Compatibility

| Plugin | NetBox | Python |
|---|---|---|
| 0.1.x | 4.7.x | 3.12, 3.13, 3.14 |

## What stays outside the exporters

Some things never go through the plugin's exporters, and are left to stdout, stderr and platform log collection instead:

- Web server access logs, typically on stdout.
- Startup errors and crashes, typically on stderr.
- The plugin's own warnings, on the `netbox_opentelemetry_plugin` logger, which is never exported; with NetBox's default `LOGGING = {}` these reach stderr through Python's last-resort handler.

## Related work

There is a related upstream request to bring native OpenTelemetry support into NetBox core: [netbox-community/netbox#22642](https://github.com/netbox-community/netbox/issues/22642).
