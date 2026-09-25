import logging
from types import SimpleNamespace

import netbox_opentelemetry_plugin as plugin
from netbox_opentelemetry_plugin import bootstrap
from netbox_opentelemetry_plugin.version import __version__


def test_plugin_config_metadata():
    cfg = plugin.NetBoxOpenTelemetryConfig
    assert cfg.name == "netbox_opentelemetry_plugin"
    assert cfg.min_version == "4.7.0"
    assert cfg.max_version == "4.7.99"
    assert cfg.default_settings == {}
    assert cfg.version == __version__


def test_netbox_loads_config_attribute():
    # NetBox imports "<plugin>.config" and expects the PluginConfig class there.
    assert plugin.config is plugin.NetBoxOpenTelemetryConfig


def _fake_settings():
    return SimpleNamespace(
        PLUGINS_CONFIG={"netbox_opentelemetry_plugin": {"enabled": False}},
        RELEASE=SimpleNamespace(version="4.7.1"),
    )


def test_ready_passes_plugin_config_and_version(monkeypatch):
    seen = {}

    def fake_install(user_config, **kwargs):
        seen["user_config"] = user_config
        seen["netbox_version"] = kwargs["netbox_version"]

    monkeypatch.setattr(plugin, "_django_settings", _fake_settings)
    monkeypatch.setattr(bootstrap, "install", fake_install)
    instance = object.__new__(plugin.NetBoxOpenTelemetryConfig)
    instance.ready()
    assert seen == {"user_config": {"enabled": False}, "netbox_version": "4.7.1"}


def test_ready_never_raises(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(plugin, "_django_settings", _fake_settings)
    monkeypatch.setattr(bootstrap, "install", boom)
    instance = object.__new__(plugin.NetBoxOpenTelemetryConfig)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        instance.ready()
    assert any("setup failed" in r.getMessage() for r in caplog.records)
