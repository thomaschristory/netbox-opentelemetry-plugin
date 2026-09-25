# netbox-opentelemetry-plugin

NetBox plugin that exports NetBox telemetry over OTLP to an OpenTelemetry Collector, from inside the NetBox processes. Each signal is a module that can be enabled or disabled in configuration.

Status: under development. Currently implemented: application logs, with support for forking web servers and RQ work-horses.

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
