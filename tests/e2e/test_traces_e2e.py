"""M5 acceptance against the dev stack (`make dev`, traces enabled in dev/configuration/plugins.py):
API requests produce spans, logs and audit records carry the request's trace id, and a webhook fired
by an edit shares that edit's trace. The requests carry their own traceparent, so each test knows
its trace id."""

import contextlib
import json
import secrets
import time
import uuid

import pytest
import requests

from tests.e2e.collector import log_records, record_attr, session_login, spans, string_attr
from tests.e2e.netbox_api import _headers, delete_token, provision_token

pytestmark = pytest.mark.e2e

NETBOX_URL = "http://localhost:8000"
SECRET = "otel-e2e-secret"
SPAN_KIND_SERVER, SPAN_KIND_CLIENT, SPAN_KIND_CONSUMER = 2, 3, 5


@pytest.fixture(scope="module")
def header():
    token_id, value = provision_token(NETBOX_URL, "admin", "admin")
    yield value
    delete_token(NETBOX_URL, value, token_id)


def _traceparent():
    trace_id = secrets.token_hex(16)
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-01"


def _call(method, path, header, traceparent=None, **kwargs):
    headers = _headers(header)
    if traceparent:
        headers["traceparent"] = traceparent
    response = requests.request(method, f"{NETBOX_URL}{path}", headers=headers, timeout=15, **kwargs)
    response.raise_for_status()
    return response


def _trace(trace_id):
    return [(res, scope, span) for res, scope, span in spans() if span.get("traceId") == trace_id]


