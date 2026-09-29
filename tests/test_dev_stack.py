"""The dev stack's `ui` profile: grafana/otel-lgtm and the Collector overlay that forwards to it.

The default stack must stay as it is: without the profile, the Collector has no exporter pointing
at otel-lgtm, so it neither fails nor logs export errors when otel-lgtm is not running.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "dev"
LGTM_EXPORTER = "otlp_grpc/lgtm"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _merge(base, overlay):
    """The Collector's merge of two --config files: maps merge key by key, anything else replaces."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _merge(base[key], value) if key in base else value
        return merged
    return overlay


def _make_dry_run(*args: str) -> str:
    if shutil.which("make") is None:
        pytest.skip("make is not installed")
    result = subprocess.run(["make", "-n", "-s", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout


def test_default_collector_config_does_not_reference_lgtm():
    config = _load(DEV / "otelcol" / "config.yaml")
    assert "lgtm" not in yaml.safe_dump(config)


def test_ui_overlay_adds_lgtm_to_every_pipeline_and_keeps_the_rest():
    base = _load(DEV / "otelcol" / "config.yaml")
    merged = _merge(base, _load(DEV / "otelcol" / "ui.yaml"))

    assert merged["exporters"][LGTM_EXPORTER]["endpoint"] == "otel-lgtm:4317"
    assert set(merged["service"]["pipelines"]) == {"logs", "traces", "metrics"}
    for name, pipeline in merged["service"]["pipelines"].items():
        base_pipeline = base["service"]["pipelines"][name]
        assert pipeline["exporters"] == [*base_pipeline["exporters"], LGTM_EXPORTER]
        assert pipeline["receivers"] == base_pipeline["receivers"]
        assert pipeline["processors"] == base_pipeline["processors"]
        assert set(pipeline["exporters"]) <= set(merged["exporters"])


def test_otel_lgtm_service_is_pinned_and_only_in_the_ui_profile():
    services = _load(DEV / "docker-compose.yml")["services"]
    lgtm = services["otel-lgtm"]

    assert lgtm["profiles"] == ["ui"]
    assert re.fullmatch(r"docker\.io/grafana/otel-lgtm:\d+\.\d+\.\d+", lgtm["image"])
    # Anonymous Admin access, so Grafana is published on the loopback interface only, and only
    # Grafana: the Collector already publishes 4317 and 4318 on the host.
    assert lgtm["ports"] == ["127.0.0.1:3000:3000"]
    assert "otel-lgtm" not in services["otel-collector"].get("depends_on", {})


def test_ui_override_loads_the_overlay_in_the_collector():
    collector = _load(DEV / "docker-compose.ui.yml")["services"]["otel-collector"]

    assert collector["command"] == [
        "--config=/etc/otelcol/config.yaml",
        "--config=/etc/otelcol/ui.yaml",
    ]
    assert "./otelcol/ui.yaml:/etc/otelcol/ui.yaml:ro" in collector["volumes"]
    assert collector["depends_on"]["otel-lgtm"]["condition"] == "service_healthy"


def test_make_dev_does_not_use_the_ui_profile():
    command = _make_dry_run("dev")
    assert "docker-compose.ui.yml" not in command
    assert "--profile ui" not in command


@pytest.mark.parametrize(
    ("target", "profile"),
    [("dev-ui", None), ("dev-gunicorn UI=1", "gunicorn"), ("dev-uwsgi UI=1", "uwsgi")],
)
def test_make_ui_targets_use_the_override_and_the_profile(target, profile):
    command = _make_dry_run(*target.split())
    assert "-f dev/docker-compose.yml -f dev/docker-compose.ui.yml" in command
    assert "--profile ui" in command
    if profile:
        assert f"--profile {profile}" in command


def test_make_down_stops_the_ui_profile():
    assert "--profile ui" in _make_dry_run("down")
