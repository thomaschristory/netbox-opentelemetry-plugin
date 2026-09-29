"""A minimal OTLP/gRPC receiver for logs, traces and metrics, run as its own process by the fork tests.

Usage: python tests/grpc_receiver.py

It listens on an ephemeral port on 127.0.0.1, prints ``{"port": N}`` as the first line on stdout,
then one JSON line per resource in each Export request:

    {"signal": "logs", "instance_id": "<service.instance.id>", "names": [...]}

``names`` holds the log bodies, the span names or the metric names. It stops when stdin is closed.

It runs in a separate process on purpose. A gRPC server inside the test process would leave gRPC
core threads and polling state in every child the test forks, which is not what the plugin meets
in production (the Collector is another process), and gRPC does not support using it across fork
in that situation.
"""

import json
import sys
import threading
from concurrent import futures

import grpc
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

_write_lock = threading.Lock()


def _emit(signal: str, resource, names: list[str]) -> None:
    instance_id = next(
        (a.value.string_value for a in resource.attributes if a.key == "service.instance.id"),
        "",
    )
    line = json.dumps({"signal": signal, "instance_id": instance_id, "names": names})
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class _Logs(logs_service_pb2_grpc.LogsServiceServicer):
    def Export(self, request, context):
        for rl in request.resource_logs:
            _emit("logs", rl.resource, [r.body.string_value for s in rl.scope_logs for r in s.log_records])
        return logs_service_pb2.ExportLogsServiceResponse()


class _Traces(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context):
        for rs in request.resource_spans:
            _emit("traces", rs.resource, [span.name for s in rs.scope_spans for span in s.spans])
        return trace_service_pb2.ExportTraceServiceResponse()


class _Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
    def Export(self, request, context):
        for rm in request.resource_metrics:
            _emit("metrics", rm.resource, [m.name for s in rm.scope_metrics for m in s.metrics])
        return metrics_service_pb2.ExportMetricsServiceResponse()


def main() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(_Logs(), server)
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_Traces(), server)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(_Metrics(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    with _write_lock:
        sys.stdout.write(json.dumps({"port": port}) + "\n")
        sys.stdout.flush()
    sys.stdin.read()  # returns when the test closes stdin or exits
    server.stop(0)


if __name__ == "__main__":
    main()
