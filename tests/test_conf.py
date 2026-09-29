import logging
import math

import pytest

from netbox_opentelemetry_plugin import conf

ENDPOINT_ENV = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}


def test_defaults_with_generic_env_endpoint():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.enabled is True
    assert s.service_name == "netbox"
    assert s.logs.enabled is True
    assert s.log_exporter.endpoint == "http://collector:4318/v1/logs"
    assert s.log_exporter.protocol == "http/protobuf"
    assert s.log_exporter.timeout == 10.0
    assert s.logs.loggers == ("netbox", "django", "rq")
    assert s.logs.level == logging.INFO
    assert s.logs.set_logger_levels is False
    assert s.warnings == ()


def test_signal_specific_env_endpoint_is_used_as_is():
    env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://logs:4318/custom", **ENDPOINT_ENV}
    assert conf.resolve({}, env).log_exporter.endpoint == "http://logs:4318/custom"


def test_explicit_signal_endpoint_wins_over_everything():
    user = {"exporter": {"endpoint": "http://base:4318"}, "logs": {"endpoint": "http://explicit/v1/logs"}}
    env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://env-logs", **ENDPOINT_ENV}
    assert conf.resolve(user, env).log_exporter.endpoint == "http://explicit/v1/logs"


def test_explicit_base_endpoint_wins_over_env_and_gets_signal_path():
    user = {"exporter": {"endpoint": "http://base:4318/"}}
    env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://env-logs", **ENDPOINT_ENV}
    assert conf.resolve(user, env).log_exporter.endpoint == "http://base:4318/v1/logs"


def test_grpc_endpoint_gets_no_path():
    user = {"exporter": {"endpoint": "http://collector:4317", "protocol": "grpc"}}
    s = conf.resolve(user, {})
    assert s.log_exporter.protocol == "grpc"
    assert s.log_exporter.endpoint == "http://collector:4317"


def test_protocol_from_signal_env_before_generic_env():
    env = {"OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", **ENDPOINT_ENV}
    assert conf.resolve({}, env).log_exporter.protocol == "grpc"


def test_service_name_env_fallback_and_explicit_precedence():
    env = {"OTEL_SERVICE_NAME": "nb-env", **ENDPOINT_ENV}
    assert conf.resolve({}, env).service_name == "nb-env"
    assert conf.resolve({"service_name": "nb-cfg"}, env).service_name == "nb-cfg"


def test_headers_from_env_are_url_decoded_and_explicit_wins():
    env = {"OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20abc,x-tenant=t1", **ENDPOINT_ENV}
    assert conf.resolve({}, env).log_exporter.headers == {"authorization": "Bearer abc", "x-tenant": "t1"}
    user = {"exporter": {"headers": {"x-only": "cfg"}}}
    assert conf.resolve(user, env).log_exporter.headers == {"x-only": "cfg"}


def test_timeout_from_env():
    env = {"OTEL_EXPORTER_OTLP_TIMEOUT": "2.5", **ENDPOINT_ENV}
    assert conf.resolve({}, env).log_exporter.timeout == 2.5


def test_missing_endpoint_disables_logs_and_audit_with_one_warning():
    s = conf.resolve({}, {})
    assert s.enabled is True
    assert s.logs.enabled is False
    assert s.audit.enabled is False
    assert s.log_exporter is None
    assert len(s.warnings) == 1
    assert s.warnings[0].startswith("logs and audit disabled:")
    assert "no endpoint" in s.warnings[0]


def test_unsupported_protocol_disables_logs():
    s = conf.resolve({"exporter": {"protocol": "http/json"}}, ENDPOINT_ENV)
    assert s.logs.enabled is False
    assert any("unsupported protocol" in w for w in s.warnings)


def test_bad_logs_value_disables_only_logs():
    s = conf.resolve({"logs": {"level": "LOUD"}}, ENDPOINT_ENV)
    assert s.enabled is True
    assert s.logs.enabled is False
    assert any("logs.level" in w for w in s.warnings)


def test_bad_shared_value_disables_plugin():
    s = conf.resolve({"service_name": 42}, ENDPOINT_ENV)
    assert s.enabled is False
    assert any("service_name" in w for w in s.warnings)


