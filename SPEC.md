# netbox-opentelemetry-plugin: implementation spec

## 1. Goal

Export NetBox telemetry to an OpenTelemetry pipeline over OTLP, directly from inside the NetBox processes (web workers, RQ workers, RQ work-horses). Three signals, each an independent module that can be switched on or off:

- **Logs**: NetBox's Python logs, plus one structured audit record per `ObjectChange`.
- **Traces**: Django requests, PostgreSQL (psycopg), Redis, outbound HTTP (webhooks) and RQ jobs, with context propagated from the originating request into the job.
- **Metrics**: HTTP and job metrics, change counters, optional process runtime metrics.

Records go to an OpenTelemetry Collector, which routes them to the backend. Stdout logging stays in place as a safety net for startup errors, crashes and web server access logs, which the plugin cannot see.

Related upstream request: netbox-community/netbox#22642 (native OTel in core, open, under review). This plugin delivers the same capability as a plugin and does not depend on the outcome of that issue.

## 2. Scope and targets

In scope:
- The three signal modules above, individually configurable.
- Correct behaviour under every documented NetBox install type (see 4.3).
- Detection of an already configured OTel SDK (for example `opentelemetry-instrument`), reusing it instead of installing a second one.

Out of scope:
- Web server access logs (left to stdout and platform collection).
- Periodic object count metrics (DB load in every process, duplicate series).
- Any UI, models or migrations.

Targets:
- NetBox 4.7.x (`min_version = "4.7.0"`, `max_version = "4.7.99"`). Wider support is added later, one minor version at a time, each with CI coverage.
- Python 3.12, 3.13, 3.14 (NetBox 4.7 requires >= 3.12).
- Reference deployment: netbox-docker 5.1.x image on OpenShift / Kubernetes. Bare metal (gunicorn, uWSGI) is supported equally.

## 3. Packaging

- Distribution name `netbox-opentelemetry-plugin`, import name `netbox_opentelemetry_plugin`.
- `pyproject.toml` with hatchling. Lint and format with ruff, tests with pytest.
- All OTel dependencies are regular dependencies, pinned to a tested range: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-exporter-otlp-proto-grpc`, `opentelemetry-instrumentation-django`, `-psycopg`, `-redis`, `-requests`, `-system-metrics`. Modules are selected in configuration, not at install time.
- No `django_apps`, no models, no migrations.
- README contains a compatibility matrix (plugin version to NetBox version), as recommended by the NetBox plugin docs.

## 4. Architecture

```
netbox_opentelemetry_plugin/
  __init__.py      PluginConfig (min/max version, default_settings); ready() -> super().ready(), bootstrap.install(), fully wrapped
  conf.py          PLUGINS_CONFIG + OTEL_* env -> frozen dataclasses, one per module
                   (not config.py: NetBox reads the package attribute `config` as the PluginConfig)
  otel.py          ALL OpenTelemetry SDK imports, including opentelemetry.sdk._logs; Resource, exporters, processors
  bootstrap.py     per-process guard, role detection, provider detection, module registry, fork hooks, atexit, shutdown
  middleware.py    adds netbox.request_id and enduser.id to the active request span
  modules/
    base.py        Module protocol: enabled(cfg), install(ctx), after_fork(ctx), flush(timeout), shutdown()
    logs.py        LoggingHandler on configured loggers, feedback-loop filter
    audit.py       ObjectChange post_save -> transaction.on_commit -> log record; change counters
    traces.py      Django, psycopg, redis, requests instrumentors
    metrics.py     MeterProvider, instrumentation metrics
    runtime.py     system-metrics instrumentor
    rq.py          job spans, job metrics, horse flush, context propagation
tests/
dev/               docker compose, NetBox config, Collector config, sample script
docs/              MkDocs Material
```

Modules depend on `config` and a shared context object (providers, Resource, process role), never on each other. The `ObjectChange` receiver is registered once and serves both audit records and change counters.

### 4.1 Bootstrap

```
ready()  -> super().ready()
         -> guard: if already installed for os.getpid(): return
         -> cfg = config.resolve()             (failure: warn once, stop)
         -> role = detect_role()               web | rqworker | rq_horse | management | runserver_parent
         -> providers = detect_or_build(cfg)   reuse global SDK providers if already set
         -> for each enabled module: try install(ctx) except: warn, skip that module
         -> os.register_at_fork(before=..., after_in_child=...)
         -> atexit.register(shutdown)
