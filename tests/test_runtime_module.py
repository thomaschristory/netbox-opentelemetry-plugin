import logging
import os

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from netbox_opentelemetry_plugin import conf, otel
from netbox_opentelemetry_plugin.modules.base import Context
from netbox_opentelemetry_plugin.modules.runtime import RUNTIME_METRICS, RuntimeModule
from tests.otel_helpers import metric_names

RESOURCE = otel.build_resource("netbox", {}, service_version="4.7.1", plugin_version="0.1.0", role="web")


def _ctx(runtime=True):
    reader = InMemoryMetricReader()
    meter = otel.SwitchableMeterProvider(otel.build_meter_provider(RESOURCE, [reader]))
    settings = conf.Settings(enabled=True, metrics=conf.MetricsConfig(enabled=True, runtime=runtime))
    return Context(settings=settings, role="web", resource=RESOURCE, meter_provider=meter), reader


@pytest.fixture
def module():
    module = RuntimeModule()
    yield module
    module.shutdown()


def test_enabled_needs_metrics_and_runtime():
    assert RuntimeModule().enabled(conf.Settings(enabled=True, metrics=conf.MetricsConfig(enabled=True, runtime=True)))
    assert not RuntimeModule().enabled(conf.Settings(enabled=True, metrics=conf.MetricsConfig(enabled=True)))
    assert not RuntimeModule().enabled(conf.Settings(enabled=True))


def test_only_process_level_metrics_are_configured():
    assert RUNTIME_METRICS
    assert all(name.startswith(("process.", "cpython.gc.")) for name in RUNTIME_METRICS)
    assert not any(name.startswith("process.runtime.") for name in RUNTIME_METRICS)


def test_install_exports_process_metrics_only(module):
    ctx, reader = _ctx()
    module.install(ctx)
    names = metric_names(reader.get_metrics_data())
    assert "process.cpu.time" in names and "process.memory.usage" in names
    assert all(name.startswith(("process.", "cpython.gc.")) for name in names)


def test_after_fork_repoints_the_process_handle(module, monkeypatch):
    ctx, _ = _ctx()
    module.install(ctx)
    seen = []
    monkeypatch.setattr(otel, "repoint_system_metrics_process", lambda inst: seen.append(inst) or True)
    module.after_fork(ctx)
    assert seen == [module._instrumentor]


def test_after_fork_warns_when_the_handle_cannot_be_repointed(module, monkeypatch, caplog):
    ctx, _ = _ctx()
    module.install(ctx)
    monkeypatch.setattr(otel, "repoint_system_metrics_process", lambda inst: False)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        module.after_fork(ctx)
    assert any("runtime metrics" in r.getMessage() for r in caplog.records)


def test_install_without_meter_provider_does_nothing(module):
    ctx, _ = _ctx()
    ctx.meter_provider = None
    module.install(ctx)
    assert module._instrumentor is None


def test_already_instrumented_outside_the_plugin_is_left_alone(module, monkeypatch):
    class Instrumented:
        is_instrumented_by_opentelemetry = True

        def instrument(self, **kwargs):
            raise AssertionError("must not instrument twice")

    monkeypatch.setattr(otel, "load_system_metrics_instrumentor", lambda config: Instrumented())
    ctx, _ = _ctx()
    module.install(ctx)
    assert module._instrumentor is None


def test_process_handle_describes_this_process(module):
    ctx, _ = _ctx()
    module.install(ctx)
    assert module._instrumentor._proc.pid == os.getpid()