def test_level_accepts_int_and_name():
    assert conf.resolve({"logs": {"level": "debug"}}, ENDPOINT_ENV).logs.level == logging.DEBUG
    assert conf.resolve({"logs": {"level": 30}}, ENDPOINT_ENV).logs.level == logging.WARNING


def test_sdk_disabled_env_turns_everything_off():
    s = conf.resolve({}, {"OTEL_SDK_DISABLED": "true", **ENDPOINT_ENV})
    assert s.enabled is False
    assert s.logs.enabled is False


def test_enabled_false():
    s = conf.resolve({"enabled": False}, ENDPOINT_ENV)
    assert s.enabled is False
    assert s.logs.enabled is False


def test_logs_and_audit_disabled_need_no_endpoint():
    s = conf.resolve({"logs": {"enabled": False}, "audit": {"enabled": False}}, {})
    assert s.logs.enabled is False
    assert s.audit.enabled is False
    assert s.log_exporter is None
    assert s.warnings == ()


def test_unknown_keys_warn_but_free_form_sections_do_not():
    user = {
        "foo": 1,
        "logs": {"bar": 2},
        "exporter": {"headers": {"anything": "goes"}},
        "resource_attributes": {"deployment.environment.name": "dev"},
        "audit": {"enabled": True},
    }
    s = conf.resolve(user, ENDPOINT_ENV)
    assert any("'foo'" in w for w in s.warnings)
    assert any("'logs.bar'" in w for w in s.warnings)
    assert not any("anything" in w or "deployment" in w or "audit" in w for w in s.warnings)


def test_resource_attributes_are_validated():
    s = conf.resolve({"resource_attributes": {"k": ["not", "allowed"]}}, ENDPOINT_ENV)
    assert s.enabled is False
    assert conf.resolve({"resource_attributes": {"k": "v", "n": 1}}, ENDPOINT_ENV).resource_attributes == {
        "k": "v",
        "n": 1,
    }


def test_redacted_masks_header_values():
    user = {"exporter": {"headers": {"authorization": "Bearer SECRET-VALUE"}}}
    s = conf.resolve(user, ENDPOINT_ENV)
    redacted = s.redacted()
    assert redacted["log_exporter"]["headers"] == {"authorization": "***"}
    assert "SECRET-VALUE" not in repr(redacted)


def test_redacted_log_exporter_masks_header_values():
    # log_exporter is resolved independently of LogsConfig (audit needs it even with logs
    # disabled), so its own redacted() must mask headers the same way.
    user = {"exporter": {"headers": {"authorization": "Bearer SECRET-VALUE"}}, "logs": {"enabled": False}}
    s = conf.resolve(user, ENDPOINT_ENV)
    redacted = s.redacted()
    assert redacted["log_exporter"]["headers"] == {"authorization": "***"}
    assert "SECRET-VALUE" not in repr(redacted)


def test_malformed_header_env_warning_does_not_leak_value():
    env = {"OTEL_EXPORTER_OTLP_HEADERS": "SECRET-NO-EQUALS-SIGN", **ENDPOINT_ENV}
    s = conf.resolve({}, env)
    assert s.logs.enabled is False
    assert "SECRET-NO-EQUALS-SIGN" not in " ".join(s.warnings)
    assert any("OTEL_EXPORTER_OTLP_HEADERS" in w for w in s.warnings)


def test_non_dict_plugin_config():
    s = conf.resolve(["not", "a", "dict"], ENDPOINT_ENV)
    assert s.enabled is False
    assert s.warnings


def test_redacted_endpoint_strips_userinfo():
    user = {"exporter": {"endpoint": "https://user:pass@host:4318"}}
    s = conf.resolve(user, {})
    redacted_endpoint = s.log_exporter.redacted()["endpoint"]
    # endpoint resolution appends /v1/logs for http/protobuf; userinfo must be stripped regardless
    assert redacted_endpoint == "https://***@host:4318/v1/logs"
    assert "user:pass" not in redacted_endpoint


def test_insecure_defaults_to_none():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.log_exporter.insecure is None


