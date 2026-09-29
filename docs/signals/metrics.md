# Metrics

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://collector:4318"},
        "metrics": {"enabled": True},
    },
}
```

With `metrics.enabled`, the web processes and the RQ worker processes each build their own plugin-owned `MeterProvider` and export these metrics:

| Name | Instrument | Unit | Attributes | Recorded in |
|---|---|---|---|---|
| `http.server.request.duration` | histogram | s | `http.request.method`, `http.route`, `http.response.status_code`, `error.type` | web (Django instrumentor) |
| `http.client.request.duration` | histogram | s | `http.request.method`, `server.address`, `http.response.status_code`, `error.type` | web, RQ worker parent (requests instrumentor) |
| `netbox.rq.job.duration` | histogram | s | `messaging.destination.name`, `code.function.name`, `netbox.rq.job.outcome` | RQ worker parent |
| `netbox.rq.jobs` | counter | `{job}` | same as above | RQ worker parent |
| `netbox.rq.queue.depth` | observable gauge | `{job}` | `messaging.destination.name` | RQ worker (and its scheduler child) |
| `netbox.object_changes` | counter | `{change}` | `netbox.change.action`, `netbox.change.object_type` | web, RQ worker parent |
| `process.*`, `cpython.gc.*` | observable | various | instrumentor's own | web, RQ worker, with `metrics.runtime` |

Only the names and attribute keys above ever leave the process from the plugin's `MeterProvider`. Every other instrument name is dropped by a catch-all View before it reaches an exporter, and every listed instrument keeps only the attribute keys shown, dropping any other attribute the instrumentation library adds: for example, the Django instrumentor's own `http.server.active_requests` is dropped entirely, and `server.port` and `url.path` are dropped from the two HTTP histograms even though the instrumentors set them. `process.*` and `cpython.gc.*` are the exception: they keep whatever attributes their own instrumentor sets, since those are low-cardinality process states, not request data.

## HTTP metrics

`http.server.request.duration` and `http.client.request.duration` come from the same Django and requests instrumentors that produce the [trace](traces.md) spans, and are recorded whenever metrics are on, whether traces are on or off. `traces.excluded_urls` applies to `http.server.request.duration` either way: an excluded URL gets neither a span nor a duration measurement.

## Detached tracer

An instrumentor that is only needed for its metrics, either because traces are off entirely, or because traces are on but that instrumentor's name is not in `traces.instrument`, is still handed a tracer, but a detached one that always returns an invalid span and never makes it current. Concretely, this means:

- No span is started by that instrumentor.
- An inbound trace context is not made current from it, and nothing is forwarded from it on an outbound call: with traces off, log and audit records written while handling a request that carried a `traceparent` header have no trace id, and an outbound `requests` call from that same request sends no `traceparent`.
- Inbound baggage is dropped as well, and nothing is forwarded from it: Django's instrumentor extracts the request context through the global propagator whatever the tracer, and the plugin's wrapper around that propagator never extracts or injects baggage. See [Traces, baggage](traces.md#baggage).

## Export interval and temporality

Metrics are exported every `metrics.export_interval` seconds (default `60`). Unset, it falls back to `OTEL_METRIC_EXPORT_INTERVAL`, which the OpenTelemetry SDK expresses in milliseconds; the plugin divides by 1000 before use. The resolved value has a floor of 1 second: anything lower is raised to 1 second, with a warning.

The export runs on the plugin's own daemon thread, one per process, entirely separate from request handling and from RQ's job loop: a slow or unreachable Collector delays that thread's next collection, never a request or a job.

Temporality is cumulative by default, meaning a counter or histogram reports its running total since the process (or, after a fork, the forked child) started, not just the delta since the last export. `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` is honoured by the underlying OTLP metrics exporter if you need delta temporality instead. Because temporality is cumulative and series are keyed in part by `service.instance.id`, every process start, a recycled worker as well as a restarted container, reports under a new `service.instance.id` and so starts fresh series, since the identity carries a random part generated per process. See [Series per process](#series-per-process) below and [How it works, instance identity across restarts](../how-it-works.md#instance-identity-across-restarts).

## Job metrics

`netbox.rq.job.duration` and `netbox.rq.jobs` are recorded in the long-lived RQ worker process (the parent), wrapped around `execute_job`, not in the forked work-horse that actually runs the job: a work-horse's own meter provider is a no-op and exports nothing. The duration is the parent's wall-clock time around `execute_job`: preparing the job, the fork, the horse's run (including the horse's own flush of its logs and its job span before it exits), until the horse exits. With `SimpleWorker` (no fork), it is simply the job's run in the worker process itself.

These two are wrapped only when `rq.enabled` and `rq.patch_worker` are both true (and metrics are on): with `rq.patch_worker = False`, the `execute_job` wrap is never installed, so no job duration or outcome is recorded at all, and the fork that runs the job is never announced as a work-horse, so it keeps the `rqworker` role and, if metrics are enabled, builds a full metrics pipeline of its own instead of switching to a no-op provider; see [How it works, RQ work-horses](../how-it-works.md#rq-work-horses). `netbox.rq.queue.depth`, below, is registered independently and needs only `rq.enabled`, not `rq.patch_worker`.

`netbox.rq.job.outcome` is rq's job status read back after the job finishes: `finished`, `failed`, `stopped`, `canceled`, `retried` (rq's `Retry` put the job back on a queue or the scheduler), or `unknown` (the status lookup itself failed, the job's data is already gone, for example with `result_ttl=0`, or the status is something else). NetBox's `JobRunner` catches a script's own exception internally and marks the NetBox job record as errored without raising, so rq itself still reports that job as `finished`; `netbox.rq.job.outcome` reflects rq's status, not the NetBox job's.

`code.function.name` is the qualified name of the job's callable. rq only stores the method name for a bound method plus the instance separately, and every NetBox job is the `JobRunner` classmethod `handle`, so without qualification every NetBox job would look identical in this attribute. The worker parent deserialises each finished job's stored data (`func_name`, `instance`) to build `<module>.<Class>.handle`, for example `extras.jobs.ScriptJob.handle`; a plain function keeps rq's own `func_name` unqualified. A job whose data cannot be deserialised this way is recorded with `unknown` instead.

## Queue depth

`netbox.rq.queue.depth` is an observable gauge, read on the metrics export thread once per collection: one Redis `LLEN` per queue in django-rq's `RQ_QUEUES`, covering NetBox's own queues and any a plugin adds. If Redis is unreachable and the client has no socket timeout configured, that lookup can delay the collection it runs in; a queue whose depth cannot be read is skipped, with one warning per process.

Every RQ worker process reports the same global queue depths, and so does the rq scheduler process each worker forks (an unannounced fork keeps the `rqworker` role rather than becoming a work-horse, see [How it works](../how-it-works.md#processes-and-roles)), so a single worker host emits more than one series per queue. Aggregate with `max`, not `sum`, across `service.instance.id` values to get the true depth.

## Change counters

`netbox.object_changes` is incremented by the same receiver that produces [audit](audit.md) records, on commit, independently of whether audit records themselves are enabled: `metrics.change_counters` (default `True`) gates this counter on its own. A rolled back change is never counted. As with the other metrics above, this only happens in a process with a meter provider (web, or the RQ worker parent): a change committed inside a job actually runs in the forked work-horse, which has none, so it is not counted; see Known limitations.

## Runtime metrics

`metrics.runtime` (default `False`) adds `process.*` and `cpython.gc.*` from `opentelemetry-instrumentation-system-metrics`: `process.cpu.time`, `process.cpu.utilization`, `process.context_switches`, `process.memory.usage`, `process.memory.virtual`, `process.open_file_descriptor.count`, `process.thread.count`, `cpython.gc.collections`, `cpython.gc.collected_objects`, `cpython.gc.uncollectable_objects`. This list is process-level only. Host-wide `system.*` metrics are deliberately not collected: every NetBox process on a host would report the identical values for those, and host-level metrics belong to the Collector's own `hostmetrics` receiver instead. The deprecated `process.runtime.*` duplicates of these are not collected either. After a fork, the instrumentor is repointed at the child process so it reports the child's own CPU and memory, not its parent's.

## Series per process

Every web worker process and every RQ worker process exports its own series, identified by its own `service.instance.id` (`<hostname>-<pid>-<6 hex>`), not one series per host or per container. A recycled gunicorn or uWSGI worker (`--max-requests`, for example) is a new process, so it reports under a new identity even though the container never restarted, and a restarted container reports under a new identity too, even when it reuses the same hostname and PID. The number of series a backend holds over time therefore grows with every process start. See [How it works, forking web servers](../how-it-works.md#forking-web-servers) and [instance identity across restarts](../how-it-works.md#instance-identity-across-restarts) for what this means when aggregating or alerting on these series.

## Known limitations

- Outbound HTTP calls and object changes made inside a job (which runs in the forked work-horse, not the worker parent) or in a management command are not counted, since neither process has a meter provider.
- If a `MeterProvider` is already configured outside the plugin and reused (see [How it works](../how-it-works.md#an-sdk-configured-outside-the-plugin)), the web and worker processes keep recording into it and the plugin's allowlist does not apply to it; its own reader also keeps running, unused, in every forked work-horse.
- A job whose Redis hash is already gone by the time the worker parent looks it up (for example `result_ttl=0`) is counted with outcome `unknown`.

See the [configuration reference](../configuration.md#reference) for every `metrics.*` setting and its default, and [Data safety](../data-safety.md) for what is guaranteed never to leave the process.
