# netbox-opentelemetry-plugin

netbox-opentelemetry-plugin exports NetBox telemetry (logs, audit records, traces and metrics) over OTLP to an OpenTelemetry Collector, directly from inside the NetBox processes (web workers, RQ workers, RQ work-horses). Each signal is an independent module, switched on or off in configuration.

## Signals

| Signal | Default | Description |
|---|---|---|
| Logs | on | NetBox's Python logs, exported through the OTel Logs API. |
| Audit records | on | One structured record per `ObjectChange` (create, update, delete). |
| Traces | off | Spans for Django requests, PostgreSQL, Redis, outbound HTTP and RQ jobs. |
| Metrics | off | HTTP and job metrics, change counters, optional process runtime metrics. |

## Compatibility

| Plugin | NetBox | Python |
|---|---|---|
| 0.1.x | 4.7.0 to 4.7.x | 3.12, 3.13, 3.14 |

`min_version = "4.7.0"` and `max_version = "4.7.99"` in the plugin's `PluginConfig` enforce this NetBox range; NetBox refuses to load the plugin outside it.

## Dependencies

Installing the package pulls in these OpenTelemetry packages, no others:

- `opentelemetry-api` ~= 1.44.0
- `opentelemetry-sdk` ~= 1.44.0
- `opentelemetry-exporter-otlp-proto-http` ~= 1.44.0
- `opentelemetry-exporter-otlp-proto-grpc` ~= 1.44.0
- `opentelemetry-instrumentation-django` == 0.65b0
- `opentelemetry-instrumentation-psycopg` == 0.65b0
- `opentelemetry-instrumentation-redis` == 0.65b0
- `opentelemetry-instrumentation-requests` == 0.65b0
- `opentelemetry-instrumentation-system-metrics` == 0.65b0

No NetBox model, no database migration and no UI of its own.

## Quick start

Install the package into the NetBox virtual environment:

```bash
source /opt/netbox/venv/bin/activate
pip install netbox-opentelemetry-plugin
```

netbox-docker's venv has no `pip`, only `uv`: use `uv pip install netbox-opentelemetry-plugin` in a custom image built on top of the upstream one. See the [installation page](https://thomaschristory.github.io/netbox-opentelemetry-plugin/installation/) for netbox-docker, the Helm chart, and bare metal gunicorn and uWSGI.

Enable the plugin in `configuration.py`:

```python
PLUGINS = ["netbox_opentelemetry_plugin"]
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://otel-collector:4318"},
    },
}
```

Turn on traces and metrics too:

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://otel-collector:4318"},
        "traces": {"enabled": True},
        "metrics": {"enabled": True},
    },
}
```

Restart the NetBox web processes and the RQ workers to pick up the change.

## Documentation

- [Full documentation](https://thomaschristory.github.io/netbox-opentelemetry-plugin/)
- [Installation](https://thomaschristory.github.io/netbox-opentelemetry-plugin/installation/)
- [Configuration reference](https://thomaschristory.github.io/netbox-opentelemetry-plugin/configuration/)
- [Logs](https://thomaschristory.github.io/netbox-opentelemetry-plugin/signals/logs/), [audit records](https://thomaschristory.github.io/netbox-opentelemetry-plugin/signals/audit/), [traces](https://thomaschristory.github.io/netbox-opentelemetry-plugin/signals/traces/), [metrics](https://thomaschristory.github.io/netbox-opentelemetry-plugin/signals/metrics/)
- [How it works](https://thomaschristory.github.io/netbox-opentelemetry-plugin/how-it-works/)
- [Data safety](https://thomaschristory.github.io/netbox-opentelemetry-plugin/data-safety/)
- [Failure behaviour](https://thomaschristory.github.io/netbox-opentelemetry-plugin/failure-behaviour/)
- [Collector on Kubernetes](https://thomaschristory.github.io/netbox-opentelemetry-plugin/collector/)
- [Limitations](https://thomaschristory.github.io/netbox-opentelemetry-plugin/limitations/)
- [Changelog](https://thomaschristory.github.io/netbox-opentelemetry-plugin/changelog/)

## Licence

Apache 2.0. See the [LICENSE](https://github.com/thomaschristory/netbox-opentelemetry-plugin/blob/main/LICENSE) file.
