import threading
import time

from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from netbox_opentelemetry_plugin import conf, otel
from tests.otel_helpers import RecordingMetricExporter, all_batches_points, data_points, metric_names

RESOURCE = otel.build_resource("netbox", {}, service_version="4.7.1", plugin_version="0.1.0", role="web")


def _reader_provider():
    reader = InMemoryMetricReader()
    return otel.build_meter_provider(RESOURCE, [reader]), reader


def test_allowlist_drops_unlisted_instruments_and_attributes():
    provider, reader = _reader_provider()
    meter = provider.get_meter("t")
    meter.create_histogram("http.server.request.duration", unit="s").record(
        0.1,
        {
            "http.request.method": "GET",
            "http.route": "api/",
            "http.response.status_code": 200,
            "server.port": 8080,
            "url.scheme": "http",
            "network.protocol.version": "1.1",
        },
    )
    meter.create_up_down_counter("http.server.active_requests").add(1, {"http.request.method": "GET"})
    meter.create_counter("some.library.counter").add(1)
    meter.create_up_down_counter("process.memory.usage").add(5, {"x": "kept"})
    data = reader.get_metrics_data()
    assert metric_names(data) == {"http.server.request.duration", "process.memory.usage"}
    (point,) = data_points(data, "http.server.request.duration")
    assert dict(point.attributes) == {
        "http.request.method": "GET",
        "http.route": "api/",
        "http.response.status_code": 200,
    }
    (runtime_point,) = data_points(data, "process.memory.usage")
    assert dict(runtime_point.attributes) == {"x": "kept"}


def test_every_allowlisted_attribute_key_set_is_explicit():
    for name, keys in otel.METRIC_ALLOWLIST.items():
        if keys is None:
            assert name.endswith(".*"), f"{name}: only runtime wildcards may keep their own attributes"


def test_build_metric_exporter_http_and_grpc():
    http = otel.build_metric_exporter(
        conf.ExporterConfig(endpoint="http://c:4318/v1/metrics", protocol="http/protobuf")
    )
    grpc = otel.build_metric_exporter(conf.ExporterConfig(endpoint="http://c:4317", protocol="grpc", insecure=True))
    assert type(http).__module__.startswith("opentelemetry.exporter.otlp.proto.http")
    assert type(grpc).__module__.startswith("opentelemetry.exporter.otlp.proto.grpc")


def test_pipeline_exports_on_its_own_thread_every_interval():
    exporter = RecordingMetricExporter()
    pipeline = otel.MetricsPipeline(RESOURCE, exporter, interval=0.05, timeout=1.0)
    try:
        pipeline.provider.get_meter("t").create_counter("netbox.object_changes").add(1)
        deadline = time.monotonic() + 5
        while not all_batches_points(exporter, "netbox.object_changes") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all_batches_points(exporter, "netbox.object_changes")
        assert any(t.name == "otel-metrics" for t in threading.enumerate())
    finally:
        pipeline.shutdown(1.0)
    assert not any(t.name == "otel-metrics" and t.is_alive() for t in threading.enumerate())
    assert exporter.shutdown_called


def test_pipeline_reader_has_no_sdk_thread():
    pipeline = otel.MetricsPipeline(RESOURCE, RecordingMetricExporter(), interval=60, timeout=1.0)
    try:
        assert not any(t.name == "OtelPeriodicExportingMetricReader" for t in threading.enumerate())
    finally:
        pipeline.shutdown(1.0)


def test_pipeline_survives_a_failing_exporter(caplog):
    exporter = RecordingMetricExporter(fail=True)
    pipeline = otel.MetricsPipeline(RESOURCE, exporter, interval=0.02, timeout=1.0)
    try:
        pipeline.provider.get_meter("t").create_counter("netbox.object_changes").add(1)
        time.sleep(0.2)
        exporter.fail = False
        deadline = time.monotonic() + 5
        while not exporter.batches and time.monotonic() < deadline:
            time.sleep(0.02)
        assert exporter.batches, "the export thread stopped after a failing export"
    finally:
        pipeline.shutdown(1.0)


