# Traces

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://collector:4318"},
        "traces": {"enabled": True},
    },
}
```

With `traces.enabled`, the plugin instruments Django, psycopg, redis and outbound `requests` calls (`traces.instrument` selects the subset, all four by default), and wraps every RQ job in a span. `rq.patch_worker = False` removes the job spans, along with the work-horse flush (see [How it works](../how-it-works.md#rq-work-horses)); with no job span to parent them, any psycopg or redis CLIENT span made while running the job is dropped by the parentless-span filter (see Sampling below), the same as an RQ worker's own polling and heartbeat commands. `rq.enabled = False` removes the RQ integration entirely, including enqueue-time context propagation and the job spans.

## Spans

- **Django**: one span per HTTP request, kind SERVER (or INTERNAL when a span is already current, for example one instrumentor called from inside another), named `<METHOD> <route template>`, for example `GET dcim/devices/`. `RequestSpanMiddleware` (registered through `PluginConfig.middleware`, so it runs after NetBox's own middleware) adds `netbox.request_id` whenever the span is recording, and `enduser.id` (the username) when the request is authenticated; the user is read after the view runs, since API token authentication happens inside DRF's view dispatch.
- **psycopg**: one span per query, kind CLIENT. The statement is recorded as `db.statement` (the instrumentor's stable `db.query.text` attribute is a separate opt-in, under a database semantic-convention flag the plugin does not set), bind parameters never (`capture_parameters=False`, and `enable_commenter=False` so no SQL comment is appended either).
- **redis**: one span per command, kind CLIENT, only for a command issued while `redis` is in `traces.instrument`.
- **requests**: one span per outbound HTTP call, kind CLIENT, with a `traceparent` header injected for the receiving service to continue the trace. A query string in the recorded URL is exported as `?REDACTED` (see Redaction below).
- No HTTP header value is exported. An operator can turn on header capture for these instrumentors through their own environment variables (see the OpenTelemetry Python instrumentation docs); even then, the redaction below strips every captured `http.request.header.*` / `http.response.header.*` attribute before it reaches the exporter.
- **RQ**: a CONSUMER span around each job, instrumentation scope `netbox_opentelemetry_plugin.rq`, named `rq.job <qualified function>`, for example `rq.job extras.jobs.ScriptJob.handle` (or plain `rq.job` when the job's data cannot be deserialised; see [Metrics](metrics.md) for how the qualified name is built). Attributes: `messaging.system` (`"rq"`), `messaging.destination.name` (the queue name), `messaging.message.id` (the rq job id), and, only for a NetBox `core.Job`, `netbox.job.id` (int) and `netbox.job.name` (only when non-empty). Its parent is the context stored in `job.meta["netbox_otel_context"]` when `rq.propagate_context` is on and that key is present; otherwise it starts a new trace. On failure, the span gets status ERROR with an exception event (see Known limitations below for an edge case where the exception event is missing).

The instrumentors use the stable HTTP semantic conventions (`url.full`, `http.request.method`, `http.response.status_code`, and so on): the plugin sets `OTEL_SEMCONV_STABILITY_OPT_IN=http` before instrumenting, unless an operator already set that environment variable, in which case the existing value is kept. Aside from the redaction below, the Django and requests instrumentors export the standard attributes they normally add under that convention, for example `client.address`, `user_agent.original`, `server.address`, `server.port`, `url.scheme`, `url.path`, `http.route`, and the equivalent database attributes from psycopg.

## Selecting URLs and instrumentors

`traces.instrument` is a list of instrumentor names (`django`, `psycopg`, `redis`, `requests`); a name left out is never instrumented for traces. `traces.excluded_urls` is a list of regular expressions, defaulting to `/static/`, `/metrics` and `/api/status/`; a URL matching any of them anywhere in the string (not only as a prefix) gets no Django span, and, see [Metrics](metrics.md), no HTTP server duration measurement either, whether traces are on or off.

Leaving `django` out of `traces.instrument` while keeping psycopg, redis or requests in it means a web request never gets a SERVER span, so any of those CLIENT spans made while handling that request has no parent and is dropped by the same parentless-span filter (see Sampling below).

## Sampling

`traces.sampler` and `traces.sampler_arg` control the sampling decision (default `parentbased_traceidratio` at `1.0`: every trace with a sampled or absent parent is kept). Unset, they fall back to the standard `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` environment variables, then to the default above.

Before the configured sampler runs, a CLIENT span with no parent is dropped outright, for any instrumentor, not only psycopg and redis: an RQ worker's own polling and heartbeat commands, or a startup query made outside any request or job, would otherwise start a trace of its own.

With a `parentbased_*` sampler (the default), an inbound `traceparent` header is honoured, as is standard OTel behaviour: a client can force sampling and choose the trace id for its own request. Use a non-parentbased sampler, or strip the header at a proxy in front of NetBox, if that matters for your deployment.

## Context propagation into jobs

When a job is enqueued through rq's `Queue.enqueue_job` (the method `Queue.enqueue()` itself calls) while traces are enabled and `rq.propagate_context` is on, the current trace context is written to `job.meta["netbox_otel_context"]`, provided a valid span context is current at that point; this includes a span the sampler chose not to sample, not only a recording one, but nothing is stored when no span is current at all. Only the W3C trace context is stored there, using the trace-context propagator directly: `traceparent` always, and `tracestate` too when the current span context carries one. Baggage is never copied into a job's `meta`, so a job cannot pick up arbitrary request-scoped data through this channel. On the other side, `perform_job` reads that key back and starts the job's span as a child of it.

Known gaps in this propagation, from rq's own code paths:

- Only `Queue.enqueue_job` is wrapped. `enqueue_at`, `enqueue_in` and `enqueue_many` reach rq internals (`schedule_job`, `_enqueue_job`) that never call `enqueue_job`, so a job created through any of them starts its own trace, including one scheduled by `enqueue_at` from inside a request.
- A retried job, a requeued job, and a job the rq scheduler moves from scheduled back onto its queue all reuse the same `Job` object and its `meta`, rather than being enqueued through `enqueue_job` again, so they keep whatever context (if any) was stored at the original enqueue. A retry can therefore surface in the original request's trace, possibly much later.
- With django-rq's `COMMIT_MODE` set to `"request_finished"`, the enqueue happens after the request's span has already ended, so the job is not linked to that request's trace.

## Baggage

Whenever traces or metrics are on, the plugin wraps the configured global propagator (`OTEL_PROPAGATORS`, or the OpenTelemetry default `tracecontext,baggage`) in one that never handles baggage. Trace-context formats are kept: `traceparent` and `tracestate`, or `b3`, `xray` and so on when `OTEL_PROPAGATORS` selects them, are still extracted from inbound requests and injected on outbound calls.

Baggage, in any format that goes through the OpenTelemetry baggage API (W3C `baggage`, and the jaeger and OT baggage headers), is neither extracted from an inbound request nor injected on an outbound call, with traces on or metrics only. A client's `baggage` header is therefore never attached to the request context, and never reaches a downstream service through NetBox's `requests` calls. Baggage set in-process, for example by a script or another plugin, is not sent on an instrumented outbound call either. A baggage entry in `OTEL_PROPAGATORS` has no effect.

The wrapper applies to every instrumentor that uses the global propagator, including one applied outside the plugin (for example by `opentelemetry-instrument`), as long as the plugin's traces or metrics are on in that process. It stays in place after shutdown, so a request racing the shutdown is still covered.

If the configured propagator cannot be loaded (for example a misspelled name in `OTEL_PROPAGATORS`), the plugin logs one warning and disables its instrumentation module in that process, so nothing is instrumented without the wrapper. Logs, audit and the RQ integration keep working. Runtime metrics, when on, are disabled with a warning of their own, since the system-metrics instrumentor imports the same OpenTelemetry propagator module.

A propagator set with `set_global_textmap` after startup, in a process that does not fork afterwards, replaces the wrapper. The plugin re-applies the wrapper after a fork, so this concerns code that runs after `ready()` in a web worker or a worker parent.

## Redaction

A `SpanProcessor` ahead of the exporting one redacts every span at export, whatever its kind, before it reaches the OTLP exporter:

- `http.request.header.*` and `http.response.header.*` attributes are removed outright.
- `url.query` becomes `REDACTED`.
- `url.full`, `http.url` and `http.target` keep everything up to and including their `?`, with the query itself replaced by `REDACTED`.
- Every span's status description is reduced to the text before its first `:` (a description with no colon, such as a bare exception type name or the plugin's own `"job failed"`, is left as is), and any `?query` text inside it is scrubbed the same way as a URL attribute.
- Any `exception` event keeps only `exception.type`, `exception.escaped` and its other attributes; `exception.message` and `exception.stacktrace` are dropped.

This covers an error surfacing on any span kind, not only the outbound CLIENT spans an instrumentor adds URL or header attributes to: a PostgreSQL error message can carry the offending values (a unique constraint violation's `DETAIL`, for example), and that error can surface unhandled on the request's SERVER span or a job's CONSUMER span just as easily as on the psycopg query span itself. The full, unredacted text is still available in log records (see [Logs](logs.md)). A span that fails to redact is dropped rather than exported as is. See [Data safety](../data-safety.md) for the full picture across every signal.

## Known limitations

- A psycopg connection opened before the plugin's `ready()`, for example by startup code, is not traced until Django closes and reopens it (`CONN_MAX_AGE`).
- If a `TracerProvider` is already configured outside the plugin and reused (see [How it works](../how-it-works.md#an-sdk-configured-outside-the-plugin)), the plugin's redaction and its filter for parentless CLIENT spans do not apply: that provider's own configuration decides what is exported.
- If an rq exception handler registered before the plugin's own returns `False`, the job span still gets status ERROR, but without an exception event, since the plugin's handler is never reached.
- `exception.message` on a log record (not a span) is not scrubbed for URL query strings; only span attributes, status descriptions and span exception events are.

See the [configuration reference](../configuration.md#reference) for every `traces.*` setting and its default.
