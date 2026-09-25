"""Unit tests run without NetBox. Provide a minimal stand-in for netbox.plugins.PluginConfig."""

import sys
import types

try:
    import netbox.plugins  # noqa: F401
except ImportError:
    netbox_module = types.ModuleType("netbox")
    plugins_module = types.ModuleType("netbox.plugins")

    class PluginConfig:
        def ready(self):
            pass

    plugins_module.PluginConfig = PluginConfig
    netbox_module.plugins = plugins_module
    sys.modules["netbox"] = netbox_module
    sys.modules["netbox.plugins"] = plugins_module