def test_insecure_explicit_false_is_respected():
    user = {"exporter": {"insecure": False}}
    s = conf.resolve(user, ENDPOINT_ENV)
    assert s.log_exporter.insecure is False


def test_insecure_env_false_is_respected():
    env = {"OTEL_EXPORTER_OTLP_INSECURE": "false", **ENDPOINT_ENV}
    s = conf.resolve({}, env)
    assert s.log_exporter.insecure is False


def test_insecure_env_true_is_respected():
    env = {"OTEL_EXPORTER_OTLP_LOGS_INSECURE": "true", **ENDPOINT_ENV}
    s = conf.resolve({}, env)
    assert s.log_exporter.insecure is True


def test_header_values_not_in_exporter_config_repr():
    user = {"exporter": {"headers": {"authorization": "TOPSECRET"}}}
    s = conf.resolve(user, ENDPOINT_ENV)
    assert "TOPSECRET" not in repr(s)
    assert "TOPSECRET" not in repr(s.log_exporter)


def test_redact_userinfo_keeps_malformed_port_and_ipv6_intact():
    assert conf._redact_userinfo("https://u:p@host:bad/v1/logs") == "https://***@host:bad/v1/logs"
    assert conf._redact_userinfo("http://u:p@[::1]:4318/v1/logs") == "http://***@[::1]:4318/v1/logs"
    assert conf._redact_userinfo("http://collector:4318/v1/logs") == "http://collector:4318/v1/logs"


def test_redact_userinfo_unparseable_url_is_fully_masked():
    assert conf._redact_userinfo("http://u:p@[::1:4318") == conf.REDACTED


def test_exporter_repr_hides_credentials_and_headers():
    cfg = conf.ExporterConfig(
        "https://user:SECRETPW@collector:4318/v1/logs", "http/protobuf", {"authorization": "TOPSECRET"}
    )
    text = repr(cfg)
    assert "SECRETPW" not in text
    assert "TOPSECRET" not in text
    assert "collector:4318" in text


def test_malformed_port_does_not_break_redacted_output():
    s = conf.resolve({"exporter": {"endpoint": "https://u:p@collector:bad"}}, {})
    assert s.redacted()["log_exporter"]["endpoint"] == "https://***@collector:bad/v1/logs"


def test_rq_defaults():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.rq == conf.RqConfig(
        enabled=True,
        patch_worker=True,
        flush_timeout=5.0,
        propagate_context=True,
        flush_breaker_threshold=3,
        flush_breaker_cooldown=30.0,
    )
    assert s.redacted()["rq"] == {
        "enabled": True,
        "patch_worker": True,
        "flush_timeout": 5.0,
        "propagate_context": True,
        "flush_breaker_threshold": 3,
        "flush_breaker_cooldown": 30.0,
    }


def test_rq_explicit_values():
    s = conf.resolve({"rq": {"patch_worker": False, "flush_timeout": 2}}, ENDPOINT_ENV)
    assert s.rq.patch_worker is False
    assert s.rq.flush_timeout == 2.0


def test_rq_flush_breaker_explicit_values():
    s = conf.resolve({"rq": {"flush_breaker_threshold": 0, "flush_breaker_cooldown": 5}}, ENDPOINT_ENV)
    assert s.rq.flush_breaker_threshold == 0
    assert s.rq.flush_breaker_cooldown == 5.0
    assert isinstance(s.rq.flush_breaker_cooldown, float)


@pytest.mark.parametrize(
    "bad",
    [
        {"flush_timeout": 0},
        {"flush_timeout": True},
        {"patch_worker": "yes"},
        "on",
        {"flush_breaker_threshold": -1},
        {"flush_breaker_threshold": 2.5},
        {"flush_breaker_threshold": True},
        {"flush_breaker_threshold": "3"},
        {"flush_breaker_cooldown": 0},
        {"flush_breaker_cooldown": -5},
        {"flush_breaker_cooldown": False},
        {"flush_breaker_cooldown": float("inf")},
        {"flush_breaker_cooldown": float("nan")},
        {"flush_breaker_cooldown": "30"},
    ],
)
def test_bad_rq_value_disables_only_rq(bad):
    s = conf.resolve({"rq": bad}, ENDPOINT_ENV)
    assert s.enabled is True
    assert s.logs.enabled is True
    assert s.rq.enabled is False
    assert any(w.startswith("rq disabled:") for w in s.warnings)


