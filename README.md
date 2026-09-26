# netbox-opentelemetry-plugin

NetBox plugin that exports NetBox telemetry over OTLP to an OpenTelemetry Collector, from inside the NetBox processes. Each signal is a module that can be enabled or disabled in configuration.

Status: under development. Currently implemented: application logs, audit records and traces, with support for forking web servers and RQ work-horses.

## Web servers

The plugin works with the web servers NetBox documents:

- Granian (netbox-docker): nothing to configure.
- gunicorn, with or without `preload_app`: nothing to configure.
- uWSGI: nothing to configure with pyuwsgi. With the classic uwsgi binary, set `enable-threads = true`, otherwise nothing is exported (the plugin logs a warning).

Each worker process exports with its own `service.instance.id`.

## Background jobs

Log lines written by jobs and custom scripts in the RQ work-horse are flushed before the horse exits, waiting at most `rq.flush_timeout` seconds (default 5). A job that hits its own `job.timeout` is still flushed: rq handles the timeout inside `perform_job`, and the flush wrap runs after `perform_job` returns either way.

Only a horse killed with SIGKILL cannot flush: a hang past `job.timeout + 60` seconds (when the worker SIGKILLs the horse's process group), a stop-job command, or a cold shutdown of the worker process. Keep `rq.flush_timeout` well below that 60 second margin.

This flush happens after the job, not during it: web requests never block on it, but a worker starts its next job only once the current horse has exited. If the Collector is unreachable, every job pays up to the full `flush_timeout` before the worker moves on, capping that worker at roughly one job per `flush_timeout` for the duration of the outage. For busy queues, set a lower `rq.flush_timeout` (for example 1 to 2 seconds) to bound that cost.

Audit records for changes made inside a job or a custom script, and log lines written by them, are flushed by this same mechanism: both wait on the horse before it exits. With `rq.enabled = False` or `rq.patch_worker = False`, the flush wrap is never installed, and records buffered when the horse exits are lost.

## Traces

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "traces": {"enabled": True},
    },
}
```

With `traces.enabled`, the plugin instruments Django, psycopg, redis and outbound `requests` calls, and adds a span around every RQ job (`rq.patch_worker = False` also removes job spans, along with the work-horse flush described above). A NetBox web request produces one span per HTTP request (named from the route template), with `netbox.request_id` and, when the request is authenticated, `enduser.id`. Database queries, redis commands and outbound HTTP calls made while handling that request or job are child spans of it.

When a job is enqueued from inside a request or another job (rq's `Queue.enqueue_job`), the current trace context travels with it in `job.meta`, so the job's span, and anything it does, such as an outbound webhook call, share the same trace as the request that enqueued it. This is how a change made through the API, its audit record, the log lines the request writes, and any webhook fired by it end up correlated under one trace id.

`traces.instrument` selects which instrumentors run (`django`, `psycopg`, `redis`, `requests`, all on by default). `traces.excluded_urls` skips instrumenting URLs matching any of the listed regular expressions (searched anywhere in the URL, not just as a prefix), `/static/`, `/metrics` and `/api/status/` by default.

Sampling is controlled by `traces.sampler` and `traces.sampler_arg` (default `parentbased_traceidratio` at `1.0`, meaning every trace with a sampled or absent parent is kept). Unset, these fall back to the standard `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` environment variables, then to the default above. A database, redis or outbound HTTP span with no parent, such as a query made outside any request or job, for example an RQ worker's own polling and heartbeat commands, is dropped before the sampler runs rather than starting a trace of its own; this applies to any parentless CLIENT span, not only database and redis.

With a `parentbased_*` sampler (the default), an inbound `traceparent` header is honoured, as is standard OTel behaviour: a client can force sampling and choose the trace id for its request. Use a non-parentbased sampler, or strip the header at a proxy in front of NetBox, if that matters for your deployment.

Nothing sensitive leaves the process through spans: no HTTP header value, no SQL bind parameter, and no URL query string, inbound or outbound, is exported. A query string on an exported URL attribute becomes `?REDACTED`; the same replacement is applied to status descriptions and exception event attributes, so a secret embedded in a failed request's URL cannot leak through those either. A psycopg query span that ends in a database error has its status description reduced to the bare exception type, and its exception event's message and stacktrace dropped, since PostgreSQL error messages can carry the offending values (for example a unique constraint violation's `DETAIL`). Only the W3C trace context is copied into an RQ job's `meta`, never baggage.

Known limitations:

- A psycopg connection opened before the plugin's `ready()`, for example by startup code, is not traced until Django closes and reopens it (`CONN_MAX_AGE`).
- Only a job enqueued through rq's `Queue.enqueue_job` (used by `Queue.enqueue()`) carries the enqueuing trace's context forward. Jobs created through `enqueue_at`, `enqueue_in` or `enqueue_many` go through a different rq code path that never calls `enqueue_job`, so they start their own trace, including a job scheduled by `enqueue_at` from inside a request. A retried job, a requeued job, and a scheduled job later moved back onto its queue by the rq scheduler all reuse the same `Job` object and its `meta`, so they keep whatever context (if any) was stored when the job was first enqueued: a retry can therefore appear in the original request's trace, possibly much later.
- With django-rq's `COMMIT_MODE` set to `"request_finished"`, a job is enqueued after the request's span has already ended, so it is not linked to that request's trace.
- If a `TracerProvider` is already configured outside the plugin (for example by `opentelemetry-instrument`) and reused, the plugin's redaction and its filter for parentless database and redis spans do not apply; that provider's own configuration decides what is exported.
- If another rq exception handler registered before the plugin's own returns `False`, the job span still gets an ERROR status but no exception event, since the plugin's handler is never reached.
- The `exception.message` attribute of a log record (application logs, not spans) is not scrubbed for URL query strings.

## Audit records

Every committed `ObjectChange` (create, update, delete of any NetBox object) produces one OpenTelemetry log record, event name `netbox.object_change`, with the action, object type, object id, object representation, request id and the user name. This is emitted through the same log pipeline as application logs, but works independently of it: audit records are exported even with `logs.enabled = False`, as long as an exporter endpoint is configured.

The message, the related object (type and id) and the user name are included only when the underlying `ObjectChange` has them set; a change with no message or no related object omits those attributes entirely, rather than sending an empty value.

By default the record does not include the object's field data, only the attributes above. Setting `audit.include_data` to `True` adds the change's `prechange_data` and `postchange_data` as JSON strings. Before export, both are filtered recursively through `audit.exclude_fields`: any key whose name contains one of the listed strings (case-insensitive) is dropped, along with nested keys inside it. The default list covers `password`, `secret`, `token` and `key`; extend it for any other sensitive field names in your NetBox instance, for example free-text `comments` fields.

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "audit": {"include_data": True, "exclude_fields": ["password", "secret", "token", "key", "comments"]},
    },
}
```

`include_data` can make a record large, especially for objects with big JSON fields. Most Collectors reject a request above their configured body size limit, which drops the whole batch that record was in, not just that one record. Keep `include_data` off unless you need it, or add fields such as `config_context` and `local_context_data` to `audit.exclude_fields` to keep records small.

If a `LoggerProvider` is already configured before the plugin loads (for example by `opentelemetry-instrument`), the plugin detects and reuses it instead of building its own. That provider's queue is sized for whatever configured it, not for the plugin's own audit bursts, and the plugin does not resize it to fit `audit.include_data` traffic.

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
