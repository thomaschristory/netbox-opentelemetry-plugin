"""Helpers shared by the e2e tests: log in to NetBox and read the Collector's JSON log output."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path

import requests

LOGS_FILE = Path(__file__).resolve().parents[2] / "dev" / "data" / "collector" / "logs.json"


def _request_with_retry(method, url: str, **kwargs) -> requests.Response:
    # The dev gunicorn/uWSGI servers run with --max-requests, so a worker can be mid-respawn when
    # a new connection lands on the shared listening socket: the OS hands the connection to the
    # exiting worker, which closes it without answering. That surfaces as a ConnectionError with
    # no response at all (distinct from an actual application error, which we don't want to mask).
    # One short retry rides out the respawn window.
    for attempt in range(3):
        try:
            return method(url, **kwargs)
        except requests.exceptions.ConnectionError:
            if attempt == 2:
                raise
            time.sleep(0.5)


def login(base_url: str, username: str, password: str) -> None:
    session = requests.Session()
    # uWSGI's --http-socket answers HTTP/1.1 without Connection: close and then closes the
    # socket anyway; without this header the POST can reuse the now-dead connection and fail
    # with a transient RemoteDisconnected.
    page = _request_with_retry(session.get, f"{base_url}/login/", headers={"Connection": "close"}, timeout=10)
    page.raise_for_status()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
    response = _request_with_retry(
        session.post,
        f"{base_url}/login/",
        data={"csrfmiddlewaretoken": token, "username": username, "password": password, "next": "/"},
        headers={"Referer": f"{base_url}/login/", "Connection": "close"},
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
