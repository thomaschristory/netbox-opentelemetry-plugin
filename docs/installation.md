# Installation

## Requirements

- NetBox 4.7.x.
- Python 3.12 to 3.14 (whatever NetBox itself uses).
- An OpenTelemetry Collector reachable from every NetBox process over OTLP, either HTTP (default port 4318) or gRPC (default port 4317).

Installing the package pulls in its OpenTelemetry dependencies: the API, SDK and OTLP exporters (HTTP and gRPC), pinned to `~=1.44.0`, and the Django, psycopg, redis, requests and system-metrics instrumentation packages, pinned to `==0.65b0`. The system-metrics instrumentation brings in `psutil`. The package adds no NetBox model, no database migration and no UI of its own; nothing in NetBox's own database or admin pages changes.

## Enable the plugin

Add the plugin to `PLUGINS` and configure it under `PLUGINS_CONFIG` in `configuration.py`:

```python
PLUGINS = ["netbox_opentelemetry_plugin"]
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://otel-collector:4318"},
    },
}
```

The endpoint can also be set with the environment variable `OTEL_EXPORTER_OTLP_ENDPOINT` instead of `exporter.endpoint`, which is useful when the same image runs in several environments. See [Configuration](configuration.md) for every setting.

Since the plugin has no migration, running `manage.py migrate` is not required for it, and since it has no static files, neither is `manage.py collectstatic`. Both are harmless to run anyway, and NetBox's own upgrade script (`upgrade.sh`) runs both regardless, so there is nothing special to skip.

## netbox-docker (and the Helm chart)

netbox-docker images do not include plugins; build a custom image on top of the upstream one, following the pattern documented in the netbox-docker wiki ("Using Netbox Plugins"). The venv inside the netbox-docker image has no `pip`, only `uv`:

```dockerfile
FROM netboxcommunity/netbox:v4.7.1
COPY ./plugin_requirements.txt /opt/netbox/
RUN /usr/local/bin/uv pip install -r /opt/netbox/plugin_requirements.txt
```

with `plugin_requirements.txt` containing:

```text
netbox-opentelemetry-plugin==0.1.0
```

`PLUGINS` and `PLUGINS_CONFIG` go in a file such as `/etc/netbox/config/plugins.py`; netbox-docker loads every `*.py` file it finds in `/etc/netbox/config/`. The endpoint can instead come from the container environment, `OTEL_EXPORTER_OTLP_ENDPOINT`, which avoids baking it into the config file.

The web container runs NetBox behind Granian (`GRANIAN_WORKERS`, 4 worker processes by default). Nothing else needs configuring: each Granian worker is started with spawn, so every worker process runs NetBox's own startup, including the plugin's `ready()`, itself; there is no parent process whose state gets forked into it. The worker container (`manage.py rqworker`) uses the same image and the same configuration files.

For the Helm chart (`netbox-community/netbox-chart`), build and reference the same custom image, then set the plugin through the chart's own values. In the chart's `charts/netbox/values.yaml`, the plugin list is the top-level `plugins` key (a list of plugin names) and its settings are the top-level `pluginsConfig` key (a dict keyed by plugin name):

```yaml
image:
  repository: your-registry/netbox-custom
  tag: v4.7.1-otel

plugins:
  - netbox_opentelemetry_plugin

pluginsConfig:
  netbox_opentelemetry_plugin:
    exporter:
      endpoint: http://otel-collector:4318
```

Setting `OTEL_*` environment variables on the NetBox and worker Deployments (through the chart's environment variable values) is the other option, instead of `pluginsConfig`.

## Bare metal, gunicorn

Install the package into NetBox's virtual environment:

```bash
source /opt/netbox/venv/bin/activate
pip install netbox-opentelemetry-plugin
```

Add it to `local_requirements.txt` too, so `upgrade.sh` reinstalls it on every future upgrade.

NetBox's `contrib/gunicorn.py` (5 workers, 3 threads each, `preload_app` not set) needs no change for the plugin. Without `preload_app`, gunicorn forks each worker before NetBox has loaded the app, so every worker runs the plugin's own startup independently, the same as under Granian. With `preload_app = True`, the app (and the plugin) is loaded once in the master and then forked into every worker; the plugin's at-fork hooks rebuild its exporters and identity in each forked worker in that case. Either way there is nothing to configure. Restart the `netbox` and `netbox-rq` systemd services to pick up the change.

## Bare metal, uWSGI

NetBox's `contrib/uwsgi.ini` (`master = true`, no `lazy-apps`) preloads the app once and forks every worker from it, the same preload-then-fork pattern as gunicorn with `preload_app`. The plugin chains onto `uwsgi.post_fork_hook` to rebuild itself in each worker, so uWSGI's `py-call-osafterfork` option is not required.

With the classic `uwsgi` binary (not `pyuwsgi`), background threads such as the exporters' batch processing threads do not run unless `enable-threads = true` (or `threads`) is set in the uWSGI configuration. Without it, nothing is exported, and the plugin logs one warning naming the fix:

```text
OpenTelemetry: uWSGI is running without thread support, so the exporter's background thread
cannot run and nothing will be exported. Set `enable-threads = true` in the uWSGI configuration.
```

`pyuwsgi`, the package installed from PyPI, always has thread support built in and is unaffected by this.

Restart the `netbox` and `netbox-rq` systemd services after installing.

## `manage.py runserver`

The development server's autoreloader parent process only watches files for changes; it never installs the plugin. The child process it spawns to actually serve requests does. A reload starts a fresh child, which goes through the plugin's startup again from scratch.

## RQ worker

`manage.py rqworker` runs the background job worker. netbox-docker's worker container starts it with no queue arguments, so NetBox falls back to the default queue set `high default low` and logs its own warning about that; the bare metal `contrib/netbox-rq.service` unit passes `rqworker high default low` explicitly.

The plugin wraps rq's internals in place; no change to `RQ["WORKER_CLASS"]` is needed or supported (NetBox itself warns if that setting names anything other than its own worker class). Each job runs inside a forked work-horse process, and the log and audit records produced by that job are flushed before the horse exits, bounded by `rq.flush_timeout` (default 5 seconds). See [How it works](how-it-works.md) for the detail of what that flush covers and where it can be lost.

## Management commands

Commands other than `rqworker` (`migrate`, `nbshell`, custom management commands, and so on) export logs and audit records only. No traces and no metrics are set up for them, so a short-lived command does not start a trace or metric exporter thread; it still gets a log exporter thread for whatever it logs or writes as an audit record while it runs.

## Checking it works

The plugin logs its own messages to the `netbox_opentelemetry_plugin` logger, on stdout, and never exports them. If a signal has no endpoint configured, that module logs one warning and disables itself; the rest of NetBox, and the rest of the plugin, keep running.

In the Collector (a `debug` exporter is enough to start with), look for the resource attributes `service.name=netbox`, `netbox.process.role` (`web`, `rqworker`, `rq_horse` or `management`) and `service.instance.id`, to confirm data is arriving and to tell processes apart.

By default NetBox's own `netbox.*` loggers are at `WARNING`, so INFO-level records from the `netbox` logger itself are not exported until you either set `logs.set_logger_levels = True` or configure NetBox's `LOGGING` setting. See [Logs](signals/logs.md) for both options.
