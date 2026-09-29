"""Helpers shared by the e2e tests: log in to NetBox and read the Collector's JSON log, trace and
metric output."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import requests

LOGS_FILE = Path(__file__).resolve().parents[2] / "dev" / "data" / "collector" / "logs.json"
TRACES_FILE = LOGS_FILE.parent / "traces.json"
METRICS_FILE = LOGS_FILE.parent / "metrics.json"


def login(base_url: str, username: str, password: str) -> None:
    session_login(base_url, username, password)


def session_login(base_url: str, username: str, password: str) -> requests.Session:
    """Like login(), but returns the authenticated session."""
    session = requests.Session()
    # Asks the server to close each connection after the response. The web servers of the dev
    # stack then send "Connection: close" back, so the client never reuses a connection the
    # server is closing.
    page = session.get(f"{base_url}/login/", headers={"Connection": "close"}, timeout=10)
    page.raise_for_status()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
    response = session.post(
        f"{base_url}/login/",
        data={"csrfmiddlewaretoken": token, "username": username, "password": password, "next": "/"},
        headers={"Referer": f"{base_url}/login/", "Connection": "close"},
        timeout=10,
    )
    response.raise_for_status()
    return session


def log_records() -> Iterator[tuple[dict, dict, dict]]:
    if not LOGS_FILE.exists():
        return
    for line in LOGS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # Tolerates a partially written last line (the file exporter may still be flushing it).
            continue
        for resource_logs in data.get("resourceLogs", []):
            resource = {a["key"]: a["value"] for a in resource_logs.get("resource", {}).get("attributes", [])}
            for scope_logs in resource_logs.get("scopeLogs", []):
                for record in scope_logs.get("logRecords", []):
                    yield resource, scope_logs.get("scope", {}), record


def spans() -> Iterator[tuple[dict, dict, dict]]:
    if not TRACES_FILE.exists():
        return
    for line in TRACES_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # Tolerates a partially written last line (the file exporter may still be flushing it).
            continue
        for resource_spans in data.get("resourceSpans", []):
            resource = {a["key"]: a["value"] for a in resource_spans.get("resource", {}).get("attributes", [])}
            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    yield resource, scope_spans.get("scope", {}), span


def metric_points() -> Iterator[tuple[dict, dict, dict, dict]]:
    if not METRICS_FILE.exists():
        return
    for line in METRICS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # Tolerates a partially written last line (the file exporter may still be flushing it).
            continue
        for resource_metrics in data.get("resourceMetrics", []):
            resource = {a["key"]: a["value"] for a in resource_metrics.get("resource", {}).get("attributes", [])}
            for scope_metrics in resource_metrics.get("scopeMetrics", []):
                for metric in scope_metrics.get("metrics", []):
                    for kind in ("sum", "gauge", "histogram"):
                        for point in metric.get(kind, {}).get("dataPoints", []):
                            yield resource, scope_metrics.get("scope", {}), metric, point


def point_value(point: dict) -> float:
    """A sum or gauge point's value, or a histogram point's count (OTLP JSON encodes 64-bit ints as strings)."""
    if "count" in point:
        return float(point["count"])
    if "asInt" in point:
        return float(point["asInt"])
    return float(point.get("asDouble", 0))


def record_time_ns(record: dict) -> int:
    time_ns = int(record.get("timeUnixNano", "0") or "0")
    if time_ns:
        return time_ns
    return int(record.get("observedTimeUnixNano", "0") or "0")


def string_attr(resource: dict, key: str) -> str | None:
    value = resource.get(key)
    return None if value is None else value.get("stringValue")


def record_attr(record: dict, key: str):
    """Return a log record attribute as a Python value (OTLP JSON encodes ints as strings)."""
    for attribute in record.get("attributes", []):
        if attribute["key"] == key:
            value = attribute["value"]
            if "intValue" in value:
                return int(value["intValue"])
            for kind in ("stringValue", "boolValue", "doubleValue"):
                if kind in value:
                    return value[kind]
    return None
