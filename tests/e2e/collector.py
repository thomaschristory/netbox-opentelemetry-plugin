"""Helpers shared by the e2e tests: log in to NetBox and read the Collector's JSON log output."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import requests

LOGS_FILE = Path(__file__).resolve().parents[2] / "dev" / "data" / "collector" / "logs.json"


def login(base_url: str, username: str, password: str) -> None:
    session = requests.Session()
    page = session.get(f"{base_url}/login/", timeout=10)
    page.raise_for_status()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
    response = session.post(
        f"{base_url}/login/",
        data={"csrfmiddlewaretoken": token, "username": username, "password": password, "next": "/"},
        headers={"Referer": f"{base_url}/login/"},
        timeout=10,
    )
    response.raise_for_status()


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


def record_time_ns(record: dict) -> int:
    time_ns = int(record.get("timeUnixNano", "0") or "0")
    if time_ns:
        return time_ns
    return int(record.get("observedTimeUnixNano", "0") or "0")


def string_attr(resource: dict, key: str) -> str | None:
    value = resource.get(key)
    return None if value is None else value.get("stringValue")
