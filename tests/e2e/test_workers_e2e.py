"""M2 acceptance: every worker exports with its own identity, nothing is duplicated or lost.

Needs `make dev` (Granian); the gunicorn and uWSGI cases also need `make dev-gunicorn` and
`make dev-uwsgi`. A server that is not running is skipped.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from tests.e2e.collector import log_records, login, record_time_ns, string_attr

pytestmark = pytest.mark.e2e

SERVERS = {
    "granian": "http://localhost:8000",
    "gunicorn": "http://localhost:8001",
    "uwsgi": "http://localhost:8002",
}
LOGINS = 12


def _reachable(base_url: str) -> bool:
    try:
        return requests.get(f"{base_url}/login/", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def _login_records(server: str, start_ns: int):
    return [
        (resource, record)
        for resource, _, record in log_records()
        if "successfully authenticated" in str(record.get("body"))
        and string_attr(resource, "netbox.dev.server") == server
        and record_time_ns(record) >= start_ns
    ]


@pytest.mark.parametrize("server", list(SERVERS))
def test_each_worker_exports_with_its_own_identity(server):
    base_url = SERVERS[server]
    if not _reachable(base_url):
        pytest.skip(f"{server} is not running (see Makefile dev targets)")

    # 5 s margin covers host/VM clock skew between this process and the collector.
    start_ns = time.time_ns() - 5_000_000_000
    # Concurrent logins so that more than one worker process has to serve them.
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: login(base_url, "admin", "admin"), range(LOGINS)))

    deadline = time.monotonic() + 30
    records = []
    while time.monotonic() < deadline:
        records = _login_records(server, start_ns)
        if len(records) >= LOGINS:
            break
        time.sleep(1)

    assert len(records) == LOGINS, f"expected {LOGINS} login records from {server}, got {len(records)}"
    instance_ids = {string_attr(resource, "service.instance.id") for resource, _ in records}
    assert len(instance_ids) >= 2, f"all records came from one worker: {instance_ids}"
    roles = {string_attr(resource, "netbox.process.role") for resource, _ in records}
    assert roles == {"web"}