```

- `detect_role` inspects `sys.argv` (`rqworker`, `runserver`, other management commands) and the presence of the `uwsgi` module.
- Management commands other than `rqworker` (`migrate`, `nbshell`, ...) install only logs and audit. Traces and metrics are skipped so short commands do not start exporter threads.
- The `runserver` autoreloader parent installs nothing; the serving child does.
- Provider detection: if a non-default global TracerProvider, MeterProvider or LoggerProvider is already set, reuse it and do not install instrumentors that report `is_instrumented_by_opentelemetry`.
- The plugin's own Resource: `service.name`, `service.version` (NetBox version), `service.instance.id` (hostname + PID, regenerated after fork), `netbox.plugin.version`, `netbox.process.role`, plus configured resource attributes.

### 4.2 Fork handling

- `before` fork: `force_flush()` with a short timeout so buffered records are not duplicated into the child.
- `after_in_child` in a web process (uWSGI, gunicorn with preload): discard the inherited processors and metric reader without exporting, rebuild them with a new `service.instance.id`, point the logging handler at the new provider.
- `after_in_child` in an RQ work-horse (fork during `execute_job`): rebuild span and log processors as synchronous (`Simple*`), no metric reader. The `perform_job` wrapper also flushes in `finally`.

### 4.3 Install types (verified against NetBox 4.7.1 and netbox-docker 5.1.1 sources)

| Install | Web server | App loaded before fork | Notes |
|---|---|---|---|
| netbox-docker 5.1 (also Helm chart) | Granian WSGI, 4 processes, many threads each | No, each process loads the app | Fork hook not exercised but harmless |
| Bare metal, gunicorn (`contrib/gunicorn.py`) | 5 workers x 3 threads | No by default, yes with `preload_app` | Fork hook required with preload |
| Bare metal, uWSGI (`contrib/uwsgi.ini`) | `master = true`, no `lazy-apps` | Yes | Fork hook required; `enable-threads` required |
| `manage.py runserver` | autoreloader | `ready()` in parent and child | Parent skipped |

- uWSGI without `enable-threads` does not run threads created by the application, so batch processors and the periodic metric reader never export. The plugin checks `uwsgi.opt` and logs one warning naming the fix. Docs list it as a requirement.
- All providers are created once per process. Trace context uses contextvars and is thread safe under threaded workers.
- RQ is identical on every install type: `manage.py rqworker`, `NetBoxRQWorker` (subclass of `rq.Worker`), one forked horse per job, horse exits with `os._exit`.

### 4.4 RQ integration (only in the `rqworker` process, except propagation)

- Worker parent: wrap `execute_job`. After the horse exits, read the job's final status and `started_at` / `ended_at`, record `netbox.rq.jobs` and `netbox.rq.job.duration`. Register the `netbox.rq.queue.depth` gauge here.
- Horse: wrap `perform_job`. Extract the propagated context from `job.meta`, start a CONSUMER span, run, record exception and ERROR status on failure, end span, `force_flush(rq.flush_timeout)` in `finally`.
- Web process: wrap `Queue.enqueue_job` to inject the current trace context into `job.meta`.
- Every wrap checks the target signature with `inspect.signature` first. On mismatch: one warning, that wrap is skipped, everything else continues. Wraps are marked to prevent double application.
- Opt-outs: `rq.patch_worker = False` disables worker wraps (operators may then set `RQ['WORKER_CLASS']` to the class the plugin ships; note NetBox 4.7 warns on any non-default worker class name). `rq.propagate_context = False` disables the enqueue wrap.

## 5. Configuration

All settings live in `PLUGINS_CONFIG["netbox_opentelemetry_plugin"]`, resolved once per process. Defaults live in `config.DEFAULTS`, not in `PluginConfig.default_settings`: NetBox merges `default_settings` one level deep only (nested dicts would be replaced wholesale), and pre-filled defaults would make explicit values indistinguishable from defaults, which breaks the `OTEL_*` fallback.

Precedence for every setting: explicit `PLUGINS_CONFIG` value, then the signal specific `OTEL_*` variable, then the generic `OTEL_*` variable, then the default. `OTEL_SDK_DISABLED=true` disables everything.

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "enabled": True,
        "exporter": {
            "endpoint": None,  # OTEL_EXPORTER_OTLP_ENDPOINT
            "protocol": "http/protobuf",  # OTEL_EXPORTER_OTLP_PROTOCOL; or "grpc"
            "headers": {},  # OTEL_EXPORTER_OTLP_HEADERS; values never logged
            "timeout": 10,  # OTEL_EXPORTER_OTLP_TIMEOUT (seconds)
            "insecure": None,  # defaults to inferred from the endpoint scheme, grpc only
            "certificate": None,  # OTEL_EXPORTER_OTLP_CERTIFICATE
        },
        "service_name": "netbox",  # OTEL_SERVICE_NAME
        "resource_attributes": {},  # OTEL_RESOURCE_ATTRIBUTES
        "logs": {
            "enabled": True,
            "endpoint": None,  # OTEL_EXPORTER_OTLP_LOGS_ENDPOINT
            "loggers": ["netbox", "django", "rq"],
            "level": "INFO",
            "set_logger_levels": False,  # if True, also lower listed loggers to `level`
        },
        "audit": {
            "enabled": True,
            "include_data": False,
            "exclude_fields": ["password", "secret", "token", "key"],
        },
        "traces": {
            "enabled": False,
            "endpoint": None,  # OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
            "sampler": "parentbased_traceidratio",  # OTEL_TRACES_SAMPLER
            "sampler_arg": 1.0,  # OTEL_TRACES_SAMPLER_ARG
            "instrument": ["django", "psycopg", "redis", "requests"],
            "excluded_urls": ["/static/", "/metrics", "/api/status/"],
        },
        "metrics": {
            "enabled": False,
            "endpoint": None,  # OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
            "export_interval": 60,  # OTEL_METRIC_EXPORT_INTERVAL (seconds)
            "change_counters": True,
            "runtime": False,
        },
        "rq": {
            "enabled": True,
            "patch_worker": True,
            "propagate_context": True,
            "flush_timeout": 5,  # seconds, horse flush
        },
    }
}
```

