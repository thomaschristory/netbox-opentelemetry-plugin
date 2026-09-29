# How it works

## Processes and roles

Every NetBox process the plugin runs in is assigned one role, and what gets set up depends on the role:

| Role | What it is | Traces | Metrics |
|---|---|---|---|
| `web` | A process serving HTTP requests (Granian, gunicorn or uWSGI worker, or the `runserver` serving child) | Yes | Yes |
| `rqworker` | The long-lived `manage.py rqworker` process | Yes | Yes |
| `rq_horse` | The forked child that runs one job, then exits | Yes, rebuilt | No |
| `management` | Any other management command (`migrate`, `nbshell`, and so on) | No | No |
| `runserver_parent` | The `runserver` autoreloader's parent process | Nothing at all | Nothing at all |

Logs and audit records are set up in every role except `runserver_parent`, which installs nothing. Traces and metrics are built fresh only in `web` and `rqworker`: a short management command should not start a trace or metric exporter thread just to run for a few seconds, though it still gets a log exporter thread for its logs and audit records, same as every other role. A work-horse never goes through this initial setup itself; it is a fork of an already running `rqworker` process (see Forking web servers and RQ work-horses, below). It rebuilds its own working tracer provider after fork, since it needs one for the job span and for any instrumented database or Redis call made while that span is active, but its meter provider is switched to a no-op, so it records no metrics at all; job duration and outcome are recorded by the worker parent instead.

Roles are assigned by `bootstrap.detect_role`, which looks at `sys.argv` and the environment (SPEC 4.1): `manage.py rqworker` gives `rqworker`; `manage.py runserver` gives `runserver_parent`, unless `--noreload` was passed or `RUN_MAIN=true` is already set in the environment, in which case it is the reloader's serving child and gets `web`; any other `manage.py <command>` gives `management`; anything else (Granian, gunicorn, uWSGI) gives `web`. The `rq_horse` role is not assigned by `detect_role` at all: it is applied only to a child of an announced fork, when the RQ integration's wrap around `fork_work_horse` runs (see RQ work-horses, below). An unannounced fork of an `rqworker` process, such as the RQ scheduler forking its own child, keeps the `rqworker` role instead.

## Resource attributes

Every span, log record and metric point the plugin exports carries a Resource with:

