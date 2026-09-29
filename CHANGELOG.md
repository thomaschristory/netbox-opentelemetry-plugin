# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Dev stack: the `uwsgi` profile now runs uWSGI on a uwsgi-protocol socket behind nginx, as NetBox's `contrib/uwsgi.ini` does, instead of uWSGI's built-in HTTP router. The router closed every connection after the response without sending `Connection: close`, so a client that reused the connection could have its next request dropped. The e2e worker test no longer retries dropped logins and expects exactly one login record per login. It skips a web server only when the connection is refused and fails when the server answers with an error. nginx resolves the uWSGI container on each request, so rebuilding the stack does not leave it pointing at a stale address ([#2](https://github.com/thomaschristory/netbox-opentelemetry-plugin/issues/2)).

### Fixed

- `dev/scripts/check_dist.py` reports a malformed wheel filename, an unreadable sdist or wheel, or a wheel without `METADATA` as a one-line error and exits non-zero, instead of failing with a traceback ([#9](https://github.com/thomaschristory/netbox-opentelemetry-plugin/issues/9)).

## [0.2.2] - 2026-09-29

### Fixed

- Outbound HTTP calls from NetBox no longer forward a client's `baggage` header: inbound baggage is dropped and only the trace context is propagated, with traces on or metrics only. `OTEL_PROPAGATORS` still selects the trace-context format ([#1](https://github.com/thomaschristory/netbox-opentelemetry-plugin/issues/1)).
- `django.request` records for 4xx and 5xx responses, and other records Django writes after the request span has ended, now carry the request's trace id and span id ([#12](https://github.com/thomaschristory/netbox-opentelemetry-plugin/issues/12)).

## [0.2.1] - 2026-09-28

### Fixed

- The NetBox plugin page now shows the plugin's author; `author` and `author_email` are set on the plugin config ([#13](https://github.com/thomaschristory/netbox-opentelemetry-plugin/issues/13)).

## [0.2.0] - 2026-09-28

### Added

- `exporter.insecure_skip_verify`: send OTLP over HTTPS without verifying the Collector's certificate, for Collectors behind a self-signed certificate. HTTP only; rejected over gRPC and together with `exporter.certificate`. Logs a warning once per process and endpoint when on ([#10](https://github.com/thomaschristory/netbox-opentelemetry-plugin/issues/10)).

## [0.1.0] - 2026-09-27

### Added

- First release.
- Logs: NetBox's Python logs exported through the OTel Logs API, with allowlisted attributes and a feedback-loop filter so the plugin's own records and the exporters' internal logging are never exported.
- Audit records: one OpenTelemetry log record per committed `ObjectChange`, with optional, filtered field data.
- Traces: spans for Django requests, psycopg, redis and outbound `requests` calls, plus RQ job spans, with trace context propagated into enqueued jobs and redaction of headers, query strings and exception text.
- Metrics: HTTP server and client request durations, RQ job duration and count, RQ queue depth, an object change counter, optional process runtime metrics, all restricted to an allowlist of names and attributes.
- Fork and multi-process safety for gunicorn, uWSGI and Granian, so each worker process exports under its own identity.
- RQ work-horse flush, bounding how long a job's log lines, audit records and spans wait to be exported before the horse exits.
- Configuration through `PLUGINS_CONFIG` and the standard `OTEL_*` environment variables.
- Reuse of an OpenTelemetry SDK already configured outside the plugin (for example by `opentelemetry-instrument`).
- NetBox 4.7 support.

[Unreleased]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/releases/tag/v0.1.0
