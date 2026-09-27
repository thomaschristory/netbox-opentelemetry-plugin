# Collector on Kubernetes

## Why a Collector

The plugin can point `exporter.endpoint` (or `OTEL_EXPORTER_OTLP_ENDPOINT`) at any OTLP-compatible endpoint, including a backend's own native OTLP receiver directly; nothing in the plugin requires a Collector in between. Routing through an OpenTelemetry Collector instead is a recommendation, not a requirement, because it keeps a few things out of NetBox:

- Backend credentials live in the Collector's own configuration (and, on Kubernetes, a Secret), not in `PLUGINS_CONFIG` or in a NetBox pod's environment.
- Retries against a slow or unreachable backend are the Collector's problem, with its own queues and backoff, not something the plugin has to implement per signal.
- Routing (splitting signals across backends, duplicating to more than one, sampling with a processor such as `tail_sampling` or `probabilistic_sampler`, or filtering with the `filter` processor) is configured once, in the Collector, without touching NetBox.

## The NetBox side

Point the plugin at the Collector with `exporter.endpoint` in `PLUGINS_CONFIG`, or the equivalent `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable, on both the NetBox web Deployment and the RQ worker Deployment:

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://otel-collector.<namespace>.svc:4318"},
    },
}
```

or, as an environment variable on both Deployments:

```yaml
env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-collector.<namespace>.svc:4318"
```

See [Configuration](configuration.md#endpoints) for how each signal resolves its own endpoint from this base, and [Installation](installation.md) for the full plugin setup.

## Example files

Two files: a standalone Collector configuration, and a set of Kubernetes manifests that embed the same configuration in a `ConfigMap`. Both were validated once, manually, against the pinned Collector image and Kubernetes version: `otelcol-contrib validate --config` for the standalone configuration, `kubeconform -strict` for the manifests. Neither runs in CI (both need `docker run` against a pinned image, which the CI jobs in this repository do not do); rerun them by hand after changing either file. A test in this repository (`tests/test_docs.py`) does run in CI and asserts that the `ConfigMap`'s embedded copy parses to the same YAML document as the standalone file (structural equality after `yaml.safe_load`, not a byte-for-byte comparison, so the two can differ in comments or formatting without failing the test), so the two cannot drift apart in substance.

`docs/examples/kubernetes/collector-config.yaml`:

```yaml
--8<-- "docs/examples/kubernetes/collector-config.yaml"
```

`docs/examples/kubernetes/collector.yaml`:

```yaml
--8<-- "docs/examples/kubernetes/collector.yaml"
```

Replace the placeholder backend (`https://otlp.example.com`), its header, and the `BACKEND_TOKEN` environment variable it reads from, with whatever your actual backend needs; some backends use a different header name, or none at all. Create the `otel-collector-backend` Secret referenced by the Deployment separately, for example with `kubectl create secret generic otel-collector-backend --from-literal=BACKEND_TOKEN=...`; this example does not create it, since a Secret's value does not belong in a file checked into version control.

## OpenShift notes

- The Deployment sets no `runAsUser`. OpenShift's restricted SCC assigns a UID from the namespace's allowed range at admission time, which this example is written to accept rather than fight; setting an explicit `runAsUser` would need a SCC that allows it. On plain Kubernetes, with no SCC involved, the container runs as whatever UID the image itself declares: `docker image inspect otel/opentelemetry-collector-contrib:0.161.0` shows UID `10001`, not root (the dev compose stack in this repository overrides that to run as root for its own reasons; this example does not). `runAsNonRoot: true` is still worth keeping regardless of which UID ends up in effect: it fails the pod at admission if a future image change (or a different image entirely) ever declares a root user, rather than silently running as root.
- No `Route` is included, and none is needed for this setup: NetBox reaches the Collector through the in-cluster `Service` (`otel-collector.<namespace>.svc`, ports 4317 and 4318), never from outside the cluster. Add a `Route` only if something outside the cluster, such as a second Collector forwarding to this one, needs to reach it directly.

## Sidecar alternative

Instead of a standalone Deployment reached over the `Service`, the [OpenTelemetry Operator](https://github.com/open-telemetry/opentelemetry-operator) can inject a Collector as a sidecar container into the NetBox pod itself, using an `OpenTelemetryCollector` resource with `spec.mode: sidecar`. The pod (the NetBox Deployment's pod template, not the Deployment's own metadata) is annotated `sidecar.opentelemetry.io/inject: "true"` (verified against the Operator's own sidecar injection documentation); the Operator then injects the Collector container alongside NetBox in every pod matching that annotation. With a sidecar, the endpoint is `http://localhost:4318` (or `4317` for gRPC), since the Collector runs in the same pod network namespace as NetBox rather than behind a separate `Service`.

A sidecar gives every NetBox pod its own Collector instance rather than sharing one Deployment; consider the trade-off in resource overhead per pod against the isolation and per-pod queuing it buys, before choosing it over the standalone Deployment above.

## What a Collector adds, that the plugin does not

- **Host metrics.** The plugin's own runtime metrics (`metrics.runtime`) are process-level only (see [Metrics](signals/metrics.md#runtime-metrics)); host-wide `system.*` metrics (CPU, memory, disk, network of the node or container itself) are not something the plugin collects, by design, since every NetBox process on a host would report the same values. Add the Collector's own `hostmetrics` receiver (or, on Kubernetes, a `kubeletstats` or node-level Collector) if you need those.
- **Body size limits.** The OTLP HTTP and gRPC receivers, and most backends behind them, enforce a maximum request body size. A batch that exceeds it is rejected whole, not partially accepted; this matters most for audit records with `audit.include_data` on, which can be large per record (see [Audit records](signals/audit.md#sizing) and [Data safety](data-safety.md)). Size the `batch` processor's batch size, and any receiver or backend limit, with that in mind, or keep `include_data` off and rely on `exclude_fields` instead.
- **`include_data` is a NetBox-side setting, not a Collector one.** The Collector has no way to reconstruct data the plugin never sent; there is no processor that adds it back. Filtering happens once, in the plugin, before export (see [Data safety](data-safety.md)).
