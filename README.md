# netbox-opentelemetry-plugin

NetBox plugin that exports NetBox telemetry over OTLP to an OpenTelemetry Collector, from inside the NetBox processes. Each signal is a module that can be enabled or disabled in configuration.

Status: under development. Currently implemented: application logs and audit records, with support for forking web servers and RQ work-horses.

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
