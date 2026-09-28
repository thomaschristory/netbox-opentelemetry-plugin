# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/thomaschristory/netbox-opentelemetry-plugin/releases/tag/v0.1.0