def test_pipeline_shutdown_does_a_final_export():
    exporter = RecordingMetricExporter()
    pipeline = otel.MetricsPipeline(RESOURCE, exporter, interval=3600, timeout=1.0)
    pipeline.provider.get_meter("t").create_counter("netbox.object_changes").add(3)
    pipeline.shutdown(1.0)
    (point,) = all_batches_points(exporter, "netbox.object_changes")
    assert point.value == 3


def test_pipeline_interval_thread_stops_promptly_on_shutdown():
    pipeline = otel.MetricsPipeline(RESOURCE, RecordingMetricExporter(), interval=3600, timeout=1.0)
    started = time.monotonic()
    pipeline.shutdown(1.0)
    assert time.monotonic() - started < 1.0  # the Event wakes the thread; it does not sleep out the interval


def test_switchable_sync_instruments_follow_the_delegate():
    first, first_reader = _reader_provider()
    second, second_reader = _reader_provider()
    switchable = otel.SwitchableMeterProvider(first)
    counter = switchable.get_meter("t", "1").create_counter("netbox.object_changes")
    counter.add(1)
    switchable.set_delegate(second)
    counter.add(2)
    switchable.set_delegate(otel.noop_meter_provider())
    counter.add(4)
    assert [p.value for p in data_points(first_reader.get_metrics_data(), "netbox.object_changes")] == [1]
    assert [p.value for p in data_points(second_reader.get_metrics_data(), "netbox.object_changes")] == [2]


def test_switchable_histogram_keeps_advisory_buckets():
    provider, reader = _reader_provider()
    switchable = otel.SwitchableMeterProvider(provider)
    switchable.get_meter("t").create_histogram(
        "netbox.rq.job.duration", unit="s", explicit_bucket_boundaries_advisory=[1.0, 10.0]
    ).record(2.0)
    (point,) = data_points(reader.get_metrics_data(), "netbox.rq.job.duration")
    assert tuple(point.explicit_bounds) == (1.0, 10.0)


def test_switchable_observables_are_registered_again_on_a_new_delegate():
    first, first_reader = _reader_provider()
    second, second_reader = _reader_provider()
    switchable = otel.SwitchableMeterProvider(first)
    calls = []

    def callback(options):
        calls.append(1)
        return [otel.observation(7, {"messaging.destination.name": "default"})]

    switchable.get_meter("t").create_observable_gauge("netbox.rq.queue.depth", callbacks=[callback])
    assert [p.value for p in data_points(first_reader.get_metrics_data(), "netbox.rq.queue.depth")] == [7]
    switchable.set_delegate(second)
    assert [p.value for p in data_points(second_reader.get_metrics_data(), "netbox.rq.queue.depth")] == [7]
    switchable.set_delegate(NoOpMeterProvider())
    assert calls  # no error when replaying on a no-op provider


def test_switchable_force_flush_delegates_or_returns_true():
    provider, _ = _reader_provider()
    assert otel.SwitchableMeterProvider(provider).force_flush(1000) is True
    assert otel.SwitchableMeterProvider(otel.noop_meter_provider()).force_flush(1000) is True


def test_existing_meter_provider_only_returns_an_sdk_provider(monkeypatch):
    from opentelemetry import metrics

    monkeypatch.setattr(metrics, "get_meter_provider", lambda: NoOpMeterProvider())
    assert otel.existing_meter_provider() is None
    sdk = MeterProvider(shutdown_on_exit=False)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: sdk)
    assert otel.existing_meter_provider() is sdk


def test_system_metrics_instrumentor_has_a_repointable_process_handle():
    import os

    instrumentor = otel.load_system_metrics_instrumentor({"process.thread.count": None})
    assert otel.repoint_system_metrics_process(instrumentor) is True
    assert instrumentor._proc.pid == os.getpid()


def test_repoint_reports_a_missing_process_handle():
    assert otel.repoint_system_metrics_process(object()) is False