- `service.name`, the configured `service_name`, which falls back to the environment variable `OTEL_SERVICE_NAME` if unset, and to `netbox` if neither is set.
- `service.version`, the running NetBox version.
- `service.instance.id`, `<hostname>-<pid>-<6 hex>`, for example `netbox-7f9c-1-a3f09c`. The last part is random, generated once per process and again whenever the process forks. See [instance identity across restarts](#instance-identity-across-restarts).
- `netbox.plugin.version`, this plugin's own version.
- `netbox.process.role`, the role above.
- Whatever is set in `resource_attributes` in `PLUGINS_CONFIG`.

The OpenTelemetry SDK also merges in `OTEL_RESOURCE_ATTRIBUTES` from the environment underneath all of this: the plugin's own keys and `resource_attributes` always win over an environment attribute of the same name. For example, with neither `service_name` nor `OTEL_SERVICE_NAME` set, `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod,service.name=x` adds `deployment.environment=prod` but leaves `service.name` as `netbox`, not `x`, since the plugin always sets `service.name` itself from its own resolved `service_name` (see above).

## Instance identity across restarts

A restarted container very often gets the same hostname and reuses low process IDs (often PID 1). Hostname and PID alone would therefore give a fresh container the same identity as the one it replaced, and a cumulative metric (a counter, or a histogram's counts) would restart from zero under the same series key. The OpenTelemetry semantic conventions require `service.instance.id` to be unique for each instance of a service, and recommend a random value.

The plugin therefore appends 6 random hex digits to `<hostname>-<pid>`. They come from the operating system's random source, are generated once when the process sets up and again in every forked child (gunicorn and uWSGI workers, RQ work-horses and any other fork), and are never written to disk. A restarted container, a recycled worker and a new work-horse each report a new identity. Hostname and PID stay in the id so that a process can still be traced back to its host and PID; the plugin does not set `host.name` or `process.pid`.

The cost is series churn: every process start begins new metric series, including a container restart that previously kept the same identity. Over time a backend sees one set of series per process that ever ran, and for a short while after a restart both the old and the new series are present until the backend marks the old ones stale. Budget active series for the number of processes, not containers, and aggregate across `service.instance.id` in dashboards and alerts rather than select one identity. There is no setting to go back to the old `<hostname>-<pid>` format.

## Forking web servers

Every worker process forked from a preloaded parent (gunicorn with `preload_app`, uWSGI's `master` mode) rebuilds its Resource, a fresh exporter and a new provider right after the fork, through Python's `os.register_at_fork` hooks (or, under uWSGI, the chained `uwsgi.post_fork_hook`). The parent's own provider and exporter connection are never touched from the child: they are simply dropped and left running unused, since any lock inside them (a `requests.Session`, a gRPC channel, an SDK batch processor's own lock) was copied by `fork()` in whatever state it happened to be in, and touching or shutting down an object in that state can deadlock the child. The result is that each worker process exports under its own `service.instance.id`, distinct from its parent's and from every sibling worker's.

This means the data from a fleet of forked workers arrives as one series per worker process, not one series per host or per container. A manual check during development (`--max-requests=20`, recycling workers quickly) saw `http.server.request.duration` reported under 6 distinct worker identities for gunicorn and 5 for uWSGI over the check's duration, because every worker gunicorn or uWSGI recycles is a new process with a new PID, and therefore a new identity, even though the container itself never restarted. Any aggregation across a worker fleet (dashboards, alerts) needs to sum or average across these per-process series rather than expect a single series per container.

## RQ work-horses

Each RQ job runs inside a forked work-horse process. After the job finishes, whether it succeeded, failed or hit its own `job.timeout` (rq handles that inside `perform_job` itself), the plugin flushes the horse's log provider and its tracer provider in parallel, waiting at most `rq.flush_timeout` seconds (default 5) in total. The horse then exits with `os._exit`, which skips `atexit` entirely, so this bounded flush is the horse's only chance to export what it buffered: the job's log lines and audit records, and its job span together with any spans from instrumented calls made while that span was active.

Several paths end a horse without giving it that chance at all, because they SIGKILL it (or its process group) instead of letting `perform_job` return normally:

- The job's working time exceeds `job.timeout + 60` seconds.
- A stop-job command is sent to the worker.
- A kill-horse command is sent to the worker.
- A cold shutdown of the worker (a second SIGINT or SIGTERM).

Whatever the horse had buffered and not yet flushed at that point is lost. Keeping `rq.flush_timeout` well below that 60 second margin reduces how much a hung job can leave stranded, though nothing enforces that relationship; it is a setting to choose deliberately, not a default the plugin can validate for you.

Because the worker only picks up its next job once the current horse has exited, a slow flush delays the worker's next job rather than the running job's own completion. During a Collector outage the exporter cannot succeed, so every horse pays the full `flush_timeout` before it exits, capping that worker at roughly one job per `flush_timeout` for as long as the outage lasts.

Setting `rq.patch_worker = False` removes this flush wrap entirely (and the `fork_work_horse` wrap that labels the child `rq_horse`, and the wrap around `perform_job` that opens the job's span, and the `execute_job` wraps that record job duration and outcome): with it off, a work-horse keeps the `rqworker` role, gets no job span, whatever it buffers before exiting is never flushed, and job duration and outcome are no longer recorded either. The queue depth gauge is unaffected, since it is registered before `rq.patch_worker` is checked.

This also changes what happens to metrics in the forked child itself. Normally a work-horse is labelled `rq_horse`, a role metrics are not set up for, so its meter provider is switched to a no-op after fork and it exports nothing. With `patch_worker = False`, the fork is never announced, so the forked child keeps the `rqworker` role, which metrics *are* set up for: if metrics are enabled, `bootstrap` rebuilds this now-unlabelled fork a full metrics pipeline of its own, resource and all. Should that fork happen to run longer than one `metrics.export_interval` before exiting, for example a slow job, it can itself export metrics (such as the queue depth gauge) under its own freshly built `service.instance.id`, indistinguishable in the data from a second, genuine `rqworker` process.

## An SDK configured outside the plugin

If a global TracerProvider, MeterProvider or LoggerProvider is already set before NetBox starts, for example by running the process under `opentelemetry-instrument`, the plugin detects it and reuses it instead of building its own. Instrumentors that report themselves as already applied are left alone rather than instrumented a second time.

Reusing an externally configured provider means several things the plugin normally does for its own providers do not apply:

- Span redaction (stripping query strings and known-sensitive attributes) and the filter that drops parentless CLIENT spans from psycopg and redis (the noise from queries and commands run outside a request or a job) both live in the sampler and span processor the plugin builds itself; an externally configured TracerProvider does not get them.
- The metric allowlist, which otherwise restricts what the plugin's own MeterProvider exports to a known set of instruments, is not applied to an externally configured MeterProvider.
- The larger log queue the plugin sizes for audit bursts is set only on a LoggerProvider it builds itself; an externally configured LoggerProvider keeps whatever queue size it was already given.

See [Limitations](limitations.md) for the fuller list of what an externally configured SDK changes.
