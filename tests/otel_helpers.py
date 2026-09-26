"""Metric test helpers shared by unit and NetBox integration tests (OTel SDK only, no NetBox)."""

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult


class RecordingMetricExporter(MetricExporter):
    """Keeps every exported batch. `fail` makes export raise; `block` (a threading.Event) makes it wait."""

    def __init__(self, fail: bool = False, block=None) -> None:
        super().__init__()
        self.batches = []
        self.fail = fail
        self.block = block
        self.shutdown_called = False

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        if self.block is not None:
            self.block.wait()
        if self.fail:
            raise RuntimeError("collector down")
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        self.shutdown_called = True


def _metrics(metrics_data):
    if metrics_data is None:
        return
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                yield resource_metrics.resource, metric


def data_points(metrics_data, name: str) -> list:
    return [point for _, metric in _metrics(metrics_data) if metric.name == name for point in metric.data.data_points]


def metric_names(metrics_data) -> set[str]:
    return {metric.name for _, metric in _metrics(metrics_data)}


def resources(metrics_data) -> list:
    return [resource for resource, _ in _metrics(metrics_data)]


def all_batches_points(exporter: RecordingMetricExporter, name: str) -> list:
    return [point for batch in exporter.batches for point in data_points(batch, name)]
