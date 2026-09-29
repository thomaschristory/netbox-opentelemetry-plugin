"""Unit tests run without NetBox. Provide a minimal stand-in for netbox.plugins.PluginConfig."""

import os
import sys
import types

import pytest

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


@pytest.fixture(autouse=True)
def _restore_semconv_opt_in():
    saved = os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN")
    yield
    if saved is None:
        os.environ.pop("OTEL_SEMCONV_STABILITY_OPT_IN", None)
    else:
        os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = saved


@pytest.fixture(autouse=True)
def _restore_global_propagator():
    # TracesModule wraps the global propagator and never restores it (by design), so tests do it.
    from opentelemetry import propagate

    saved = propagate.get_global_textmap()
    yield
    propagate.set_global_textmap(saved)