def test_rq_does_not_need_an_endpoint():
    s = conf.resolve({}, {})
    assert s.rq.enabled is True


@pytest.mark.parametrize("bad_timeout", [float("nan"), float("inf")])
def test_rq_flush_timeout_rejects_non_finite(bad_timeout):
    s = conf.resolve({"rq": {"flush_timeout": bad_timeout}}, ENDPOINT_ENV)
    assert s.rq.enabled is False
    assert any(w.startswith("rq disabled:") for w in s.warnings)


def test_exporter_timeout_rejects_non_finite():
    s = conf.resolve({"exporter": {"timeout": float("inf")}}, ENDPOINT_ENV)
    assert s.logs.enabled is False
    assert any("exporter.timeout" in w for w in s.warnings)


def test_exporter_timeout_env_rejects_nan():
    env = {"OTEL_EXPORTER_OTLP_TIMEOUT": "nan", **ENDPOINT_ENV}
    s = conf.resolve({}, env)
    assert s.logs.enabled is False
    assert any("exporter.timeout" in w for w in s.warnings)


def test_rq_disabled_skips_validation():
    s = conf.resolve({"rq": {"enabled": False, "flush_timeout": -1}}, ENDPOINT_ENV)
    assert s.rq == conf.RQ_OFF
    assert not any(w.startswith("rq disabled:") for w in s.warnings)


def test_audit_defaults():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.audit == conf.AuditConfig(
        enabled=True, include_data=False, exclude_fields=("password", "secret", "token", "key")
    )
    assert s.log_exporter is not None
    assert s.log_exporter.endpoint == "http://collector:4318/v1/logs"
    assert s.redacted()["audit"] == {
        "enabled": True,
        "include_data": False,
        "exclude_fields": ["password", "secret", "token", "key"],
    }


def test_audit_only_resolves_the_log_exporter():
    s = conf.resolve({"logs": {"enabled": False}}, ENDPOINT_ENV)
    assert s.logs.enabled is False
    assert not hasattr(s.logs, "exporter")
    assert s.audit.enabled is True
    assert s.log_exporter.endpoint == "http://collector:4318/v1/logs"


def test_audit_uses_the_logs_endpoint():
    s = conf.resolve({"logs": {"enabled": False, "endpoint": "http://logs:4318/custom"}}, ENDPOINT_ENV)
    assert s.log_exporter.endpoint == "http://logs:4318/custom"


def test_audit_explicit_values():
    s = conf.resolve({"audit": {"include_data": True, "exclude_fields": ["Comments"]}}, ENDPOINT_ENV)
    assert s.audit.include_data is True
    assert s.audit.exclude_fields == ("Comments",)


@pytest.mark.parametrize(
    "bad", [{"include_data": "yes"}, {"exclude_fields": "password"}, {"exclude_fields": [1]}, "on"]
)
def test_bad_audit_value_disables_only_audit(bad):
    s = conf.resolve({"audit": bad}, ENDPOINT_ENV)
    assert s.audit.enabled is False
    assert s.logs.enabled is True
    assert any(w.startswith("audit disabled:") for w in s.warnings)


def test_missing_endpoint_with_logs_disabled_names_only_audit():
    s = conf.resolve({"logs": {"enabled": False}}, {})
    assert s.audit.enabled is False
    assert s.warnings == (s.warnings[0],)
    assert s.warnings[0].startswith("audit disabled:")


def test_audit_disabled_skips_validation():
    s = conf.resolve({"audit": {"enabled": False, "include_data": "yes"}}, ENDPOINT_ENV)
    assert s.audit == conf.AUDIT_OFF
    assert not any(w.startswith("audit disabled:") for w in s.warnings)


def test_traces_off_by_default():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.traces == conf.TRACES_OFF
    assert s.redacted()["traces"]["enabled"] is False
    assert s.exporters() == (s.log_exporter,)


