# Logs

With `logs.enabled` (default `True`), the plugin attaches a logging handler directly to the Python loggers listed in `logs.loggers`, so their records are exported over OTLP as well as going wherever NetBox's own logging configuration already sends them (normally stdout).

## Which loggers

`logs.loggers` defaults to `["netbox", "django", "rq"]`. For each name, the plugin calls `logging.getLogger(name).addHandler(...)`, at `logs.level` (default `INFO`). Because Python's logging propagates a record up through parent loggers, this also captures records from a child logger under one of those names, for example `django.request` or `django.security.csrf`, as long as the record passes that child logger's own effective level first.

## Getting INFO records

NetBox's default `LOGGING` setting is an empty dict, so `netbox`, `django` and `rq` have no level of their own and inherit the root logger's level, which is `WARNING`. `logs.level = "INFO"` only decides what the handler accepts once a record reaches it; it does not lower the logger's own effective level. With the defaults, an INFO record is filtered out by the logger itself before the handler ever sees it, so nothing below `WARNING` is exported. (NetBox's own configuration docs describe INFO records reaching the console by default; the code disagrees, and this plugin follows the code.)

Two ways to get INFO records exported:

- Set `logs.set_logger_levels = True`. This lowers the effective level of each logger in `logs.loggers` to `logs.level`, but only if that logger currently has no level of its own or a higher one; a level an operator already set explicitly, lower than `logs.level`, is left alone.

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
- **Attributes**, built only from an allowlist, never from arbitrary `extra=` fields: `code.file.path`, `code.function.name`, `code.line.number`, `logger.name`, `thread.name`; `exception.type`, `exception.message` and `exception.stacktrace` when the record carries exception info.
- **`extra=` fields are not exported.** The handler is implemented to build its attributes only from the allowlist above, so a call such as `logger.info("...", extra={"exception.type": "Fake"})` cannot add data of its own, or spoof one of the allowlisted names.
- Inside an active span, a record carries that span's trace id and span id automatically.

## Instrumentation scope

Each record is emitted through the OTel Logger obtained for the *originating* Python logger's own name, not a single fixed scope for the whole module: a record from `django.request` is emitted under the instrumentation scope `django.request`, one from `netbox` under `netbox`, and so on.

## Feedback loop

Records from `netbox_opentelemetry_plugin`, `opentelemetry`, `urllib3` and `grpc` (and any logger nested under one of those names) are always excluded from export, regardless of `logs.loggers`. This stops a record produced by the export path itself, such as a warning logged internally by the OTel SDK or by the HTTP or gRPC client the exporter uses, from feeding back into another export attempt. The plugin's own logger (`netbox_opentelemetry_plugin`) only ever writes to stdout, through NetBox's normal logging configuration.

## Background jobs

Log lines written inside an RQ job or custom script run in the work-horse process. They are flushed, together with that horse's tracer provider, before the horse exits, waiting at most `rq.flush_timeout` seconds. See [How it works](../how-it-works.md#rq-work-horses) for the paths that SIGKILL a horse instead of letting it flush.

## Known limitations

- `exception.message` on a log record is not scrubbed for URL query strings. Span attributes, status descriptions and span exception events are (see [Traces](traces.md)); log records are not.

See the [configuration reference](../configuration.md#reference) for every `logs.*` setting and its default.
