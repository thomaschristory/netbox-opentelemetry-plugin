# Configuration

## Where settings live

Every setting lives under `PLUGINS_CONFIG["netbox_opentelemetry_plugin"]` in NetBox's `configuration.py`, resolved once per process at startup. There is no live reload: change a setting, then restart the process (or the container) to pick it up.

The plugin's defaults are its own (`conf.DEFAULTS`), not NetBox's `PluginConfig.default_settings`. NetBox merges `default_settings` into `PLUGINS_CONFIG` one level deep only, so a nested dict such as `exporter` or `logs` would be replaced wholesale rather than merged key by key, and a pre-filled default would make an explicit value indistinguishable from one the operator never set, which breaks the `OTEL_*` environment fallback described below.

## Precedence

For every setting, in order: an explicit `PLUGINS_CONFIG` value, then a signal-specific `OTEL_*` environment variable, then the generic `OTEL_*` variable, then the default. Setting `OTEL_SDK_DISABLED=true` in the environment disables the whole plugin, before any other setting is even looked at.

Two things do not fit that simple per-key picture:

- The `exporter.*` values in `PLUGINS_CONFIG` (`protocol`, `headers`, `timeout`, `insecure`, `certificate`) apply to every signal at once, and an explicit `exporter.*` value beats even a signal-specific environment variable such as `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`.
- The plugin has no per-signal `headers` or `timeout` setting of its own in `PLUGINS_CONFIG`. To give one signal its own headers or timeout, use the signal-specific environment variable (`OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS`, `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`) instead, and leave `exporter.headers` / `exporter.timeout` unset in `PLUGINS_CONFIG`.

## Endpoints

Each signal (`logs`, `traces`, `metrics`) resolves its own OTLP endpoint in this order:

1. The signal's own `endpoint` (`logs.endpoint`, `traces.endpoint`, `metrics.endpoint`), used as is.
2. `exporter.endpoint`. Over HTTP, the signal's path is appended; over gRPC, the value is used as is.
3. `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` (for example `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), used as is.
4. `OTEL_EXPORTER_OTLP_ENDPOINT`. Over HTTP, the signal's path is appended; over gRPC, the value is used as is.

If none of these resolve to a value, that signal logs one warning and disables itself; the rest of the plugin keeps running.

For example, with `exporter.endpoint = "http://collector:4318"` and the default HTTP protocol, logs export to `http://collector:4318/v1/logs`, traces to `http://collector:4318/v1/traces`, and metrics to `http://collector:4318/v1/metrics`. Over gRPC, `exporter.endpoint = "http://collector:4317"` is used as is for every signal, since gRPC has no per-signal path.

Audit records use the logs endpoint: `logs.endpoint`, then `exporter.*`, the same order as above. This means audit records still export even with `logs.enabled = False`, as long as an endpoint resolves for the logs pipeline.

## Reference

Every key in `conf.DEFAULTS`, its default, and its environment fallback if it has one.

| Setting | Default | Environment fallback | Description |
|---|---|---|---|
| `enabled` | `True` | - | Master switch for the whole plugin. `False` disables every signal. There is no environment fallback for this key itself; `OTEL_SDK_DISABLED=true` has the same effect from the environment. |
| `exporter.endpoint` | `None` | `OTEL_EXPORTER_OTLP_ENDPOINT` | Generic OTLP endpoint, used when a signal has no endpoint of its own. See Endpoints above. |
| `exporter.protocol` | `"http/protobuf"` | `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL`, then `OTEL_EXPORTER_OTLP_PROTOCOL` | `"http/protobuf"` or `"grpc"`. Applies to every signal; an explicit value here beats the environment variables. |
| `exporter.headers` | `{}` | `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS`, then `OTEL_EXPORTER_OTLP_HEADERS` | Dict of strings, sent as OTLP request headers. Values are never logged. The environment form is `key=value,key2=value2`, percent-decoded, for example `authorization=Bearer%20abc123`. |
| `exporter.timeout` | `10` | `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`, then `OTEL_EXPORTER_OTLP_TIMEOUT` | Export request timeout, in seconds. The environment variable is also read as seconds, matching how this plugin reads it (it does not follow the SDK's default unit for this variable). |
| `exporter.insecure` | `None` | `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE`, then `OTEL_EXPORTER_OTLP_INSECURE` | gRPC only, ignored over HTTP. `None` (the default) is inferred from the endpoint's URL scheme: `https://` gives a secure channel, `http://` gives an insecure one. Set explicitly to override that inference. |
| `exporter.certificate` | `None` | `OTEL_EXPORTER_OTLP_<SIGNAL>_CERTIFICATE`, then `OTEL_EXPORTER_OTLP_CERTIFICATE` | Path to a CA certificate file, read at export time (not at startup). Over gRPC, its bytes build the channel's SSL credentials (`otel._grpc_credentials`); this has no effect if `insecure` ends up `True`. Over HTTP, the path is passed straight through as the exporter's `certificate_file`, which the HTTP client uses to verify the collector's certificate. |
| `service_name` | `"netbox"` | `OTEL_SERVICE_NAME` | The `service.name` resource attribute on every span, log record and metric point. |
| `resource_attributes` | `{}` | - | Dict of string keys to string, bool, int or float values, added to the Resource on every signal. The OpenTelemetry SDK separately merges `OTEL_RESOURCE_ATTRIBUTES` from the environment underneath this; both this setting and the plugin's own resource keys (`service.name`, `service.version`, and so on) win over an environment attribute of the same name. See [How it works](how-it-works.md). |
| `logs.enabled` | `True` | - | Switches the logs signal on or off. |
| `logs.endpoint` | `None` | `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | Logs (and audit) OTLP endpoint. See Endpoints above. |
| `logs.loggers` | `["netbox", "django", "rq"]` | - | Logger name prefixes whose records are exported. |
| `logs.level` | `"INFO"` | - | Minimum level exported, as a Python logging level name (`"WARNING"`) or number (`30`). |
| `logs.set_logger_levels` | `False` | - | If `True`, also lowers the effective level of each logger in `logs.loggers` to `logs.level`, instead of only filtering records the logger already emits. See [Logs](signals/logs.md). |
| `audit.enabled` | `True` | - | Switches audit record export on or off, independently of `logs.enabled`. Audit records use the logs exporter (see Endpoints above), so they still export with `logs.enabled = False`. |
| `audit.include_data` | `False` | - | If `True`, includes the changed object's field values in the exported record, not only which fields changed. See [Audit records](signals/audit.md) and [Data safety](data-safety.md). |
| `audit.exclude_fields` | `["password", "secret", "token", "key"]` | - | Field name fragments dropped from `audit.include_data` output. Matching is case-sensitive substring matching against the field name. |
| `traces.enabled` | `False` | - | Switches the traces signal on or off. |
| `traces.endpoint` | `None` | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Traces OTLP endpoint. See Endpoints above. |
| `traces.sampler` | `"parentbased_traceidratio"` | `OTEL_TRACES_SAMPLER` | One of `always_on`, `always_off`, `traceidratio`, `parentbased_always_on`, `parentbased_always_off`, `parentbased_traceidratio`. |
| `traces.sampler_arg` | `1.0` | `OTEL_TRACES_SAMPLER_ARG` | Number from `0` to `1`. Only meaningful for the two `traceidratio` samplers: the fraction of traces sampled. |
| `traces.instrument` | `["django", "psycopg", "redis", "requests"]` | - | Subset of `django`, `psycopg`, `redis`, `requests` to instrument. |
| `traces.excluded_urls` | `["/static/", "/metrics", "/api/status/"]` | - | Regular expressions searched anywhere in the request URL; a match is not traced. Entries must not contain a comma (the Django instrumentation takes one comma-separated string, so a comma inside an entry would silently split it into two patterns). Also applied to HTTP server metrics, and honoured even with `traces.enabled = False`. |
| `metrics.enabled` | `False` | - | Switches the metrics signal on or off. |
| `metrics.endpoint` | `None` | `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | Metrics OTLP endpoint. See Endpoints above. |
| `metrics.export_interval` | `60` | `OTEL_METRIC_EXPORT_INTERVAL` | Export interval, in **seconds**. The environment variable is in **milliseconds** (the OpenTelemetry SDK's own convention for this name) and is divided by 1000 before use. The minimum is 1 second; a smaller value (from either source) is raised to 1 second with a warning. |
| `metrics.change_counters` | `True` | - | Switches the `netbox.object_changes` and RQ job counters on or off. See [Metrics](signals/metrics.md). |
| `metrics.runtime` | `False` | - | Switches the `process.*` and `cpython.gc.*` system and runtime metrics on or off. |
| `rq.enabled` | `True` | - | Switches the RQ integration on or off: the work-horse role, the job span, and the flush around each job. |
| `rq.patch_worker` | `True` | - | If `False`, removes the wraps around `fork_work_horse` and `perform_job` entirely: no `rq_horse` role, no job span, and nothing flushed before a horse exits. |
| `rq.propagate_context` | `True` | - | Switches trace context propagation from the enqueueing request or job into the job span on or off. |
| `rq.flush_timeout` | `5` | - | Seconds a work-horse waits, at most, for its log and tracer providers to flush before exiting. Keep this well below 60 seconds: several paths in rq (a job running more than `job.timeout + 60` seconds, a stop-job or kill-horse command, a cold shutdown) `SIGKILL` the horse instead of letting it finish this flush, so whatever was still buffered is lost either way; a lower `flush_timeout` bounds how much a hung job can leave stranded. See [How it works](how-it-works.md). |

## Other environment variables

These are read by the OpenTelemetry SDK or the OTLP exporters directly, not by this plugin's own configuration code, so there is no corresponding `PLUGINS_CONFIG` key:

- `OTEL_RESOURCE_ATTRIBUTES`: merged into the Resource by the SDK itself, underneath `resource_attributes` and the plugin's own resource keys. See [How it works](how-it-works.md).
- `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`: honoured by the metrics exporter; the default is cumulative temporality.
- `OTEL_SEMCONV_STABILITY_OPT_IN`: the plugin sets this to `http` (selecting the stable HTTP semantic conventions) unless it is already set in the environment, so an operator's own value is always kept.

## Validation

The plugin resolves `PLUGINS_CONFIG` once per process, and a bad value never stops NetBox from starting:

- An unknown key logs one warning and is otherwise ignored (this does not apply inside `exporter.headers` or `resource_attributes`, whose keys are chosen by the operator).
- A wrong type or an invalid value inside one section (`exporter`, `logs`, `audit`, `traces`, `metrics`, `rq`) disables only that section, with one warning; the rest of the plugin keeps running. A problem resolving the logs exporter disables logs and audit together, since they share it.
- An invalid top-level value (`enabled`, `service_name`, `resource_attributes` or `exporter` not of the expected type) disables the whole plugin, with one warning.
- A signal with no endpoint that resolves at all logs one warning and disables itself; the other signals are unaffected.
- The resolved configuration is logged once at `DEBUG` level, with every header value replaced and any URL credentials in an endpoint replaced by `***`.

Every one of these warnings goes to the `netbox_opentelemetry_plugin` logger, on stdout, and is never exported: nothing about a misconfiguration leaves the process. See [Failure behaviour](failure-behaviour.md) and [Data safety](data-safety.md).

## Examples

Each example below is a complete `PLUGINS_CONFIG` block.

Minimal, everything else at its default (the endpoint can also come from `OTEL_EXPORTER_OTLP_ENDPOINT` instead):

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://collector:4318"},
    },
}
```

gRPC, with a bearer token header and a private CA certificate:

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {
            "endpoint": "collector.example.com:4317",
            "protocol": "grpc",
            "headers": {"authorization": "Bearer secret-token"},
            "certificate": "/etc/ssl/certs/otel-ca.pem",
        },
    },
}
```

Traces and metrics on, sampling 10% of traces:

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://collector:4318"},
        "traces": {"enabled": True, "sampler": "parentbased_traceidratio", "sampler_arg": 0.1},
        "metrics": {"enabled": True},
    },
}
```

A separate Collector per signal, each given as a full endpoint URL:

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "logs": {"endpoint": "http://logs-collector:4318/v1/logs"},
        "traces": {"enabled": True, "endpoint": "http://traces-collector:4318/v1/traces"},
        "metrics": {"enabled": True, "endpoint": "http://metrics-collector:4318/v1/metrics"},
    },
}
```

Audit records with field values included, and a longer list of excluded fields:

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://collector:4318"},
        "audit": {
            "include_data": True,
            "exclude_fields": ["password", "secret", "token", "key", "api_key", "private_key"],
        },
    },
}
```
