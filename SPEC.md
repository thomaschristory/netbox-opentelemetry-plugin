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
- All OTel dependencies are regular dependencies, pinned to a tested range: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-exporter-otlp-proto-grpc` at `~=1.44.0`; the four instrumentation packages `opentelemetry-instrumentation-django`, `-psycopg`, `-redis`, `-requests` at `==0.65b0`; `-system-metrics` is added with the metrics milestone. Modules are selected in configuration, not at install time.
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
         -> role = detect_role()               web | rqworker | management | runserver_parent
                                                (rq_horse is assigned only after an announced fork, see 4.2)
         -> providers = detect_or_build(cfg)   reuse global SDK providers if already set
         -> for each enabled module: try install(ctx) except: warn, skip that module
         -> os.register_at_fork(before, after_in_parent, after_in_child) at import; uwsgi.post_fork_hook when under uWSGI
         -> atexit.register(shutdown)
```

- `detect_role` inspects `sys.argv` (`rqworker`, `runserver`, other management commands) and the presence of the `uwsgi` module.
- Management commands other than `rqworker` (`migrate`, `nbshell`, ...) install only logs and audit. Traces and metrics are skipped so short commands do not start exporter threads.
- The `runserver` autoreloader parent installs nothing; the serving child does.
- Provider detection: if a non-default global TracerProvider, MeterProvider or LoggerProvider is already set, reuse it and do not install instrumentors that report `is_instrumented_by_opentelemetry`.
- The plugin's own Resource: `service.name`, `service.version` (NetBox version), `service.instance.id` (hostname + PID, regenerated after fork), `netbox.plugin.version`, `netbox.process.role`, plus configured resource attributes.

### 4.2 Fork handling

