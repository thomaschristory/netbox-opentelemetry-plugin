import netbox_opentelemetry_plugin as plugin
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
