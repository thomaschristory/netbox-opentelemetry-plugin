"""M4 acceptance, live: prefix create, update and delete, and a bulk create of 10 prefixes, each produce
the expected audit records in the Collector. Needs `make dev`."""

import random
import time

import pytest
import requests

from tests.e2e.collector import log_records, record_attr
from tests.e2e.netbox_api import delete_token, provision_token

pytestmark = pytest.mark.e2e

NETBOX_URL = "http://localhost:8000"
AUDIT_SCOPE = "netbox_opentelemetry_plugin.audit"


@pytest.fixture(scope="module")
def header():
    token_id, value = provision_token(NETBOX_URL, "admin", "admin")
    yield value
    delete_token(NETBOX_URL, value, token_id)


def _api(header, method, path, **kwargs):
    headers = {"Authorization": header, "Accept": "application/json", "Connection": "close"}
    response = requests.request(method, f"{NETBOX_URL}{path}", headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def _audit_records(object_ids: set[int], minimum: int, timeout: float = 30):
    deadline = time.monotonic() + timeout
    found = []
    while time.monotonic() < deadline:
        found = [
            record
            for _, scope, record in log_records()
            if scope.get("name") == AUDIT_SCOPE
            and record_attr(record, "netbox.change.object_id") in object_ids
            and record_attr(record, "netbox.change.object_type") == "ipam.prefix"
        ]
        if len(found) >= minimum:
            time.sleep(2)  # let possible duplicates arrive before asserting exact counts
            return [
                record
                for _, scope, record in log_records()
                if scope.get("name") == AUDIT_SCOPE
                and record_attr(record, "netbox.change.object_id") in object_ids
                and record_attr(record, "netbox.change.object_type") == "ipam.prefix"
            ]
        time.sleep(1)
    return found


def test_prefix_create_update_delete(header):
    cidr = f"10.{random.randint(100, 250)}.{random.randint(0, 250)}.0/24"
    created = _api(header, "POST", "/api/ipam/prefixes/", json={"prefix": cidr})
    pk = created["id"]
    _api(header, "PATCH", f"/api/ipam/prefixes/{pk}/", json={"description": "otel e2e"})
    _api(header, "DELETE", f"/api/ipam/prefixes/{pk}/")

    records = _audit_records({pk}, 3)
    assert sorted(record_attr(r, "netbox.change.action") for r in records) == ["create", "delete", "update"]
    assert all(r.get("eventName") == "netbox.object_change" for r in records)
    assert all(record_attr(r, "enduser.id") == "admin" for r in records)
    assert len({record_attr(r, "netbox.change.request_id") for r in records}) == 3
    assert all(record_attr(r, "netbox.change.postchange_data") is None for r in records)


def test_bulk_create_shares_one_request_id(header):
    base = random.randint(0, 250)
    payload = [{"prefix": f"10.251.{base}.{i * 16}/28"} for i in range(10)]
    created = _api(header, "POST", "/api/ipam/prefixes/", json=payload)
    ids = {item["id"] for item in created}
    try:
        records = [r for r in _audit_records(ids, 10) if record_attr(r, "netbox.change.action") == "create"]
        assert len(records) == 10
        assert len({record_attr(r, "netbox.change.request_id") for r in records}) == 1
    finally:
        _api(header, "DELETE", "/api/ipam/prefixes/", json=[{"id": i} for i in ids])
