"""M6 acceptance against the dev stack (`make dev`, metrics on with a 5 s export interval in
dev/configuration/plugins.py): every SPEC 6.4 metric reaches the Collector, job metrics come from the
worker parent, a work-horse exports nothing, and only allowlisted names and attributes are exported."""

import fnmatch
import secrets
import time
import uuid
from pathlib import Path

import pytest
import requests

from netbox_opentelemetry_plugin.otel import METRIC_ALLOWLIST
from tests.e2e.collector import metric_points, point_value, record_attr, session_login, string_attr
from tests.e2e.netbox_api import _headers, delete_token, ensure_script, provision_token, run_script, wait_for_job

pytestmark = pytest.mark.e2e

NETBOX_URL = "http://localhost:8000"
SCRIPT_FILE = Path(__file__).resolve().parents[2] / "dev" / "scripts" / "otel_demo.py"


@pytest.fixture(scope="module")
def header():
    token_id, value = provision_token(NETBOX_URL, "admin", "admin")
    yield value
    delete_token(NETBOX_URL, value, token_id)


def _call(method, path, header, **kwargs):
    response = requests.request(method, f"{NETBOX_URL}{path}", headers=_headers(header), timeout=15, **kwargs)
    response.raise_for_status()
    return response


def _wait(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(1)
    return predicate()


def _points(name, role=None, **attributes):
    for resource, _, metric, point in metric_points():
        if metric["name"] != name:
            continue
        if role is not None and string_attr(resource, "netbox.process.role") != role:
            continue
        if all(record_attr(point, k) == v for k, v in attributes.items()):
            yield resource, point


def _total(name, **attributes):
    """Sum over processes of each series' latest cumulative value (max, since values only grow).

    The series is also keyed on its start time, so a counter that starts again from zero under the
    same service.instance.id would still be counted separately.
    """
    latest = {}
    for resource, point in _points(name, **attributes):
        key = (
            string_attr(resource, "service.instance.id"),
            point.get("startTimeUnixNano"),
            tuple(sorted((a["key"], str(a["value"])) for a in point.get("attributes", []))),
        )
        latest[key] = max(latest.get(key, 0), point_value(point))
    return sum(latest.values())


def test_http_server_duration_from_the_web_process(header):
    _call("GET", "/api/dcim/sites/", header)

    def found():
        return [
            p
            for _, p in _points("http.server.request.duration", role="web")
            if str(record_attr(p, "http.route") or "").startswith("api/dcim/sites")
        ]

    assert _wait(found)


def test_http_client_duration_from_the_web_process(header):
    feed = f"http://webhook-sink:8080/feed?run={uuid.uuid4().hex}"
    session = session_login(NETBOX_URL, "admin", "admin")
    session.get(f"{NETBOX_URL}/", timeout=15).raise_for_status()  # creates the user's dashboard
    widget = str(uuid.uuid4())
    original = _call("GET", "/api/extras/dashboard/", header).json()
    _call(
        "PATCH",
        "/api/extras/dashboard/",
        header,
        json={
            "layout": [{"id": widget, "x": 0, "y": 0, "w": 4, "h": 3}],
            "config": {
                widget: {
                    "class": "extras.RSSFeedWidget",
                    "title": "otel",
                    "config": {"feed_url": feed, "max_entries": 1, "cache_timeout": 60, "request_timeout": 3},
                }
            },
        },
    )
    try:
        session.get(f"{NETBOX_URL}/", timeout=15).raise_for_status()  # the web process fetches the feed
        assert _wait(
            lambda: list(_points("http.client.request.duration", role="web", **{"server.address": "webhook-sink"}))
        )
    finally:
        restore = {"layout": original["layout"], "config": original["config"]}
        _call("PATCH", "/api/extras/dashboard/", header, json=restore)


def test_job_metrics_come_from_the_worker_parent(header):
    script = ensure_script(NETBOX_URL, header, SCRIPT_FILE, "otel_demo", "OtelDemo")
    before = _total("netbox.rq.jobs", **{"netbox.rq.job.outcome": "finished"})
    job_id = run_script(NETBOX_URL, header, script, {"marker": f"metrics-{secrets.token_hex(4)}", "fail": False})
    wait_for_job(NETBOX_URL, header, job_id)
    assert _wait(lambda: _total("netbox.rq.jobs", **{"netbox.rq.job.outcome": "finished"}) > before, timeout=60)
    functions = {record_attr(p, "code.function.name") for _, p in _points("netbox.rq.jobs", role="rqworker")}
    assert any(f and f.endswith(".handle") and "." in f.removesuffix(".handle") for f in functions), functions
    assert list(_points("netbox.rq.job.duration", role="rqworker"))


def test_queue_depth_is_reported_per_queue_by_the_worker():
    def names():
        return {
            record_attr(p, "messaging.destination.name") for _, p in _points("netbox.rq.queue.depth", role="rqworker")
        }

    assert _wait(lambda: {"high", "default", "low"} <= names())


def test_committed_changes_are_counted(header):
    attributes = {"netbox.change.action": "create", "netbox.change.object_type": "ipam.prefix"}
    before = _total("netbox.object_changes", **attributes)
    prefix = _call(
        "POST",
        "/api/ipam/prefixes/",
        header,
        json={"prefix": f"10.{secrets.randbelow(200) + 20}.{secrets.randbelow(250)}.0/24"},
    ).json()
    try:
        assert _wait(lambda: _total("netbox.object_changes", **attributes) > before)
    finally:
        _call("DELETE", f"/api/ipam/prefixes/{prefix['id']}/", header)


def test_runtime_metrics_from_web_and_worker():
    def roles():
        return {string_attr(r, "netbox.process.role") for r, _ in _points("process.cpu.time")}

    assert _wait(lambda: {"web", "rqworker"} <= roles())


def test_only_allowlisted_metrics_and_attributes_and_nothing_from_a_horse():
    seen = False
    for resource, _, metric, point in metric_points():
        seen = True
        name = metric["name"]
        assert string_attr(resource, "netbox.process.role") != "rq_horse", name
        matches = [pattern for pattern in METRIC_ALLOWLIST if fnmatch.fnmatchcase(name, pattern)]
        assert matches, f"{name} is not allowlisted"
        keys = METRIC_ALLOWLIST[matches[0]]
        if keys is not None:
            assert {a["key"] for a in point.get("attributes", [])} <= keys, name
    assert seen
