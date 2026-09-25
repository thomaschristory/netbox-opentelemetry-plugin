"""M3 acceptance: every exported log line of a script run in the RQ work-horse reaches the Collector,
including when the script raises. Needs `make dev` (web and worker)."""

import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.collector import log_records, string_attr
from tests.e2e.netbox_api import delete_token, ensure_script, provision_token, run_script, wait_for_job

pytestmark = pytest.mark.e2e

NETBOX_URL = "http://localhost:8000"
SCRIPT_FILE = Path(__file__).resolve().parents[2] / "dev" / "scripts" / "otel_demo.py"
# DEBUG is below the dev stack's export level (INFO), so "debug" is not expected.
EXPECTED_SUFFIXES = ("info", "success", "warning", "failure")


@pytest.fixture(scope="module")
def api():
    token_id, header = provision_token(NETBOX_URL, "admin", "admin")
    script = ensure_script(NETBOX_URL, header, SCRIPT_FILE, "otel_demo", "OtelDemo")
    yield header, script
    delete_token(NETBOX_URL, header, token_id)


def _body(record: dict) -> str:
    body = record.get("body")
    return body.get("stringValue", "") if isinstance(body, dict) else str(body or "")


def _records_with(marker: str):
    return [(resource, scope, record) for resource, scope, record in log_records() if marker in _body(record)]


def _wait_for_records(marker: str, minimum: int, timeout: float = 30):
    deadline = time.monotonic() + timeout
    found = []
    while time.monotonic() < deadline:
        found = _records_with(marker)
        if len(found) >= minimum:
            time.sleep(2)  # let stragglers (possible duplicates) arrive before asserting exact counts
            return _records_with(marker)
        time.sleep(1)
    return found


@pytest.mark.parametrize("fail", [False, True])
def test_script_log_lines_reach_collector_from_the_horse(api, fail):
    header, script = api
    marker = f"otel-e2e-{uuid.uuid4().hex[:12]}"
    job_id = run_script(NETBOX_URL, header, script, {"marker": marker, "fail": fail})
    job = wait_for_job(NETBOX_URL, header, job_id)
    assert job["status"]["value"] == ("errored" if fail else "completed")

    minimum = len(EXPECTED_SUFFIXES) + (1 if fail else 0)
    records = _wait_for_records(marker, minimum)
    script_records = [r for r in records if str(r[1].get("name", "")).startswith("netbox.scripts.")]
    bodies = sorted(_body(record) for _, _, record in script_records if "boom" not in _body(record))

    assert bodies == sorted(f"{marker} {suffix}" for suffix in EXPECTED_SUFFIXES)
    roles = {string_attr(resource, "netbox.process.role") for resource, _, _ in script_records}
    assert roles == {"rq_horse"}
    instance_ids = {string_attr(resource, "service.instance.id") for resource, _, _ in script_records}
    assert len(instance_ids) == 1
    if fail:
        errors = [record for _, _, record in records if f"{marker} boom" in _body(record)]
        assert errors, "the script's exception was not exported"
        assert all(record.get("severityText") == "ERROR" for record in errors)