def _wait(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(1)
    return predicate()


def _assert_no_secret(trace):
    assert SECRET not in json.dumps([span for _, _, span in trace])


def _safe_delete(path, header):
    """Best-effort cleanup: one failing delete must not stop the others from running."""
    with contextlib.suppress(requests.RequestException):
        _call("DELETE", path, header)


def test_api_request_produces_a_server_span_with_request_id_and_user(header):
    trace_id, traceparent = _traceparent()
    response = _call("GET", f"/api/dcim/sites/?q={SECRET}", header, traceparent)
    request_id = response.headers["X-Request-ID"]

    def server():
        return [t for t in _trace(trace_id) if t[2]["kind"] == SPAN_KIND_SERVER]

    found = _wait(server)
    assert len(found) == 1
    resource, _, span = found[0]
    assert span["name"].startswith("GET api/dcim/sites")
    assert record_attr(span, "netbox.request_id") == request_id
    assert record_attr(span, "enduser.id") == "admin"
    assert string_attr(resource, "netbox.process.role") == "web"
    trace = _wait(lambda: [t for t in _trace(trace_id) if t[2]["kind"] == SPAN_KIND_CLIENT])
    assert any(record_attr(span, "db.system") == "postgresql" for _, _, span in trace)
    _assert_no_secret(_trace(trace_id))


def test_audit_and_log_records_carry_the_request_trace_id(header):
    trace_id, traceparent = _traceparent()
    prefix = f"10.{secrets.randbelow(200) + 20}.{secrets.randbelow(250)}.0/24"
    response = _call("POST", "/api/ipam/prefixes/", header, traceparent, json={"prefix": prefix})
    request_id = response.headers["X-Request-ID"]
    try:

        def records():
            return [r for _, _, r in log_records() if r.get("traceId") == trace_id]

        def ready():
            current = records()
            has_audit = any(record_attr(r, "netbox.change.request_id") == request_id for r in current)
            has_info = any("Creating new prefix" in str(r.get("body")) for r in current)
            return current if has_audit and has_info else None

        # Waits for both expected records by content, not just a count: the audit record and the
        # API view's INFO line are exported independently and can arrive in either order.
        found = _wait(ready)
        assert found, "expected both an audit record and an API view log line for this request"
        audit = [r for r in found if record_attr(r, "netbox.change.request_id") == request_id]
        assert len(audit) == 1
        created = [r for r in found if "Creating new prefix" in str(r.get("body"))]
        assert created, "the API view's INFO line should carry the request's trace id"

        def server():
            return [t for t in _trace(trace_id) if t[2]["kind"] == SPAN_KIND_SERVER]

        # The span batch processor's export interval is longer than the log one, so the request's
        # span can still be unflushed when its log records have already arrived.
        found_server = _wait(server)
        assert found_server and audit[0].get("spanId") == found_server[0][2]["spanId"]
    finally:
        _call("DELETE", f"/api/ipam/prefixes/{response.json()['id']}/", header)


def test_django_request_404_record_carries_the_server_span_ids(header):
    # Django logs "Not Found: ..." to django.request after the request's span has ended; the record
    # still carries that span's trace id and span id. Sent directly: _call raises on the 404.
    trace_id, traceparent = _traceparent()
    headers = {**_headers(header), "traceparent": traceparent}
    response = requests.get(f"{NETBOX_URL}/api/dcim/devices/2147483647/", headers=headers, timeout=15)
    assert response.status_code == 404

    def not_found():
        return [
            r
            for _, _, r in log_records()
            if r.get("traceId") == trace_id
            and r.get("body", {}).get("stringValue", "").startswith("Not Found: /api/dcim/devices/")
        ]

    found = _wait(not_found)
    assert len(found) == 1, "expected one django.request record carrying the request's trace id"

    def server():
        return [t for t in _trace(trace_id) if t[2]["kind"] == SPAN_KIND_SERVER]

    found_server = _wait(server)
    assert found_server and found[0].get("spanId") == found_server[0][2]["spanId"]


def test_webhook_fired_by_an_edit_shares_the_edit_trace(header):
    suffix = uuid.uuid4().hex[:8]
    webhook = rule = prefix = None
    try:
        webhook = _call(
            "POST",
            "/api/extras/webhooks/",
            header,
            json={
                "name": f"otel-e2e-{suffix}",
                "payload_url": f"http://webhook-sink:8080/netbox?token={SECRET}",
                "http_method": "POST",
                "http_content_type": "application/json",
            },
        ).json()
        rule = _call(
            "POST",
            "/api/extras/event-rules/",
            header,
            json={
                "name": f"otel-e2e-{suffix}",
                "object_types": ["ipam.prefix"],
                "event_types": ["object_updated"],
                "action_type": "webhook",
                "action_object_type": "extras.webhook",
                "action_object_id": webhook["id"],
            },
        ).json()
        prefix = _call(
            "POST",
            "/api/ipam/prefixes/",
            header,
            json={"prefix": f"10.{secrets.randbelow(200) + 20}.{secrets.randbelow(250)}.0/24"},
        ).json()
        trace_id, traceparent = _traceparent()
        _call("PATCH", f"/api/ipam/prefixes/{prefix['id']}/", header, traceparent, json={"description": suffix})

        def job_and_call():
            trace = _trace(trace_id)
            # NetBox enqueues more than the webhook job from the same request (for example its own
            # search-cache-update job); select the webhook job span by name rather than assuming it
            # is the only CONSUMER span in the trace.
            jobs = [
                t
                for t in trace
                if t[2]["kind"] == SPAN_KIND_CONSUMER and t[2]["name"] == "rq.job extras.webhooks.send_webhook"
            ]
            calls = [t for t in trace if t[2]["kind"] == SPAN_KIND_CLIENT and record_attr(t[2], "url.full")]
            return (jobs, calls) if jobs and calls else None

        found = _wait(job_and_call, timeout=90)
        assert found, "no job span and outbound call in the edit's trace"
        jobs, calls = found
        ((job_resource, _, job_span),) = jobs
        assert job_span["name"] == "rq.job extras.webhooks.send_webhook"
        assert string_attr(job_resource, "netbox.process.role") == "rq_horse"
        assert record_attr(job_span, "messaging.system") == "rq"

        def server():
            return [t for t in _trace(trace_id) if t[2]["kind"] == SPAN_KIND_SERVER]

        # The web process flushes its span batch on its own schedule, slower than the rq_horse's
        # flush-on-exit, so the job span above can be visible before the request's own span is.
        found_server = _wait(server)
        assert found_server and job_span["parentSpanId"] == found_server[0][2]["spanId"]
        ((_, _, call),) = calls
        assert call["parentSpanId"] == job_span["spanId"]
        assert record_attr(call, "url.full") == "http://webhook-sink:8080/netbox?REDACTED"
        assert record_attr(call, "http.response.status_code") == 204
        _assert_no_secret(_trace(trace_id))
    finally:
        # Clean up only what was actually created, in reverse order, and don't let one failing
        # delete skip the others (a partial failure above must not leak every prior object).
        if prefix is not None:
            _safe_delete(f"/api/ipam/prefixes/{prefix['id']}/", header)
        if rule is not None:
            _safe_delete(f"/api/extras/event-rules/{rule['id']}/", header)
        if webhook is not None:
            _safe_delete(f"/api/extras/webhooks/{webhook['id']}/", header)


def test_dashboard_feed_fetch_carries_the_request_trace_but_not_its_baggage(header):
    # The sink echoes the traceparent trace id and the baggage it was fetched with into the feed's
    # item title, which the RSS widget renders on the home page.
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
                    "config": {"feed_url": feed, "max_entries": 1, "cache_timeout": 600, "request_timeout": 3},
                }
            },
        },
    )
    try:
        trace_id, traceparent = _traceparent()
        page = session.get(
            f"{NETBOX_URL}/",
            headers={"traceparent": traceparent, "baggage": f"leak={SECRET}"},
            timeout=15,
        )
        page.raise_for_status()
        assert "otel-probe" in page.text, "the RSS widget did not render the sink's feed"
        assert f"tp={trace_id}" in page.text
        assert "bag=none" in page.text
        assert SECRET not in page.text
    finally:
        restore = {"layout": original["layout"], "config": original["config"]}
        _call("PATCH", "/api/extras/dashboard/", header, json=restore)
