import logging

import pytest

from netbox_opentelemetry_plugin import conf

ENDPOINT_ENV = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}


def test_defaults_with_generic_env_endpoint():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.enabled is True
    assert s.service_name == "netbox"
    assert s.logs.enabled is True
    assert s.logs.exporter.endpoint == "http://collector:4318/v1/logs"
    assert s.logs.exporter.protocol == "http/protobuf"
    assert s.logs.exporter.timeout == 10.0
    assert s.logs.loggers == ("netbox", "django", "rq")
    assert s.logs.level == logging.INFO
    assert s.logs.set_logger_levels is False
    assert s.warnings == ()


def test_signal_specific_env_endpoint_is_used_as_is():
    env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://logs:4318/custom", **ENDPOINT_ENV}
    assert conf.resolve({}, env).logs.exporter.endpoint == "http://logs:4318/custom"


def test_explicit_signal_endpoint_wins_over_everything():
    user = {"exporter": {"endpoint": "http://base:4318"}, "logs": {"endpoint": "http://explicit/v1/logs"}}
    env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://env-logs", **ENDPOINT_ENV}
    assert conf.resolve(user, env).logs.exporter.endpoint == "http://explicit/v1/logs"


def test_explicit_base_endpoint_wins_over_env_and_gets_signal_path():
    user = {"exporter": {"endpoint": "http://base:4318/"}}
    env = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://env-logs", **ENDPOINT_ENV}
    assert conf.resolve(user, env).logs.exporter.endpoint == "http://base:4318/v1/logs"


def test_grpc_endpoint_gets_no_path():
    user = {"exporter": {"endpoint": "http://collector:4317", "protocol": "grpc"}}
    s = conf.resolve(user, {})
    assert s.logs.exporter.protocol == "grpc"
    assert s.logs.exporter.endpoint == "http://collector:4317"


def test_protocol_from_signal_env_before_generic_env():
    env = {"OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", **ENDPOINT_ENV}
    assert conf.resolve({}, env).logs.exporter.protocol == "grpc"


def test_service_name_env_fallback_and_explicit_precedence():
    env = {"OTEL_SERVICE_NAME": "nb-env", **ENDPOINT_ENV}
    assert conf.resolve({}, env).service_name == "nb-env"
    assert conf.resolve({"service_name": "nb-cfg"}, env).service_name == "nb-cfg"


def test_headers_from_env_are_url_decoded_and_explicit_wins():
    env = {"OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20abc,x-tenant=t1", **ENDPOINT_ENV}
    assert conf.resolve({}, env).logs.exporter.headers == {"authorization": "Bearer abc", "x-tenant": "t1"}
    user = {"exporter": {"headers": {"x-only": "cfg"}}}
    assert conf.resolve(user, env).logs.exporter.headers == {"x-only": "cfg"}


def test_timeout_from_env():
    env = {"OTEL_EXPORTER_OTLP_TIMEOUT": "2.5", **ENDPOINT_ENV}
    assert conf.resolve({}, env).logs.exporter.timeout == 2.5


def test_missing_endpoint_disables_logs_with_one_warning():
    s = conf.resolve({}, {})
    assert s.enabled is True
    assert s.logs.enabled is False
    assert len(s.warnings) == 1
    assert "logs disabled" in s.warnings[0]
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


def test_logs_enabled_false_needs_no_endpoint():
    s = conf.resolve({"logs": {"enabled": False}}, {})
    assert s.logs.enabled is False
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
    assert redacted["logs"]["exporter"]["headers"] == {"authorization": "***"}
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
    redacted_endpoint = s.logs.exporter.redacted()["endpoint"]
    # endpoint resolution appends /v1/logs for http/protobuf; userinfo must be stripped regardless
    assert redacted_endpoint == "https://***@host:4318/v1/logs"
    assert "user:pass" not in redacted_endpoint


def test_insecure_defaults_to_none():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.logs.exporter.insecure is None


def test_insecure_explicit_false_is_respected():
    user = {"exporter": {"insecure": False}}
    s = conf.resolve(user, ENDPOINT_ENV)
    assert s.logs.exporter.insecure is False


def test_insecure_env_false_is_respected():
    env = {"OTEL_EXPORTER_OTLP_INSECURE": "false", **ENDPOINT_ENV}
    s = conf.resolve({}, env)
    assert s.logs.exporter.insecure is False


def test_insecure_env_true_is_respected():
    env = {"OTEL_EXPORTER_OTLP_LOGS_INSECURE": "true", **ENDPOINT_ENV}
    s = conf.resolve({}, env)
    assert s.logs.exporter.insecure is True


def test_header_values_not_in_exporter_config_repr():
    user = {"exporter": {"headers": {"authorization": "TOPSECRET"}}}
    s = conf.resolve(user, ENDPOINT_ENV)
    assert "TOPSECRET" not in repr(s)
    assert "TOPSECRET" not in repr(s.logs.exporter)


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
    assert s.redacted()["logs"]["exporter"]["endpoint"] == "https://***@collector:bad/v1/logs"


def test_rq_defaults():
    s = conf.resolve({}, ENDPOINT_ENV)
    assert s.rq == conf.RqConfig(enabled=True, patch_worker=True, flush_timeout=5.0)
    assert s.redacted()["rq"] == {"enabled": True, "patch_worker": True, "flush_timeout": 5.0}


def test_rq_explicit_values():
    s = conf.resolve({"rq": {"patch_worker": False, "flush_timeout": 2}}, ENDPOINT_ENV)
    assert s.rq.patch_worker is False
    assert s.rq.flush_timeout == 2.0


@pytest.mark.parametrize("bad", [{"flush_timeout": 0}, {"flush_timeout": True}, {"patch_worker": "yes"}, "on"])
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
