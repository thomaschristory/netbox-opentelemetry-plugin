# Configuration

## Where settings live

Every setting lives under `PLUGINS_CONFIG["netbox_opentelemetry_plugin"]` in NetBox's `configuration.py`, resolved once per process at startup. There is no live reload: change a setting, then restart the process (or the container) to pick it up.

The plugin's defaults are its own (`conf.DEFAULTS`), not NetBox's `PluginConfig.default_settings`. NetBox merges `default_settings` into `PLUGINS_CONFIG` one level deep only, so a nested dict such as `exporter` or `logs` would be replaced wholesale rather than merged key by key, and a pre-filled default would make an explicit value indistinguishable from one the operator never set, which breaks the `OTEL_*` environment fallback described below.

## Precedence

For every setting, in order: an explicit `PLUGINS_CONFIG` value, then a signal-specific `OTEL_*` environment variable, then the generic `OTEL_*` variable, then the default. Setting `OTEL_SDK_DISABLED=true` in the environment disables the whole plugin. That check runs after the top-level type check and after scanning `PLUGINS_CONFIG` for unknown keys (any unknown-key warnings from that scan are still produced), but before every other setting is resolved.

Two things do not fit that simple per-key picture:

- The `exporter.*` values in `PLUGINS_CONFIG` (`protocol`, `headers`, `timeout`, `insecure`, `certificate`, `insecure_skip_verify`) apply to every signal at once, and an explicit `exporter.*` value beats even a signal-specific environment variable such as `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`.
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
| `exporter.timeout` | `10` | `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`, then `OTEL_EXPORTER_OTLP_TIMEOUT` | Export request timeout, in seconds. The environment variable is read as seconds too by the installed OTLP exporters (the published OpenTelemetry specification names this variable in milliseconds, but the Python exporters this plugin uses treat the value as seconds, and the plugin does not convert it). |
| `exporter.insecure` | `None` | `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE`, then `OTEL_EXPORTER_OTLP_INSECURE` | gRPC only, ignored over HTTP. `None` (the default) is inferred from the endpoint's URL scheme: an `https://` endpoint resolves to a secure channel, an `http://` endpoint to an insecure one, and an endpoint with no recognized scheme (a bare `host:port`, as gRPC endpoints are often written) also resolves to a secure channel. An `https://` endpoint is always forced to a secure channel, even if `insecure` is explicitly set to `True`. |
| `exporter.certificate` | `None` | `OTEL_EXPORTER_OTLP_<SIGNAL>_CERTIFICATE`, then `OTEL_EXPORTER_OTLP_CERTIFICATE` | Path to a CA certificate file. Over gRPC, its bytes are read once when the exporter is built (`otel._grpc_credentials`, at startup and again after every fork that rebuilds the exporter); a missing file fails that build, and rotating the certificate on disk needs a restart (or a fork rebuild) to take effect. This has no effect if `insecure` ends up `True`. Over HTTP, the path is passed straight through as the exporter's `certificate_file`, which the underlying HTTP client reads when it opens the TLS connection for each export, not when the exporter is built; a missing file only fails at export time, and rotating the certificate on disk is picked up on the exporter's next connection, without a restart. |
| `exporter.insecure_skip_verify` | `False` | none | HTTP only. `True` sends every export without checking the Collector's TLS certificate (chain, expiry and host name), for a Collector behind a self-signed certificate whose CA file is not at hand; prefer `certificate` whenever the CA file is available. Anyone able to intercept the connection can then read everything exported, including the `headers` values, so the plugin logs one warning per process and endpoint when it is on. Rejected as a configuration error over gRPC, since the gRPC Python client has no way to skip verification, and when `certificate` also resolves (from `PLUGINS_CONFIG` or an `OTEL_*_CERTIFICATE` variable), since the two contradict each other. The OpenTelemetry specification defines no environment variable for this, so it is read from `PLUGINS_CONFIG` only. |
| `service_name` | `"netbox"` | `OTEL_SERVICE_NAME` | The `service.name` resource attribute on every span, log record and metric point. |
| `resource_attributes` | `{}` | - | Dict of string keys to string, bool, int or float values, added to the Resource on every signal. The OpenTelemetry SDK separately merges `OTEL_RESOURCE_ATTRIBUTES` from the environment underneath this; both this setting and the plugin's own resource keys (`service.name`, `service.version`, and so on) win over an environment attribute of the same name. See [How it works](how-it-works.md). |
| `logs.enabled` | `True` | - | Switches the logs signal on or off. |
| `logs.endpoint` | `None` | `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | Logs (and audit) OTLP endpoint. See Endpoints above. |
| `logs.loggers` | `["netbox", "django", "rq"]` | - | Logger name prefixes whose records are exported. |
| `logs.level` | `"INFO"` | - | Minimum level exported, as a Python logging level name (`"WARNING"`) or number (`30`). |
| `logs.set_logger_levels` | `False` | - | If `True`, also lowers the effective level of each logger in `logs.loggers` to `logs.level`, instead of only filtering records the logger already emits. See [Logs](signals/logs.md). |
| `audit.enabled` | `True` | - | Switches audit record export on or off, independently of `logs.enabled`. Audit records use the logs exporter (see Endpoints above), so they still export with `logs.enabled = False`. |
| `audit.include_data` | `False` | - | If `True`, includes the changed object's field values in the exported record, not only which fields changed. See [Audit records](signals/audit.md) and [Data safety](data-safety.md). |
| `audit.exclude_fields` | `["password", "secret", "token", "key"]` | - | Field name fragments dropped from `audit.include_data` output. Matching is a case-insensitive substring match against field names, applied at every nesting level (inside nested dicts and lists), not only at the top level. |
| `traces.enabled` | `False` | - | Switches the traces signal on or off. |
| `traces.endpoint` | `None` | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Traces OTLP endpoint. See Endpoints above. |
| `traces.sampler` | `"parentbased_traceidratio"` | `OTEL_TRACES_SAMPLER` | One of `always_on`, `always_off`, `traceidratio`, `parentbased_always_on`, `parentbased_always_off`, `parentbased_traceidratio`. |
| `traces.sampler_arg` | `1.0` | `OTEL_TRACES_SAMPLER_ARG` | Number from `0` to `1`. Only meaningful for the two `traceidratio` samplers: the fraction of traces sampled. |
| `traces.instrument` | `["django", "psycopg", "redis", "requests"]` | - | Subset of `django`, `psycopg`, `redis`, `requests` to instrument. |
| `traces.excluded_urls` | `["/static/", "/metrics", "/api/status/"]` | - | Regular expressions searched anywhere in the request URL; a match is not traced. Entries must not contain a comma (the Django instrumentation takes one comma-separated string, so a comma inside an entry would silently split it into two patterns). Also applied to HTTP server metrics, and honoured even with `traces.enabled = False`. |
| `metrics.enabled` | `False` | - | Switches the metrics signal on or off. |
| `metrics.endpoint` | `None` | `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | Metrics OTLP endpoint. See Endpoints above. |
| `metrics.export_interval` | `60` | `OTEL_METRIC_EXPORT_INTERVAL` | Export interval, in **seconds**. The environment variable is in **milliseconds** (the OpenTelemetry SDK's own convention for this name) and is divided by 1000 before use. Must resolve to a positive number: `0` or a negative value is an invalid configuration and disables metrics entirely, with one warning. A positive value below 1 second is not rejected; it is raised to 1 second instead, with a separate warning. |
| `metrics.change_counters` | `True` | - | Switches the `netbox.object_changes` counter on or off. It does not gate RQ job metrics (`netbox.rq.job.duration`, `netbox.rq.jobs`), which instead depend on `metrics.enabled`, `rq.enabled` and `rq.patch_worker`. See [Metrics](signals/metrics.md). |
| `metrics.runtime` | `False` | - | Switches the `process.*` and `cpython.gc.*` system and runtime metrics on or off. |
| `rq.enabled` | `True` | - | Master switch for the whole RQ integration. `False` removes everything below: enqueue-time trace context propagation, the `netbox.rq.queue.depth` gauge, the work-horse role, the job span, RQ job metrics, and the flush around each job. Because `fork_work_horse` is never wrapped, the fork is not announced, so a work-horse keeps the `rqworker` role instead of becoming `rq_horse`, and if metrics are enabled it is treated as a full `rqworker` process with its own metrics pipeline. See [Limitations](limitations.md). |
| `rq.patch_worker` | `True` | - | If `False`, removes only the wraps around `fork_work_horse` and `perform_job`/`execute_job`: no `rq_horse` role, no job span, no RQ job metrics, and nothing flushed before a horse exits (a work-horse in this case keeps the `rqworker` role and is never flushed, since it is not distinguished from its parent). Enqueue-time context propagation and the `netbox.rq.queue.depth` gauge are wrapped separately and are unaffected by `patch_worker = False`. |
| `rq.propagate_context` | `True` | - | Switches trace context propagation from the enqueueing request or job into the job span on or off. |
| `rq.flush_timeout` | `5` | - | Seconds a work-horse waits, at most, for its providers to flush before exiting: its log provider and its tracer provider always, and its metrics pipeline too, on the rare process that owns one (in practice a work-horse never does; see [How it works](how-it-works.md)). Keep this well below 60 seconds: several paths in rq (a job running more than `job.timeout + 60` seconds, a stop-job or kill-horse command, a cold shutdown) `SIGKILL` the horse instead of letting it finish this flush, so whatever was still buffered is lost either way; a lower `flush_timeout` bounds how much a hung job can leave stranded. |
| `rq.flush_breaker_threshold` | `3` | - | Number of consecutive work-horse flushes that fail (the flush hits `rq.flush_timeout`, or exports return an error and none succeeds) after which work-horses skip their flush of that signal (log records, including audit records, and spans are counted separately), so that a Collector outage does not slow every job down by `flush_timeout`. `0` turns this off: every horse then flushes in full, whatever the outcome of the previous ones. Records buffered in a horse that skips its flush are dropped. See [Failure behaviour](failure-behaviour.md#collector-outage-and-rq-throughput). |
| `rq.flush_breaker_cooldown` | `30` | - | Seconds between two full flush attempts while work-horses skip their flush. After this delay, the next horse flushes in full: if its exports succeed, every horse flushes normally again; if not, horses skip their flush for another `flush_breaker_cooldown`. Bounds how long, after the Collector comes back, horses can still skip their flush. |

## Other environment variables

These are read by the OpenTelemetry SDK or the OTLP exporters directly, not by this plugin's own configuration code, so there is no corresponding `PLUGINS_CONFIG` key:

- `OTEL_RESOURCE_ATTRIBUTES`: merged into the Resource by the SDK itself, underneath `resource_attributes` and the plugin's own resource keys. See [How it works](how-it-works.md).
- `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`: honoured by the metrics exporter; the default is cumulative temporality.
- `OTEL_SEMCONV_STABILITY_OPT_IN`: the plugin sets this to `http` (selecting the stable HTTP semantic conventions) unless it is already set in the environment, so an operator's own value is always kept.
- `OTEL_PROPAGATORS`: selects the trace-context formats used for inbound and outbound HTTP (default `tracecontext,baggage`). Baggage propagation is off, whatever this variable lists, in every process where the plugin's traces or metrics are on (web and worker processes); see [Traces, baggage](signals/traces.md#baggage).

## Validation

The plugin resolves `PLUGINS_CONFIG` once per process, and a bad value never stops NetBox from starting:

- An unknown key logs one warning and is otherwise ignored (this does not apply inside `exporter.headers` or `resource_attributes`, whose keys are chosen by the operator).
- A wrong type or an invalid value inside `logs`, `audit`, `traces`, `metrics` or `rq` disables only that section, with one warning; the rest of the plugin keeps running.
- An invalid `exporter.*` value (`protocol`, `timeout`, `headers`, `insecure`, `certificate`, `insecure_skip_verify`) is resolved separately for each enabled signal, so it disables every enabled signal that uses the exporter, each with its own warning: logs and audit together (they share one exporter), traces, and metrics, whichever of those are enabled. A signal that is off to begin with is not affected, since its exporter is never resolved.
- An invalid top-level value (`enabled`, `service_name`, `resource_attributes` or `exporter` not of the expected type) disables the whole plugin, with one warning.
- A signal with no endpoint that resolves at all logs one warning and disables itself; the other signals are unaffected.
- The resolved configuration is logged once at `DEBUG` level, with every header value replaced and any URL credentials in an endpoint replaced by `***`.

Every one of these warnings goes to the `netbox_opentelemetry_plugin` logger and is never exported: nothing about a misconfiguration leaves the process. With NetBox's default `LOGGING = {}`, that logger has no handler of its own, so a warning falls through to Python's last-resort handler, which writes to stderr; configure `LOGGING` to send it elsewhere. See [Failure behaviour](failure-behaviour.md) and [Data safety](data-safety.md).

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

HTTP to a Collector behind a self-signed certificate, without verifying it (see `exporter.insecure_skip_verify` above before using this outside a lab):

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {
            "endpoint": "https://collector.lab.example:4318",
            "insecure_skip_verify": True,
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
