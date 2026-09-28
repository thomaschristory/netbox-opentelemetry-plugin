# Logs

With `logs.enabled` (default `True`), the plugin attaches a logging handler directly to the Python loggers listed in `logs.loggers`, so their records are exported over OTLP as well as continuing to go wherever NetBox's own logging configuration already sends them (stderr by default).

## Which loggers

`logs.loggers` defaults to `["netbox", "django", "rq"]`. For each name, the plugin calls `logging.getLogger(name).addHandler(...)`, at `logs.level` (default `INFO`). Because Python's logging propagates a record up through parent loggers, this also captures records from a child logger under one of those names, for example `django.request` or `django.security.csrf`, as long as the record passes that child logger's own effective level first.

## Getting INFO records

`logs.level = "INFO"` only decides what the handler accepts once a record reaches it; it does not change any logger's own effective level. NetBox's default `LOGGING` setting is an empty dict, so `netbox` (and anything under it, such as `netbox.dcim.api.views`) has no level of its own and inherits the root logger's level, `WARNING`: with the defaults, an INFO record from `netbox.*` is filtered out by the logger itself before the handler ever sees it. (NetBox's own configuration docs describe INFO records reaching the console by default; the code disagrees, and this plugin follows the code.)

`django` and `rq.worker` are not in the same situation. Django's `configure_logging` always applies its own built-in `DEFAULT_LOGGING` first, which sets the `django` logger to `INFO`; because NetBox's `LOGGING` is an empty dict, the step that would apply an operator's own `LOGGING` on top of that default never runs. Separately, the RQ worker process (`manage.py rqworker`) calls rq's `setup_loghandlers` at its default verbosity, which sets `rq.worker` to `INFO`; a record from `rq.worker` still reaches the handler the plugin attaches to `rq`, since it propagates up to that ancestor logger. So with the plugin's defaults, an INFO record from `django` or `rq.worker` is already exported. `logs.set_logger_levels` and a custom `LOGGING` mainly matter for getting `netbox.*` records below `WARNING`.

Two ways to get `netbox.*` INFO records exported:

- Set `logs.set_logger_levels = True`. This lowers the effective level of each logger in `logs.loggers` to `logs.level`, but only if that logger currently has no level of its own or a higher one; a level an operator already set explicitly, lower than `logs.level`, is left alone. `django` and `rq.worker` are already at `INFO` by the time this runs, so in practice this only changes `netbox`.

  ```python
  PLUGINS_CONFIG = {
      "netbox_opentelemetry_plugin": {
          "exporter": {"endpoint": "http://collector:4318"},
          "logs": {"set_logger_levels": True},
      },
  }
  ```

- Configure `LOGGING` in `configuration.py` yourself, to whatever level and handlers you want, independently of this plugin:

  ```python
  LOGGING = {
      "version": 1,
      "disable_existing_loggers": False,
      "loggers": {
          "netbox": {"level": "INFO"},
      },
  }
  ```

## Record fields

- **Body**: the formatted message (`record.getMessage()`, or the result of the handler's formatter if one is set).
- **Severity number**: mapped from the Python level number. A level that falls between two standard levels maps to the lower one, for example `15` (between `DEBUG` at `10` and `INFO` at `20`) gives `DEBUG`.
- **Severity text**: the Python level name, except `WARN` for `WARNING` and `FATAL` for `CRITICAL` (the spelling the OTel logs data model uses for those two).
- **Attributes**, built only from an allowlist, never from arbitrary `extra=` fields: `code.file.path`, `code.function.name`, `code.line.number` and `logger.name` are always present; `thread.name` is present when the record's thread has a name; when the record carries exception info, `exception.type` and `exception.stacktrace` are added, and `exception.message` too, but only when the exception itself has a message (its first argument).
- **`extra=` fields are not exported.** The handler is implemented to build its attributes only from the allowlist above, so a call such as `logger.info("...", extra={"exception.type": "Fake"})` cannot add data of its own, or spoof one of the allowlisted names.
- Inside an active span, a record carries that span's trace id and span id automatically.
- A record written after the request's span has ended, when no span is current, carries the trace id and span id of that request's SERVER span, taken from the request the record references (its `extra={"request": ...}`). Django writes such records itself: the `Not Found`, `Forbidden`, `Bad Request` and `Internal Server Error` lines of `django.request` are logged once the middleware chain has returned, after the span has ended. This includes an API 500 that NetBox's exception handling turns into a response. `RequestSpanMiddleware` records the span context on the request before the view runs; the request object itself is only read for these ids and is never exported. The record's timestamp can be slightly after the span's end time.
- With traces off (including metrics-only instrumentation), or for a URL in `traces.excluded_urls`, there is no span, so such a record has no trace id.

## Instrumentation scope

Each record is emitted through the OTel Logger obtained for the *originating* Python logger's own name, not a single fixed scope for the whole module: a record from `django.request` is emitted under the instrumentation scope `django.request`, one from `netbox` under `netbox`, and so on.

## Feedback loop

Records from `netbox_opentelemetry_plugin`, `opentelemetry`, `urllib3` and `grpc` (and any logger nested under one of those names) are always excluded from export, regardless of `logs.loggers`. This stops a record produced by the export path itself, such as a warning logged internally by the OTel SDK or by the HTTP or gRPC client the exporter uses, from feeding back into another export attempt. The plugin's own logger (`netbox_opentelemetry_plugin`) is never exported and goes wherever NetBox's logging configuration sends it (stderr by default).

## Background jobs

Log lines written inside an RQ job or custom script run in the work-horse process. They are flushed, together with that horse's tracer provider, before the horse exits, waiting at most `rq.flush_timeout` seconds. See [How it works](../how-it-works.md#rq-work-horses) for the paths that SIGKILL a horse instead of letting it flush.

## Known limitations

- `exception.message` on a log record is not scrubbed for URL query strings. Span attributes, status descriptions and span exception events are (see [Traces](traces.md)); log records are not.
- A 4xx or 5xx response returned, without raising, by a middleware that runs before the plugin's own `RequestSpanMiddleware` (for example another plugin's middleware listed earlier in `PLUGINS`) produces a `django.request` record without a trace id: the request never reached the point where its span context is recorded. Stock NetBox 4.7 middleware has no such path.

See the [configuration reference](../configuration.md#reference) for every `logs.*` setting and its default.