Validation:
- Unknown keys: warning.
- Wrong types: the affected module is disabled with one warning, other modules continue.
- A module with no resolvable endpoint: one warning, module disabled.
- The resolved config is logged at DEBUG level with header values masked.

## 6. Signals

### 6.1 Logs
- SDK `LoggingHandler` attached to each logger in `logs.loggers`, at `logs.level`.
- Body: formatted message. Severity: mapped from Python level.
- Attributes: `code.file.path`, `code.function.name`, `code.line.number`, `logger.name`, `thread.name`; `exception.type`, `exception.message`, `exception.stacktrace` when present.
- Arbitrary `extra=` fields are not exported (allowlist principle). The SDK `LoggingHandler` exports every non-reserved `LogRecord` attribute by default, so the plugin subclasses it and filters attributes against an allowlist.
- NetBox's default `LOGGING` is empty, so the `netbox` logger inherits the root level (WARNING). INFO records only reach the handler if `LOGGING` sets a lower level or `set_logger_levels` is true.
- Providers are passed explicitly to handlers and instrumentors; the plugin does not set the global OTel providers.
- Inside an active span, records carry `trace_id` and `span_id`.
- Feedback-loop filter: records from `netbox_opentelemetry_plugin` and `opentelemetry.*` loggers are rejected by the handler. The plugin's own logger writes to stdout only.

### 6.2 Audit
- Receiver: `post_save` on `core.models.ObjectChange`, `created=True` only, emission deferred with `transaction.on_commit` so rolled back changes produce nothing.
- Emitted through the OTel Logger API directly (independent of the logs module and of stdout). Uses the logs exporter settings (`logs.endpoint`, then `exporter.*`); the LoggerProvider is built when either `logs` or `audit` is enabled.
- Event name `netbox.object_change`, severity INFO, body `"<action> <app_label>.<model> <object_repr>"`.
- Attributes: `netbox.change.id`, `netbox.change.action`, `netbox.change.object_type`, `netbox.change.object_id`, `netbox.change.object_repr`, `netbox.change.request_id`, `netbox.change.message`, `netbox.change.related_object_type`, `netbox.change.related_object_id` (when present), `enduser.id` (username).
- With `audit.include_data`: `netbox.change.prechange_data` and `netbox.change.postchange_data` as JSON strings, filtered recursively by `exclude_fields` (case-insensitive substring match on keys).
- Errors in the receiver are caught and logged; the save always proceeds.

### 6.3 Traces
- Django: one span per request, named from the route template. The plugin middleware adds `netbox.request_id` and `enduser.id`.
- psycopg: span per query; statement recorded, bind parameters never.
- redis: span per command, only when listed in `traces.instrument`.
- requests: span per outbound call, `traceparent` injected; URL query strings replaced with `REDACTED`.
- No HTTP headers are captured by any instrumentor.
- RQ: CONSUMER span `rq.job <func_name>` with `messaging.system=rq`, `messaging.destination.name`, `messaging.message.id`, `netbox.job.id` and `netbox.job.name` for NetBox `Job`s. Parent is the enqueuing request when context was propagated.

### 6.4 Metrics

