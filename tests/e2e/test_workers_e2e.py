"""M2 acceptance: every worker exports with its own identity, nothing is duplicated or lost.

Needs `make dev` (Granian); the gunicorn and uWSGI cases also need `make dev-gunicorn` and
`make dev-uwsgi`. A server that is not running is skipped.

The Collector's log file is append-only and shared across test runs, so this test does not just
count matching records in a trailing time window (a previous run's login can still be inside that
window). Instead it snapshots the matching records already present (after waiting for that set to
go quiet) and then counts only the records that appear on top of that baseline once this test's
own logins are sent.
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


def _key(resource: dict, record: dict):
    return (record.get("timeUnixNano"), string_attr(resource, "service.instance.id"), str(record.get("body")))


def _wait_for_quiescence(server: str, start_ns: int, *, cap: float = 20, quiet_for: float = 3) -> set:
    """Poll roughly every second until the matching key set is unchanged for `quiet_for` seconds
    in a row, capped at `cap` seconds total. Returns the key set at that point (or at the cap)."""
    deadline = time.monotonic() + cap
    keys = {_key(resource, record) for resource, record in _login_records(server, start_ns)}
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(1)
        current = {_key(resource, record) for resource, record in _login_records(server, start_ns)}
        if current != keys:
            keys = current
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= quiet_for:
            break
    return keys


@pytest.mark.parametrize("server", list(SERVERS))
def test_each_worker_exports_with_its_own_identity(server):
    base_url = SERVERS[server]
    if not _reachable(base_url):
        pytest.skip(f"{server} is not running (see Makefile dev targets)")

    # 5 s margin covers host/VM clock skew between this process and the collector; also bounds the scan.
    start_ns = time.time_ns() - 5_000_000_000

    baseline = _wait_for_quiescence(server, start_ns)

    # Concurrent logins so that more than one worker process has to serve them.
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: login(base_url, "admin", "admin"), range(LOGINS)))

    deadline = time.monotonic() + 30
    new_records = []
    while time.monotonic() < deadline:
        records = _login_records(server, start_ns)
        new_records = [(resource, record) for resource, record in records if _key(resource, record) not in baseline]
        if len(new_records) >= LOGINS:
            break
        time.sleep(1)

    assert len(new_records) == LOGINS, f"expected {LOGINS} new login records from {server}, got {len(new_records)}"
    instance_ids = {string_attr(resource, "service.instance.id") for resource, _ in new_records}
    assert len(instance_ids) >= 2, f"all records came from one worker: {instance_ids}"
    roles = {string_attr(resource, "netbox.process.role") for resource, _ in new_records}
    assert roles == {"web"}