- The OTel SDK (1.44) re-initialises its own batch processors in a forked child: new worker thread, new locks, cleared queue. A parent's unflushed records are therefore not exported twice, and no flush before fork is needed.
- The SDK keeps the parent's Resource and shares the parent's exporter (the OTLP HTTP exporter's `requests.Session` is inherited as is). `after_in_child` therefore rebuilds the Resource (new `service.instance.id`), a fresh exporter and a new LoggerProvider, points the logging handler at it (`Module.after_fork`) and drops the inherited provider without shutting it down (its locks were copied from the parent in an unknown state; the SDK has already reset its batch processor). A provider configured outside the plugin is left alone.
- `before` fork takes the bootstrap lock and `after_in_parent` releases it; the child gets a new lock.
- The re-initialisation is keyed on the PID and runs at most once per process, so it is safe when both Python's at-fork hooks and uWSGI's `post_fork_hook` fire.
- A child of the `rqworker` process takes the role `rq_horse` only when the RQ integration announced the fork (`fork_work_horse`); other forks of the worker process, such as the RQ scheduler, keep the role `rqworker`.
- If rebuilding in the child fails, the child logs one warning. When the plugin owns the provider, it also detaches the logging handler and exports nothing rather than using the parent's exporter connection; with a provider configured outside the plugin, only the warning is logged and the inherited, unmodified provider keeps being used.
- The TracerProvider handed to instrumentors is a switchable wrapper created once; after fork the child builds its own SDK TracerProvider and exporter and swaps them in. On a failed rebuild, span export stops in that process (no-op provider).

### 4.3 Install types (verified against NetBox 4.7.1 and netbox-docker 5.1.1 sources)

| Install | Web server | App loaded before fork | Notes |
|---|---|---|---|
| netbox-docker 5.1 (also Helm chart) | Granian WSGI, 4 processes, many threads each | No. On Python 3.14 Granian starts workers with spawn, each runs `ready()` | Fork hooks not exercised |
| Bare metal, gunicorn (`contrib/gunicorn.py`) | 5 workers x 3 threads | No by default, yes with `preload_app` | gunicorn forks with `os.fork()`, Python at-fork hooks fire |
| Bare metal, uWSGI (`contrib/uwsgi.ini`) | `master = true`, no `lazy-apps` | Yes | uWSGI forks in C; the plugin chains `uwsgi.post_fork_hook` |
| `manage.py runserver` | autoreloader | The serving child is a separate process (`subprocess`, `RUN_MAIN=true`) | Parent skipped |

- uWSGI runs Python's at-fork hooks only with `py-call-osafterfork`, so the plugin also sets `uwsgi.post_fork_hook` (chaining any hook already set).
- The classic uwsgi binary does not run threads created by the application unless `enable-threads` (or `threads`) is set; the batch processors would then never export. The plugin logs one warning in that case. pyuwsgi (the PyPI package) always has thread support.
- All providers are created once per process. Trace context uses contextvars and is thread safe under threaded workers.
- RQ is identical on every install type: `manage.py rqworker`, `NetBoxRQWorker` (subclass of `rq.Worker`), one forked horse per job, horse exits with `os._exit`.

### 4.4 RQ integration

- `BaseWorker.perform_job` is wrapped: after the job (success, failure or timeout, which rq handles inside `perform_job`), a forked work-horse (`worker.is_horse`) flushes the log provider on a helper thread and waits at most `rq.flush_timeout` seconds. The horse then leaves with `os._exit`, so this is the only flush it gets.
- `Worker.fork_work_horse` is wrapped to announce the fork, so the child is labelled `rq_horse`.
- Each wrap first checks the target's signature (`self, job, queue`). On mismatch: one warning, that wrap is skipped, everything else continues. Wraps are marked and never applied twice.
- `rq.patch_worker = False` disables both wraps; nothing is flushed in the horse then, it keeps the `rqworker` role instead of being labelled `rq_horse`, and no job span is created (the `perform_job` wrap is what creates the span, see 6.3).
- Not covered: a horse killed by the parent (SIGKILL after `job.timeout + 60` s) cannot flush. `SpawnWorker` (not used by NetBox) starts a fresh interpreter and is not detected as a horse.
- `Queue.enqueue_job` is wrapped (web and worker processes alike) to store the current span's W3C trace context in `job.meta["netbox_otel_context"]`, gated by `rq.propagate_context`. `BaseWorker.perform_job` runs each job inside a CONSUMER span (see 6.3), parented on that stored context when present. The horse flush (above) flushes the log provider and the tracer provider in parallel, still bounded by the single `rq.flush_timeout`.

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
            # Audit uses the log pipeline exporter (logs.endpoint, then exporter.*) and works
            # even when logs.enabled is False.
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
            "propagate_context": True,  # traces milestone
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
- Receiver: `post_save` on `core.models.ObjectChange`, `created=True` only (a later `created=False` save of the same record, which NetBox uses to fold an M2M change into the record made earlier in the same request, is ignored by the receiver itself); a raw save (for example loading a fixture) is also ignored. Emission is deferred with `transaction.on_commit`, so a rolled back change produces nothing.
- Emitted through the OTel Logger API directly (independent of the logs module and of stdout). Uses the logs exporter settings (`logs.endpoint`, then `exporter.*`); the LoggerProvider is built when either `logs` or `audit` is enabled, so audit works with `logs.enabled = False`.
- Instrumentation scope `netbox_opentelemetry_plugin.audit`. Event name `netbox.object_change`. Severity INFO. Timestamp: the `ObjectChange` row's `time` (the change's own time, not when the record is emitted).
- Body: `"<action> <app_label>.<model> <object_repr>"`, for example `"update dcim.device edge-rtr-01"`.
- Attributes, only these and only when noted:
  - `netbox.change.id` (int), `netbox.change.action` (str), `netbox.change.object_type` (str, `app_label.model`), `netbox.change.object_id` (int), `netbox.change.object_repr` (str), `netbox.change.request_id` (str): always present.
  - `netbox.change.message` (str): only when non-empty.
  - `netbox.change.related_object_type` and `netbox.change.related_object_id`: only when both are set.
  - `enduser.id` (str, the change's `user_name`): only when non-empty.
  - With `audit.include_data`: `netbox.change.prechange_data` and `netbox.change.postchange_data`, each a JSON string (`json.dumps(..., sort_keys=True)`) of the stored value filtered recursively by `exclude_fields` (case-insensitive substring match on keys), only when that stored value is not null.
- With `audit.include_data`, the pre/post data is read from the database at commit time rather than from the `post_save` instance, batched per thread (one query per up to 1000 pending changes, not one query per change). This is what lets a later, same-request update to `postchange_data` (the M2M case above) reach the record that was already queued for that change.
- `ObjectChange` rows are emitted regardless of which database alias they were written to, including a netbox-branching branch's own alias (NetBox picks the alias with `router.db_for_write`). With `audit.include_data`, that data is read back from the same alias the change was written to, batched separately per alias, so a pk that exists on more than one alias never picks up another alias's data.
- Never raises into NetBox: the receiver and the commit callback each catch every exception. On failure, one warning is logged per process, naming only the exception type (never its message, which could contain object data).

Known limitations:
- If NetBox updates an M2M change record in a later transaction than the one that created it, the data sent with the first record does not include that later update. This only matters with `include_data`; same-transaction M2M updates are covered above.
- The log pipeline buffers up to 20,000 records per process while audit is on. A single commit larger than that can drop records, and the SDK reports this only on its own logger, which the plugin does not export.
- When a `LoggerProvider` configured outside the plugin is reused (4.1's provider detection), its queue is not resized; the 20,000 figure above applies only to a provider the plugin builds itself.
- With `include_data`, a record can grow large for an object with big JSON fields. Most Collectors reject a request above their configured body size limit, dropping the whole batch that record was in, not just that record. Keep `include_data` off, or exclude large fields such as `config_context` and `local_context_data` in `audit.exclude_fields`.

### 6.3 Traces
- Django: one span per request, named `<METHOD> <route template>`. `RequestSpanMiddleware` adds `netbox.request_id` (str, always when the span records) and `enduser.id` (str, username, only when authenticated).
- psycopg: span per query; statement recorded, bind parameters never.
- redis: span per command, only when listed in `traces.instrument`.
- requests: span per outbound call, `traceparent` injected; URL query strings replaced with `REDACTED`.
- No HTTP headers are captured by any instrumentor.
- RQ: CONSUMER span, scope `netbox_opentelemetry_plugin.rq`, name `rq.job <func_name>` (`rq.job` when the job cannot be deserialised). Attributes: `messaging.system = "rq"`, `messaging.destination.name` (queue name), `messaging.message.id` (rq job id), and for NetBox `core.Job` jobs `netbox.job.id` (int) and `netbox.job.name` (str, only when non-empty). Parent: the context in `job.meta["netbox_otel_context"]` when `rq.propagate_context` is on, otherwise a root span. On failure: status ERROR, exception event.
- The instrumentations use the stable HTTP semantic conventions (`url.full`, `http.request.method`, ...): the plugin sets `OTEL_SEMCONV_STABILITY_OPT_IN=http` unless an operator already set it.
- The Django and requests instrumentors also export the standard HTTP semantic-convention attributes they normally add (`client.address`, `user_agent.original`, `server.address`, `server.port`, `network.*`, `url.scheme`, `url.path`, `http.route`, `http.request.method`, `http.response.status_code`, `error.type`, and similarly for psycopg's DB attributes): only headers, query strings and bind parameters are redacted, everything else the instrumentors set is exported as is.
- A CLIENT span with no parent (a psycopg or redis call outside a request or job, for example a startup query, or an RQ worker's own poll and heartbeat commands) is dropped before the configured sampler runs, so it does not start a trace of its own.
- Redaction runs at export, in a `SpanProcessor` ahead of the exporting one, and covers every exported span kind (including SERVER and CONSUMER, not only the outbound CLIENT spans instrumentors add attributes to): `http.request.header.*` / `http.response.header.*` are removed; `url.query` becomes `REDACTED`; `url.full`, `http.url` and `http.target` end in `?REDACTED` when they had a query; status descriptions and exception event attributes have any `path?query` replaced with `path?REDACTED`. Every span, regardless of kind or attributes, also has its status description reduced to the text before the first `:` (a description with no `:`, such as a bare exception type name or a plugin-set description like "job failed", is left unchanged), and `exception.message`/`exception.stacktrace` dropped from any `exception` event, keeping `exception.type`, `exception.escaped` and any other event attributes. This applies whether the error surfaces on a psycopg query span, or unhandled on the request's SERVER span (Django's middleware exits its span activation with the exception still propagating) or a job's CONSUMER span (`modules/rq.py`), since a PostgreSQL error message can carry the offending values (for example a unique constraint violation's `DETAIL`). The full text is still available in log records. A span that fails to redact is dropped rather than exported as is.
- Only the W3C trace context (`traceparent`) is written to `job.meta["netbox_otel_context"]`; baggage is not copied, so a job never receives arbitrary request-scoped data through this channel.
- An inbound `traceparent` header is honoured, as with any OTel SDK using a `parentbased_*` sampler (the default): a client can force sampling and choose the trace id for its own request. An operator for whom this matters should use a non-parentbased sampler, or strip the header at a proxy in front of NetBox.

Known limitations:
- A psycopg connection opened before the plugin's `ready()` (for example by startup code) is not traced until Django closes and reopens it (`CONN_MAX_AGE`).
- Only a job enqueued through rq's `Queue.enqueue_job` (the method `Queue.enqueue()` itself calls) carries the enqueuing trace's context. `enqueue_at`, `enqueue_in` and `enqueue_many` reach rq internals (`schedule_job`, `_enqueue_job`) that never call `enqueue_job`, so those jobs start their own trace, including one scheduled by `enqueue_at` from inside a request. A retried job, a requeued job, and a job the rq scheduler moves from scheduled back onto its queue all reuse the same `Job` and its `meta` rather than being enqueued through `enqueue_job` again, so they keep whatever context was stored at the original enqueue: a retry can therefore appear in the original request's trace, possibly much later.
- With django-rq `COMMIT_MODE = "request_finished"`, the enqueue happens after the request's span has already ended, so the job is not linked to the request's trace.
- With a `TracerProvider` configured outside the plugin (4.1's provider detection), the plugin's redaction and the parentless-CLIENT-span filter do not apply: that provider's own configuration governs what is exported.
- If an rq exception handler registered before the plugin's own returns `False` (telling rq to stop walking the handler stack), the job span still gets status ERROR, but without an exception event, since the plugin's handler was not reached.
- The `exception.message` attribute of a log record (logs module) is not scrubbed for URL query strings; only span attributes, status descriptions and span exception events are.

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
- No HTTP headers, no SQL bind parameters, no URL query strings (inbound or outbound) in spans.
- Every exported span keeps only the exception type in its status description and in any `exception` event; the message and stacktrace are not exported. The full text is still available in log records.

## 8. Failure behaviour

| Situation | Behaviour |
|---|---|
| OTel import error | one warning, plugin inactive, NetBox runs |
| Invalid config | one warning per affected module, others run |
| No endpoint for a module | one warning, module disabled |
| Collector unreachable | exporter retries in background, then drops; bounded queues; web requests never block. Each RQ work-horse still waits up to `rq.flush_timeout` after its job before exiting; the job's status is already saved, and the worker starts its next job only once the horse has exited. During an outage this caps each worker at roughly one job per `flush_timeout` |
| Horse flush slow | bounded by `rq.flush_timeout` (helper thread); delays the worker's next job, not the job's own completion |
| Error in audit receiver | caught and logged; save proceeds |
| uWSGI without `enable-threads` | one warning naming the fix |
| RQ wrap target signature changed | one warning, that wrap skipped |
| SDK already configured externally | reuse it; skip already applied instrumentors |

## 9. Dev environment

- `dev/docker-compose.yml`: `netboxcommunity/netbox:v4.7.1` (netbox-docker 5.1.1), worker, Postgres, Redis, `otelcol-contrib` with OTLP receiver and `debug` (verbosity detailed) and `file` (JSON) exporters.
- Plugin mounted and installed editable (`uv pip install -e`) by an entrypoint wrapper.
- Compose profiles: default (Granian), `gunicorn` (gunicorn with `--preload`, host port 8001), `uwsgi` (pyuwsgi master with forked workers, host port 8002), `ui` (`grafana/otel-lgtm`, Collector forwards to it; not implemented yet). The uWSGI threads warning path is covered by unit tests only, since pyuwsgi always has thread support.
- `dev/scripts/otel_demo.py`: custom script logging at every level, touching objects, with an option to raise.
- Make targets: `dev`, `dev-gunicorn`, `dev-uwsgi`, `down`, `test`, `e2e`, `lint`, `format`, `logs-collector`. `test-netbox` (NetBox integration tests) is added with the audit milestone.

## 10. Testing

1. Unit (pytest, no NetBox): config precedence and validation, masking, module install idempotency with in-memory exporters (`InMemorySpanExporter`, `InMemoryLogExporter`, `InMemoryMetricReader`), feedback filter, `exclude_fields` filter, audit mapping, a real `os.fork()` test for child rebuild, RQ wrap signature checks against rq 2.12 including the mismatch path.
2. NetBox integration: a NetBox layer under `tests_netbox/`, run with the NetBox test runner (`make test-netbox`) against a NetBox v4.7.1 checkout with Postgres and Redis services, and in CI by the `netbox-integration` job. Covers audit: create, update and delete of a prefix via the API produce the expected records with a distinct `request_id` each; `include_data` on filters `exclude_fields` out of `postchange_data`; a bulk API create of 10 devices shares one `request_id`; a rolled back change emits nothing while a committed one emits exactly one record. Covers traces: a request produces a span carrying `netbox.request_id` and `enduser.id`; query strings are redacted; excluded URLs are not instrumented; a log record written inside a request carries the request's trace id; a real `rq.Worker` run via `SimpleWorker` (jobs run in the worker process itself, no fork) runs a job whose span carries context propagated from the enqueuing request, an outbound HTTP call inside the job is its own child span, and NetBox job attributes (`netbox.job.id`, `netbox.job.name`) are present. The forking work-horse, and the web edit that reaches a webhook, are proven live by `make e2e`, not by the NetBox test runner.
3. End-to-end (`make e2e`): drives the compose stack via the API and the sample script (normal and raising), then asserts on the Collector's JSON file output. Runs per web server profile. With traces enabled, also proves: an API request produces a SERVER span carrying `netbox.request_id` and `enduser.id`; a log record and an audit record from the same request carry its trace id and the request's span id; a webhook fired by an edit shares that edit's trace, with the RQ job span parented on the request's span and the outbound call parented on the job span.

CI (GitHub Actions): ruff; unit tests on Python 3.12, 3.13, 3.14; NetBox integration on 4.7.1; e2e for Granian and uWSGI on nightly or manual trigger.

## 11. Milestones and acceptance

| M | Content | Acceptance |
|---|---|---|
| M0 | scaffold, PluginConfig with min/max version, compose stack, CI | NetBox 4.7.1 loads the plugin; CI green |
| M1 | config, `otel.py`, bootstrap, logs module | a login produces a log record in the Collector with correct severity, body, resource; missing endpoint gives one warning and NetBox serves |
| M2 | fork safety | Granian, gunicorn (with preload) and uWSGI export from every worker PID; no duplicate handlers after autoreload; no-threads warning fires |
| M3 | RQ flush | all log lines of the sample script arrive, including when it raises |
| M4 | audit | create, update, delete of a prefix and bulk import of 10 devices produce the expected records with shared `request_id`; rollback produces none; filtering proven by tests (integration tests: devices via API bulk create; e2e: prefixes) |
| M5 | traces, RQ spans, propagation | API requests produce spans; logs carry the matching trace id; a webhook fired by an edit shares the edit's trace |
| M6 | metrics, change counters, runtime | all metrics of 6.4 visible in the Collector; job metrics come from the worker parent |
| M7 | docs and release | MkDocs Material site (install per install type, config reference, OpenShift Collector example, limitations), README with compatibility matrix, CHANGELOG, tagged release on PyPI |