| Name | Type | Attributes | Source |
|---|---|---|---|
| `http.server.request.duration` | histogram (s) | method, route, status | Django instrumentor |
| `http.client.request.duration` | histogram (s) | method, server.address, status | requests instrumentor |
| `netbox.rq.job.duration` | histogram (s) | queue, function, outcome | RQ worker parent |
| `netbox.rq.jobs` | counter | queue, function, outcome | RQ worker parent |
| `netbox.rq.queue.depth` | gauge | queue | RQ worker parent |
| `netbox.object_changes` | counter | action, object_type | audit receiver |
| `process.*` runtime metrics | various | | runtime module, off by default |

- Job metrics are recorded in the long-lived worker parent (cumulative, one series per worker). Horses have no metric reader.
- Every worker parent reports the same global queue depth; docs recommend aggregating with `max`.

## 7. Data safety

- Nothing leaves the process unless it is listed in section 6.
- Audit data payloads are off by default and filtered when on.
- Exporter header values never appear in logs, debug output or exception messages.
- No HTTP headers, no SQL bind parameters, no outbound URL query strings in spans.

## 8. Failure behaviour

| Situation | Behaviour |
|---|---|
| OTel import error | one warning, plugin inactive, NetBox runs |
| Invalid config | one warning per affected module, others run |
| No endpoint for a module | one warning, module disabled |
| Collector unreachable | exporter retries in background, then drops; bounded queues; no request blocks |
| Horse flush slow | bounded by `rq.flush_timeout`; delays only job completion |
| Error in audit receiver | caught and logged; save proceeds |
| uWSGI without `enable-threads` | one warning naming the fix |
| RQ wrap target signature changed | one warning, that wrap skipped |
| SDK already configured externally | reuse it; skip already applied instrumentors |

## 9. Dev environment

- `dev/docker-compose.yml`: `netboxcommunity/netbox:v4.7.1` (netbox-docker 5.1.1), worker, Postgres, Redis, `otelcol-contrib` with OTLP receiver and `debug` (verbosity detailed) and `file` (JSON) exporters.
- Plugin mounted and installed editable (`uv pip install -e`) by an entrypoint wrapper.
- Compose profiles: default (Granian), `gunicorn-preload`, `uwsgi` (with `enable-threads`), `uwsgi-nothreads` (warning path), `ui` (`grafana/otel-lgtm`, Collector forwards to it).
- `dev/scripts/otel_demo.py`: custom script logging at every level, touching objects, with an option to raise.
- Make targets: `dev`, `dev-gunicorn`, `dev-uwsgi`, `test`, `test-netbox`, `e2e`, `lint`, `logs-collector`.

## 10. Testing

1. Unit (pytest, no NetBox): config precedence and validation, masking, module install idempotency with in-memory exporters (`InMemorySpanExporter`, `InMemoryLogExporter`, `InMemoryMetricReader`), feedback filter, `exclude_fields` filter, audit mapping, a real `os.fork()` test for child rebuild, RQ wrap signature checks against rq 2.12 including the mismatch path.
2. NetBox integration (NetBox test runner, NetBox v4.7.1 checkout, Postgres and Redis services): audit on create, update, delete; no audit on rollback; bulk import of 10 objects shares one `request_id`; request spans carry `netbox.request_id`; logs inside requests carry the trace id; a real forking `rq.Worker` runs a job and its spans, logs and parent metrics arrive.
3. End-to-end (`make e2e`): drives the compose stack via the API and the sample script (normal and raising), then asserts on the Collector's JSON file output. Runs per web server profile.

CI (GitHub Actions): ruff; unit tests on Python 3.12, 3.13, 3.14; NetBox integration on 4.7.1; e2e for Granian and uWSGI on nightly or manual trigger.

## 11. Milestones and acceptance

| M | Content | Acceptance |
|---|---|---|
| M0 | scaffold, PluginConfig with min/max version, compose stack, CI | NetBox 4.7.1 loads the plugin; CI green |
| M1 | config, `otel.py`, bootstrap, logs module | a login produces a log record in the Collector with correct severity, body, resource; missing endpoint gives one warning and NetBox serves |
| M2 | fork safety | Granian, gunicorn-preload and uWSGI export from every worker PID; no duplicate handlers after autoreload; no-threads warning fires |
| M3 | RQ flush | all log lines of the sample script arrive, including when it raises |
| M4 | audit | create, update, delete of a prefix and bulk import of 10 devices produce the expected records with shared `request_id`; rollback produces none; filtering proven by tests |
| M5 | traces, RQ spans, propagation | API requests produce spans; logs carry the matching trace id; a webhook fired by an edit shares the edit's trace |
| M6 | metrics, change counters, runtime | all metrics of 6.4 visible in the Collector; job metrics come from the worker parent |
| M7 | docs and release | MkDocs Material site (install per install type, config reference, OpenShift Collector example, limitations), README with compatibility matrix, CHANGELOG, tagged release on PyPI |
