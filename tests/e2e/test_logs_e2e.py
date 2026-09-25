"""M1 acceptance: a login produces a log record in the Collector. Needs `make dev` running."""

import time

import pytest

from tests.e2e.collector import LOGS_FILE, log_records, login, record_time_ns, string_attr

pytestmark = pytest.mark.e2e

NETBOX_URL = "http://localhost:8000"


def _wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in log_records():
            if predicate(item):
                return item
        time.sleep(1)
    pytest.fail(f"no matching log record in {LOGS_FILE} within {timeout}s")


def test_login_produces_log_record():
    # 5 s margin covers host/VM clock skew between this process and the collector.
    start_ns = time.time_ns() - 5_000_000_000
    login(NETBOX_URL, "admin", "admin")

    resource, scope, record = _wait_for(
        lambda item: (
            "successfully authenticated" in str(item[2].get("body"))
            and record_time_ns(item[2]) >= start_ns
            and string_attr(item[0], "netbox.dev.server") == "granian"
        )
    )
    assert record["severityText"] == "INFO"
    assert scope["name"] == "netbox.auth.login"
    assert resource["service.name"]["stringValue"] == "netbox"
    assert resource["netbox.process.role"]["stringValue"] == "web"
    assert resource["deployment.environment.name"]["stringValue"] == "dev"
    attribute_keys = {a["key"] for a in record.get("attributes", [])}
    assert {"code.file.path", "code.function.name", "code.line.number", "logger.name"} <= attribute_keys
