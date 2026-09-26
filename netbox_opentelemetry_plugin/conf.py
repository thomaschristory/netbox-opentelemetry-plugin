"""Resolve plugin settings from PLUGINS_CONFIG and OTEL_* environment variables.

Precedence for every setting: explicit PLUGINS_CONFIG value, then the signal specific
OTEL_* variable, then the generic OTEL_* variable, then the default. Error messages never
contain header values.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

PROTOCOLS = ("http/protobuf", "grpc")
REDACTED = "***"

TRACE_SAMPLERS = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)
INSTRUMENTATIONS = ("django", "psycopg", "redis", "requests")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "exporter": {
        "endpoint": None,
        "protocol": "http/protobuf",
        "headers": {},
        "timeout": 10,
        "insecure": None,
        "certificate": None,
    },
    "service_name": "netbox",
    "resource_attributes": {},
    "logs": {
        "enabled": True,
        "endpoint": None,
        "loggers": ["netbox", "django", "rq"],
        "level": "INFO",
        "set_logger_levels": False,
    },
    "audit": {
        "enabled": True,
        "include_data": False,
        "exclude_fields": ["password", "secret", "token", "key"],
    },
    "traces": {
        "enabled": False,
        "endpoint": None,
        "sampler": "parentbased_traceidratio",
        "sampler_arg": 1.0,
        "instrument": ["django", "psycopg", "redis", "requests"],
        "excluded_urls": ["/static/", "/metrics", "/api/status/"],
    },
    "metrics": {
        "enabled": False,
        "endpoint": None,
        "export_interval": 60,
        "change_counters": True,
        "runtime": False,
    },
    "rq": {
        "enabled": True,
        "patch_worker": True,
        "propagate_context": True,
        "flush_timeout": 5,
    },
}

# Sections whose keys are chosen by the operator; unknown-key warnings do not apply inside them.
_FREEFORM = {("exporter", "headers"), ("resource_attributes",)}


class ConfigError(ValueError):
    """Invalid configuration value. Messages never include header values."""


@dataclass(frozen=True, repr=False)
class ExporterConfig:
    endpoint: str
    protocol: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout: float = 10.0
    insecure: bool | None = None
    certificate: str | None = None

    def redacted(self) -> dict[str, Any]:
        return {
            "endpoint": _redact_userinfo(self.endpoint),
            "protocol": self.protocol,
            "headers": {key: REDACTED for key in self.headers},
            "timeout": self.timeout,
            "insecure": self.insecure,
            "certificate": self.certificate,
        }

    def __repr__(self) -> str:
        return f"ExporterConfig({self.redacted()!r})"


def _redact_userinfo(endpoint: str) -> str:
    """Strip credentials from a URL's authority, e.g. https://user:pass@host -> https://***@host.

    Works on the raw netloc so a malformed port or an IPv6 literal never raises or loses brackets.
    A URL that cannot be parsed at all is masked completely.
    """
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return REDACTED
    _, sep, hostport = parts.netloc.rpartition("@")
    if not sep:
        return endpoint
    return urlunsplit((parts.scheme, f"{REDACTED}@{hostport}", parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class LogsConfig:
    enabled: bool
    exporter: ExporterConfig | None = None
    loggers: tuple[str, ...] = ()
    level: int = logging.INFO
    set_logger_levels: bool = False

    def redacted(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "exporter": self.exporter.redacted() if self.exporter else None,
            "loggers": list(self.loggers),
            "level": logging.getLevelName(self.level),
            "set_logger_levels": self.set_logger_levels,
        }


LOGS_OFF = LogsConfig(enabled=False)


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = True
    include_data: bool = False
    exclude_fields: tuple[str, ...] = ("password", "secret", "token", "key")

    def redacted(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "include_data": self.include_data,
            "exclude_fields": list(self.exclude_fields),
        }


AUDIT_OFF = AuditConfig(enabled=False)


@dataclass(frozen=True)
class TracesConfig:
    enabled: bool
    exporter: ExporterConfig | None = None
    sampler: str = "parentbased_traceidratio"
    sampler_arg: float = 1.0
    instrument: tuple[str, ...] = INSTRUMENTATIONS
    excluded_urls: tuple[str, ...] = ("/static/", "/metrics", "/api/status/")

    def redacted(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "exporter": self.exporter.redacted() if self.exporter else None,
            "sampler": self.sampler,
            "sampler_arg": self.sampler_arg,
            "instrument": list(self.instrument),
            "excluded_urls": list(self.excluded_urls),
        }


TRACES_OFF = TracesConfig(enabled=False)


@dataclass(frozen=True)
class RqConfig:
    enabled: bool = True
    patch_worker: bool = True
    flush_timeout: float = 5.0
    propagate_context: bool = True

    def redacted(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "patch_worker": self.patch_worker,
            "flush_timeout": self.flush_timeout,
            "propagate_context": self.propagate_context,
        }


RQ_OFF = RqConfig(enabled=False)


@dataclass(frozen=True)
class Settings:
    enabled: bool
    service_name: str = "netbox"
    resource_attributes: Mapping[str, str | bool | int | float] = field(default_factory=dict)
    logs: LogsConfig = LOGS_OFF
    audit: AuditConfig = AUDIT_OFF
    log_exporter: ExporterConfig | None = None
    traces: TracesConfig = TRACES_OFF
    rq: RqConfig = field(default_factory=RqConfig)
    warnings: tuple[str, ...] = ()

    def redacted(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "service_name": self.service_name,
            "resource_attributes": dict(self.resource_attributes),
            "logs": self.logs.redacted(),
            "audit": self.audit.redacted(),
            "log_exporter": self.log_exporter.redacted() if self.log_exporter else None,
            "traces": self.traces.redacted(),
            "rq": self.rq.redacted(),
        }

    def exporters(self) -> tuple[ExporterConfig, ...]:
        """Every resolved exporter; used to redact their endpoints and header values from messages."""
        return tuple(cfg for cfg in (self.log_exporter, self.traces.exporter) if cfg is not None)


def resolve(user: Mapping[str, Any] | None, env: Mapping[str, str]) -> Settings:
    if user is None:
        user = {}
    if not isinstance(user, Mapping):
        return Settings(enabled=False, warnings=("plugin disabled: PLUGINS_CONFIG entry must be a dict",))

    warnings: list[str] = []
    _check_unknown_keys(user, DEFAULTS, (), warnings)

    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return Settings(enabled=False, warnings=tuple(warnings))

    try:
        enabled = _typed(user.get("enabled", DEFAULTS["enabled"]), bool, "enabled")
        exporter_section = _section(user, "exporter")
        service_name = _typed(
            _pick(user, "service_name", env, ("OTEL_SERVICE_NAME",), DEFAULTS["service_name"], _parse_str),
            str,
            "service_name",
        )
        resource_attributes = _resource_attributes(user.get("resource_attributes"))
    except ConfigError as exc:
        warnings.append(f"plugin disabled: {exc}")
        return Settings(enabled=False, warnings=tuple(warnings))

    if not enabled:
        return Settings(enabled=False, warnings=tuple(warnings))

    try:
        logs_section = _section(user, "logs")
    except ConfigError as exc:
        warnings.append(f"logs disabled: {exc}")
        logs_section, logs = {}, LOGS_OFF
    else:
        try:
            logs = _resolve_logs(logs_section)
        except ConfigError as exc:
            warnings.append(f"logs disabled: {exc}")
            logs = LOGS_OFF

    try:
        audit = _resolve_audit(_section(user, "audit"))
    except ConfigError as exc:
        warnings.append(f"audit disabled: {exc}")
        audit = AUDIT_OFF

    log_exporter = None
    users = [name for name, cfg in (("logs", logs), ("audit", audit)) if cfg.enabled]
    if users:
        try:
            log_exporter = resolve_exporter("logs", logs_section, exporter_section, env)
        except ConfigError as exc:
            warnings.append(f"{' and '.join(users)} disabled: {exc}")
            logs, audit = LOGS_OFF, AUDIT_OFF
    if log_exporter is not None and logs.enabled:
        logs = replace(logs, exporter=log_exporter)

    try:
        traces = _resolve_traces(_section(user, "traces"), exporter_section, env)
    except ConfigError as exc:
        warnings.append(f"traces disabled: {exc}")
        traces = TRACES_OFF

    try:
        rq = _resolve_rq(_section(user, "rq"))
    except ConfigError as exc:
        warnings.append(f"rq disabled: {exc}")
        rq = RQ_OFF

    return Settings(
        enabled=True,
        service_name=service_name,
        resource_attributes=resource_attributes,
        logs=logs,
        audit=audit,
        log_exporter=log_exporter,
        traces=traces,
        rq=rq,
        warnings=tuple(warnings),
    )


def resolve_exporter(
    signal: str,
    signal_section: Mapping[str, Any],
    exporter_section: Mapping[str, Any],
    env: Mapping[str, str],
) -> ExporterConfig:
    upper = signal.upper()

    def env_names(suffix: str) -> tuple[str, str]:
        return (f"OTEL_EXPORTER_OTLP_{upper}_{suffix}", f"OTEL_EXPORTER_OTLP_{suffix}")

    defaults = DEFAULTS["exporter"]
    protocol = _pick(exporter_section, "protocol", env, env_names("PROTOCOL"), defaults["protocol"], _parse_str)
    if protocol not in PROTOCOLS:
        raise ConfigError(f"unsupported protocol {protocol!r}, expected one of: {', '.join(PROTOCOLS)}")

    endpoint = _endpoint(signal, protocol, signal_section, exporter_section, env)

    headers = _pick(exporter_section, "headers", env, env_names("HEADERS"), {}, _parse_headers)
    if not isinstance(headers, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise ConfigError("exporter.headers must be a dict of strings")

    timeout = _pick(exporter_section, "timeout", env, env_names("TIMEOUT"), defaults["timeout"], _parse_float)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0 or not math.isfinite(timeout):
        raise ConfigError("exporter.timeout must be a positive number of seconds")

    insecure = _pick(exporter_section, "insecure", env, env_names("INSECURE"), defaults["insecure"], _parse_bool)
    if insecure is not None:
        insecure = _typed(insecure, bool, "exporter.insecure")
    certificate = _pick(exporter_section, "certificate", env, env_names("CERTIFICATE"), None, _parse_str)
    if certificate is not None and not isinstance(certificate, str):
        raise ConfigError("exporter.certificate must be a file path")

    return ExporterConfig(
        endpoint=endpoint,
        protocol=protocol,
        headers=dict(headers),
        timeout=float(timeout),
        insecure=insecure,
        certificate=certificate,
    )


def _resolve_logs(section: Mapping[str, Any]) -> LogsConfig:
    defaults = DEFAULTS["logs"]
    if not _typed(section.get("enabled", defaults["enabled"]), bool, "logs.enabled"):
        return LOGS_OFF
    loggers = section.get("loggers", defaults["loggers"])
    if not isinstance(loggers, list | tuple) or not all(isinstance(name, str) for name in loggers):
        raise ConfigError("logs.loggers must be a list of logger names")
    level = _level(section.get("level", defaults["level"]), "logs.level")
    set_levels = _typed(section.get("set_logger_levels", defaults["set_logger_levels"]), bool, "logs.set_logger_levels")
    return LogsConfig(
        enabled=True,
        exporter=None,
        loggers=tuple(loggers),
        level=level,
        set_logger_levels=set_levels,
    )


def _resolve_audit(section: Mapping[str, Any]) -> AuditConfig:
    defaults = DEFAULTS["audit"]
    if not _typed(section.get("enabled", defaults["enabled"]), bool, "audit.enabled"):
        return AUDIT_OFF
    include_data = _typed(section.get("include_data", defaults["include_data"]), bool, "audit.include_data")
    exclude = section.get("exclude_fields", defaults["exclude_fields"])
    if not isinstance(exclude, list | tuple) or not all(isinstance(name, str) for name in exclude):
        raise ConfigError("audit.exclude_fields must be a list of field name fragments")
    return AuditConfig(enabled=True, include_data=include_data, exclude_fields=tuple(exclude))


def _resolve_traces(
    section: Mapping[str, Any], exporter_section: Mapping[str, Any], env: Mapping[str, str]
) -> TracesConfig:
    defaults = DEFAULTS["traces"]
    if not _typed(section.get("enabled", defaults["enabled"]), bool, "traces.enabled"):
        return TRACES_OFF
    sampler = _pick(section, "sampler", env, ("OTEL_TRACES_SAMPLER",), defaults["sampler"], _parse_str)
    if not isinstance(sampler, str) or sampler.strip().lower() not in TRACE_SAMPLERS:
        raise ConfigError(f"traces.sampler must be one of: {', '.join(TRACE_SAMPLERS)}")
    sampler_arg = _pick(
        section, "sampler_arg", env, ("OTEL_TRACES_SAMPLER_ARG",), defaults["sampler_arg"], _parse_float
    )
    if (
        isinstance(sampler_arg, bool)
        or not isinstance(sampler_arg, int | float)
        or not math.isfinite(sampler_arg)
        or not 0 <= sampler_arg <= 1
    ):
        raise ConfigError("traces.sampler_arg must be a number between 0 and 1")
    instrument = section.get("instrument", defaults["instrument"])
    if not isinstance(instrument, list | tuple) or not all(isinstance(name, str) for name in instrument):
        raise ConfigError("traces.instrument must be a list of instrumentation names")
    unknown = [name for name in instrument if name not in INSTRUMENTATIONS]
    if unknown:
        raise ConfigError(f"traces.instrument has unknown entries {unknown}; known: {', '.join(INSTRUMENTATIONS)}")
    excluded = section.get("excluded_urls", defaults["excluded_urls"])
    if not isinstance(excluded, list | tuple) or not all(isinstance(url, str) for url in excluded):
        raise ConfigError("traces.excluded_urls must be a list of URL patterns")
    if any("," in url for url in excluded):
        raise ConfigError("traces.excluded_urls entries must not contain a comma")
    exporter = resolve_exporter("traces", section, exporter_section, env)
    return TracesConfig(
        enabled=True,
        exporter=exporter,
        sampler=sampler.strip().lower(),
        sampler_arg=float(sampler_arg),
        instrument=tuple(dict.fromkeys(instrument)),
        excluded_urls=tuple(excluded),
    )


def _resolve_rq(section: Mapping[str, Any]) -> RqConfig:
    defaults = DEFAULTS["rq"]
    enabled = _typed(section.get("enabled", defaults["enabled"]), bool, "rq.enabled")
    if not enabled:
        return RQ_OFF
    patch_worker = _typed(section.get("patch_worker", defaults["patch_worker"]), bool, "rq.patch_worker")
    flush_timeout = section.get("flush_timeout", defaults["flush_timeout"])
    if (
        isinstance(flush_timeout, bool)
        or not isinstance(flush_timeout, int | float)
        or flush_timeout <= 0
        or not math.isfinite(flush_timeout)
    ):
        raise ConfigError("rq.flush_timeout must be a positive number of seconds")
    propagate_context = _typed(
        section.get("propagate_context", defaults["propagate_context"]), bool, "rq.propagate_context"
    )
    return RqConfig(
        enabled=enabled,
        patch_worker=patch_worker,
        flush_timeout=float(flush_timeout),
        propagate_context=propagate_context,
    )


def _endpoint(
    signal: str,
    protocol: str,
    signal_section: Mapping[str, Any],
    exporter_section: Mapping[str, Any],
    env: Mapping[str, str],
) -> str:
    explicit = signal_section.get("endpoint")
    if explicit:
        return _typed(explicit, str, f"{signal}.endpoint")
    base = exporter_section.get("endpoint")
    if base:
        return _signal_url(_typed(base, str, "exporter.endpoint"), signal, protocol)
    specific = env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific.strip()
    generic = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if generic:
        return _signal_url(generic.strip(), signal, protocol)
    raise ConfigError(
        f"no endpoint configured; set exporter.endpoint, {signal}.endpoint or OTEL_EXPORTER_OTLP_ENDPOINT"
    )


def _signal_url(base: str, signal: str, protocol: str) -> str:
    # OTLP spec: the generic endpoint is a base URL; HTTP appends the signal path, gRPC does not.
    if protocol == "grpc":
        return base
    return f"{base.rstrip('/')}/v1/{signal}"


def _pick(
    section: Mapping[str, Any],
    key: str,
    env: Mapping[str, str],
    env_names: tuple[str, ...],
    default: Any,
    parse: Callable[[str, str], Any],
) -> Any:
    value = section.get(key)
    if value is not None:
        return value
    for name in env_names:
        raw = env.get(name)
        if raw:
            return parse(raw, name)
    return default


def _section(user: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = user.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a dict")
    return value


def _typed(value: Any, expected: type, path: str) -> Any:
    if expected is not bool and isinstance(value, bool):
        raise ConfigError(f"{path} must be of type {expected.__name__}")
    if not isinstance(value, expected):
        raise ConfigError(f"{path} must be of type {expected.__name__}")
    return value


def _level(value: Any, path: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        levels = logging.getLevelNamesMapping()
        if value.upper() in levels:
            return levels[value.upper()]
    raise ConfigError(f"{path} must be a logging level name such as INFO")


def _resource_attributes(value: Any) -> dict[str, str | bool | int | float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError("resource_attributes must be a dict")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str | bool | int | float):
            raise ConfigError("resource_attributes keys must be strings and values str, bool, int or float")
    return dict(value)


def _parse_str(raw: str, name: str) -> str:
    return raw.strip()


def _parse_float(raw: str, name: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def _parse_bool(raw: str, name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    raise ConfigError(f"{name} must be true or false")


def _parse_headers(raw: str, name: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"{name} is malformed; expected key=value pairs separated by commas")
        headers[unquote(key.strip())] = unquote(value.strip())
    return headers


def _check_unknown_keys(
    user: Mapping[str, Any], defaults: Mapping[str, Any], path: tuple[str, ...], warnings: list[str]
) -> None:
    for key, value in user.items():
        full = (*path, str(key))
        if key not in defaults:
            warnings.append(f"unknown setting {'.'.join(full)!r} ignored")
            continue
        if full in _FREEFORM:
            continue
        if isinstance(defaults[key], dict) and isinstance(value, Mapping):
            _check_unknown_keys(value, defaults[key], full, warnings)
