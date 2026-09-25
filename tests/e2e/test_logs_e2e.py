"""M1 acceptance: a login produces a log record in the Collector. Needs `make dev` running."""

import json
import re
import time
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.e2e

NETBOX_URL = "http://localhost:8000"
LOGS_FILE = Path(__file__).resolve().parents[2] / "dev" / "data" / "collector" / "logs.json"


def _login(username: str, password: str) -> None:
    session = requests.Session()
    page = session.get(f"{NETBOX_URL}/login/", timeout=10)
    page.raise_for_status()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
    response = session.post(
        f"{NETBOX_URL}/login/",
        data={"csrfmiddlewaretoken": token, "username": username, "password": password, "next": "/"},
        headers={"Referer": f"{NETBOX_URL}/login/"},
        timeout=10,
    )
    response.raise_for_status()


def _log_records():
    if not LOGS_FILE.exists():
        return
    for line in LOGS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        for resource_logs in json.loads(line).get("resourceLogs", []):
            resource = {a["key"]: a["value"] for a in resource_logs.get("resource", {}).get("attributes", [])}
            for scope_logs in resource_logs.get("scopeLogs", []):
                for record in scope_logs.get("logRecords", []):
                    yield resource, scope_logs.get("scope", {}), record


def _wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in _log_records():
            if predicate(item):
                return item
        time.sleep(1)
    pytest.fail(f"no matching log record in {LOGS_FILE} within {timeout}s")


def test_login_produces_log_record():
    _login("admin", "admin")

    resource, scope, record = _wait_for(lambda item: "successfully authenticated" in str(item[2].get("body")))
    assert record["severityText"] == "INFO"
    assert scope["name"] == "netbox.auth.login"
    assert resource["service.name"]["stringValue"] == "netbox"
    assert resource["netbox.process.role"]["stringValue"] == "web"
    assert resource["deployment.environment.name"]["stringValue"] == "dev"
    attribute_keys = {a["key"] for a in record.get("attributes", [])}
    assert {"code.file.path", "code.function.name", "code.line.number", "logger.name"} <= attribute_keys