def test_traces_enabled_defaults():
    s = conf.resolve({"traces": {"enabled": True}}, ENDPOINT_ENV)
    t = s.traces
    assert t.enabled is True
    assert t.exporter.endpoint == "http://collector:4318/v1/traces"
    assert t.sampler == "parentbased_traceidratio"
    assert t.sampler_arg == 1.0
    assert t.instrument == ("django", "psycopg", "redis", "requests")
    assert t.excluded_urls == ("/static/", "/metrics", "/api/status/")
    assert s.exporters() == (s.log_exporter, t.exporter)
    assert s.warnings == ()


def test_traces_signal_endpoint_env_and_explicit_endpoint():
    env = {**ENDPOINT_ENV, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://tempo:4318/v1/traces"}
    assert conf.resolve({"traces": {"enabled": True}}, env).traces.exporter.endpoint == "http://tempo:4318/v1/traces"
    explicit = {"traces": {"enabled": True, "endpoint": "http://other:4318/v1/traces"}}
    assert conf.resolve(explicit, env).traces.exporter.endpoint == "http://other:4318/v1/traces"


def test_traces_sampler_from_env_and_config():
    env = {**ENDPOINT_ENV, "OTEL_TRACES_SAMPLER": "traceidratio", "OTEL_TRACES_SAMPLER_ARG": "0.25"}
    t = conf.resolve({"traces": {"enabled": True}}, env).traces
    assert (t.sampler, t.sampler_arg) == ("traceidratio", 0.25)
    t = conf.resolve({"traces": {"enabled": True, "sampler": "ALWAYS_ON", "sampler_arg": 1}}, env).traces
    assert (t.sampler, t.sampler_arg) == ("always_on", 1.0)


@pytest.mark.parametrize(
    ("section", "env", "fragment"),
    [
        ({"sampler": "jaeger_remote"}, {}, "traces.sampler"),
        ({}, {"OTEL_TRACES_SAMPLER": "xray"}, "traces.sampler"),
        ({"sampler_arg": 1.5}, {}, "traces.sampler_arg"),
        ({"sampler_arg": True}, {}, "traces.sampler_arg"),
        ({}, {"OTEL_TRACES_SAMPLER_ARG": "half"}, "OTEL_TRACES_SAMPLER_ARG"),
        ({"instrument": ["django", "celery"]}, {}, "traces.instrument"),
        ({"instrument": "django"}, {}, "traces.instrument"),
        ({"excluded_urls": ["/a/,/b/"]}, {}, "traces.excluded_urls"),
        ({"excluded_urls": [1]}, {}, "traces.excluded_urls"),
        ({"enabled": "yes"}, {}, "traces.enabled"),
    ],
)
def test_invalid_traces_settings_disable_traces_only(section, env, fragment):
    s = conf.resolve({"traces": {"enabled": True, **section}}, {**ENDPOINT_ENV, **env})
    assert s.traces == conf.TRACES_OFF
    assert s.logs.enabled is True
    assert len(s.warnings) == 1
    assert s.warnings[0].startswith("traces disabled:")
    assert fragment in s.warnings[0]


def test_traces_without_endpoint_warns_once():
    s = conf.resolve({"traces": {"enabled": True}, "logs": {"enabled": False}, "audit": {"enabled": False}}, {})
    assert s.traces == conf.TRACES_OFF
    assert len(s.warnings) == 1
    assert s.warnings[0].startswith("traces disabled:") and "no endpoint" in s.warnings[0]


def test_traces_section_must_be_a_dict():
    s = conf.resolve({"traces": ["enabled"]}, ENDPOINT_ENV)
    assert s.traces == conf.TRACES_OFF
    assert s.warnings == ("traces disabled: traces must be a dict",)


def test_instrument_duplicates_collapse_and_empty_list_is_allowed():
    s = conf.resolve({"traces": {"enabled": True, "instrument": ["redis", "redis", "django"]}}, ENDPOINT_ENV)
    assert s.traces.instrument == ("redis", "django")
    s = conf.resolve({"traces": {"enabled": True, "instrument": [], "excluded_urls": []}}, ENDPOINT_ENV)
    assert s.traces.instrument == ()
    assert s.traces.excluded_urls == ()


def test_traces_redacted_masks_headers():
    user = {"exporter": {"headers": {"authorization": "Bearer abc"}}, "traces": {"enabled": True}}
    red = conf.resolve(user, ENDPOINT_ENV).redacted()["traces"]
    assert red["exporter"]["headers"] == {"authorization": conf.REDACTED}
    assert red["sampler"] == "parentbased_traceidratio"
    assert "abc" not in repr(red)


def test_rq_propagate_context():
    assert conf.resolve({}, ENDPOINT_ENV).rq.propagate_context is True
    s = conf.resolve({"rq": {"propagate_context": False}}, ENDPOINT_ENV)
    assert s.rq.propagate_context is False
    assert s.redacted()["rq"]["propagate_context"] is False
    s = conf.resolve({"rq": {"propagate_context": "no"}}, ENDPOINT_ENV)
    assert s.rq == conf.RQ_OFF
    assert "rq.propagate_context" in s.warnings[0]


BASE = {"exporter": {"endpoint": "http://collector:4318"}}


def test_metrics_off_by_default():
    settings = conf.resolve(BASE, {})
    assert settings.metrics.enabled is False
    assert settings.metrics is conf.METRICS_OFF


def test_metrics_enabled_resolves_exporter_and_defaults():
    settings = conf.resolve({**BASE, "metrics": {"enabled": True}}, {})
    m = settings.metrics
    assert m.enabled and m.change_counters and not m.runtime
    assert m.export_interval == 60.0
    assert m.exporter.endpoint == "http://collector:4318/v1/metrics"
    assert m.exporter in settings.exporters()


def test_metrics_signal_endpoint_env_wins_over_generic():
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://a:4318", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://b:4318/m"}
    assert conf.resolve({"metrics": {"enabled": True}}, env).metrics.exporter.endpoint == "http://b:4318/m"


def test_export_interval_env_is_milliseconds():
    env = {"OTEL_METRIC_EXPORT_INTERVAL": "15000"}
    assert conf.resolve({**BASE, "metrics": {"enabled": True}}, env).metrics.export_interval == 15.0


def test_explicit_export_interval_is_seconds_and_wins_over_env():
    env = {"OTEL_METRIC_EXPORT_INTERVAL": "15000"}
    settings = conf.resolve({**BASE, "metrics": {"enabled": True, "export_interval": 5}}, env)
    assert settings.metrics.export_interval == 5.0


@pytest.mark.parametrize("value", [0, -1, True, "60", math.inf, math.nan])
def test_bad_export_interval_disables_metrics_only(value):
    settings = conf.resolve({**BASE, "metrics": {"enabled": True, "export_interval": value}}, {})
    assert settings.metrics is conf.METRICS_OFF
    assert settings.logs.enabled
    assert any("metrics disabled" in w for w in settings.warnings)


@pytest.mark.parametrize(
    ("section", "env"),
    [({"export_interval": 0.001}, {}), ({}, {"OTEL_METRIC_EXPORT_INTERVAL": "1"})],
)
def test_export_interval_below_one_second_is_raised_to_one_second(section, env):
    settings = conf.resolve({**BASE, "metrics": {"enabled": True, **section}}, env)
    assert settings.metrics.enabled
    assert settings.metrics.export_interval == 1.0
    assert "metrics.export_interval below 1 s; using 1 s" in settings.warnings


def test_export_interval_of_one_second_is_kept_without_warning():
    settings = conf.resolve({**BASE, "metrics": {"enabled": True, "export_interval": 1}}, {})
    assert settings.metrics.export_interval == 1.0
    assert not any("export_interval" in w for w in settings.warnings)


def test_bad_export_interval_env_disables_metrics():
    settings = conf.resolve({**BASE, "metrics": {"enabled": True}}, {"OTEL_METRIC_EXPORT_INTERVAL": "soon"})
    assert settings.metrics is conf.METRICS_OFF


@pytest.mark.parametrize("key", ["change_counters", "runtime"])
def test_metrics_flags_must_be_bool(key):
    settings = conf.resolve({**BASE, "metrics": {"enabled": True, key: "yes"}}, {})
    assert settings.metrics is conf.METRICS_OFF


def test_metrics_without_endpoint_warns_and_disables():
    settings = conf.resolve({"logs": {"enabled": False}, "audit": {"enabled": False}, "metrics": {"enabled": True}}, {})
    assert settings.metrics is conf.METRICS_OFF
    assert any(w.startswith("metrics disabled: no endpoint") for w in settings.warnings)


def test_metrics_redacted_masks_headers():
    user = {**BASE, "exporter": {**BASE["exporter"], "headers": {"x-key": "s3cret"}}, "metrics": {"enabled": True}}
    redacted = conf.resolve(user, {}).redacted()
    assert redacted["metrics"]["exporter"]["headers"] == {"x-key": conf.REDACTED}
    assert "s3cret" not in repr(redacted)


def test_excluded_urls_are_resolved_with_traces_off():
    settings = conf.resolve({**BASE, "traces": {"excluded_urls": ["/health/"]}}, {})
    assert settings.traces.enabled is False
    assert settings.traces.excluded_urls == ("/health/",)


def test_plugin_logger_name_lives_in_conf():
    assert conf.PLUGIN_LOGGER == "netbox_opentelemetry_plugin"


def test_logs_config_has_no_exporter_alias():
    s = conf.resolve({"exporter": {"endpoint": "http://collector:4318"}}, {})
    assert not hasattr(s.logs, "exporter")
    assert "exporter" not in s.logs.redacted()
    assert s.redacted()["log_exporter"]["endpoint"] == "http://collector:4318/v1/logs"


def test_insecure_skip_verify_defaults_to_false():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.log_exporter.insecure_skip_verify is False
    assert s.log_exporter.redacted()["insecure_skip_verify"] is False


def test_insecure_skip_verify_true_is_respected():
    user = {"exporter": {"insecure_skip_verify": True}, "traces": {"enabled": True}, "metrics": {"enabled": True}}
    s = conf.resolve(user, ENDPOINT_ENV)
    assert s.log_exporter.insecure_skip_verify is True
    assert s.traces.exporter.insecure_skip_verify is True
    assert s.metrics.exporter.insecure_skip_verify is True
    assert s.warnings == ()


def test_insecure_skip_verify_must_be_bool():
    with pytest.raises(conf.ConfigError, match="exporter.insecure_skip_verify"):
        conf.resolve_exporter("logs", {}, {"insecure_skip_verify": "yes"}, ENDPOINT_ENV)


def test_insecure_skip_verify_is_rejected_over_grpc():
    with pytest.raises(conf.ConfigError, match="not supported over grpc"):
        conf.resolve_exporter("logs", {}, {"insecure_skip_verify": True, "protocol": "grpc"}, ENDPOINT_ENV)


def test_insecure_skip_verify_with_certificate_is_rejected():
    section = {"insecure_skip_verify": True, "certificate": "/etc/ssl/ca.pem"}
    with pytest.raises(conf.ConfigError, match="exporter.certificate"):
        conf.resolve_exporter("logs", {}, section, ENDPOINT_ENV)


def test_insecure_skip_verify_with_env_certificate_is_rejected():
    env = {"OTEL_EXPORTER_OTLP_CERTIFICATE": "/etc/ssl/ca.pem", **ENDPOINT_ENV}
    with pytest.raises(conf.ConfigError, match="exporter.certificate"):
        conf.resolve_exporter("logs", {}, {"insecure_skip_verify": True}, env)


def test_insecure_skip_verify_is_not_read_from_the_environment():
    env = {"OTEL_EXPORTER_OTLP_INSECURE_SKIP_VERIFY": "true", **ENDPOINT_ENV}
    assert conf.resolve({}, env).log_exporter.insecure_skip_verify is False


def test_insecure_skip_verify_disables_only_the_grpc_signal():
    user = {"exporter": {"insecure_skip_verify": True}, "traces": {"enabled": True}, "metrics": {"enabled": True}}
    env = {"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc", **ENDPOINT_ENV}
    s = conf.resolve(user, env)
    assert s.traces.enabled is False
    assert any("not supported over grpc" in w for w in s.warnings)
    assert s.log_exporter.insecure_skip_verify is True
    assert s.metrics.exporter.insecure_skip_verify is True
