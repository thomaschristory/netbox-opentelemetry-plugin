"""Audit records against a real NetBox. Run with `make test-netbox` or the CI netbox-integration job.

These tests use NetBox's own test runner and are not collected by pytest (pytest's testpaths is `tests`).
"""

import dataclasses
import json
import uuid

from dcim.models import DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from ipam.models import Prefix
from netbox.context_managers import event_tracking
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from rest_framework import status
from utilities.testing import APITestCase

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.conf import AuditConfig
from netbox_opentelemetry_plugin.modules.audit import AUDIT_SCOPE, EVENT_NAME


class AuditCaptureMixin:
    """Route audit records to an in-memory exporter for the duration of each test."""

    def setUp(self):
        super().setUp()
        state = bootstrap._state
        self.assertTrue(state is not None and state.context is not None, "the plugin is not installed in this process")
        self.ctx = state.context
        self._saved = (self.ctx.logger_provider, self.ctx.settings)
        self.exporter = InMemoryLogRecordExporter()
        self.ctx.logger_provider = otel.build_logger_provider(self.ctx.resource, self.exporter, synchronous=True)

    def tearDown(self):
        self.ctx.logger_provider, self.ctx.settings = self._saved
        super().tearDown()

    def audit_records(self):
        return [r for r in self.exporter.get_finished_logs() if r.instrumentation_scope.name == AUDIT_SCOPE]


class PrefixAuditTest(AuditCaptureMixin, APITestCase):
    def test_create_update_delete(self):
        self.add_permissions("ipam.add_prefix", "ipam.change_prefix", "ipam.delete_prefix", "ipam.view_prefix")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("ipam-api:prefix-list"), {"prefix": "10.99.0.0/24"}, format="json", **self.header
            )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        pk = response.data["id"]
        detail = reverse("ipam-api:prefix-detail", kwargs={"pk": pk})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(detail, {"description": "changed"}, format="json", **self.header)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(detail, **self.header)

        records = self.audit_records()
        self.assertEqual(
            [r.log_record.attributes["netbox.change.action"] for r in records], ["create", "update", "delete"]
        )
        for record in records:
            attrs = record.log_record.attributes
            self.assertEqual(record.log_record.event_name, EVENT_NAME)
            self.assertEqual(attrs["netbox.change.object_type"], "ipam.prefix")
            self.assertEqual(attrs["netbox.change.object_id"], pk)
            self.assertEqual(attrs["enduser.id"], self.user.username)
            self.assertNotIn("netbox.change.prechange_data", attrs)
            self.assertNotIn("netbox.change.postchange_data", attrs)
        self.assertEqual(len({r.log_record.attributes["netbox.change.request_id"] for r in records}), 3)
        self.assertEqual(records[0].log_record.body, "create ipam.prefix 10.99.0.0/24")

    def test_include_data_filters_excluded_fields(self):
        self.ctx.settings = dataclasses.replace(
            self.ctx.settings, audit=AuditConfig(enabled=True, include_data=True, exclude_fields=("description",))
        )
        self.add_permissions("ipam.add_prefix", "ipam.view_prefix")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("ipam-api:prefix-list"),
                {"prefix": "10.97.0.0/24", "description": "should not be exported"},
                format="json",
                **self.header,
            )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        attrs = self.audit_records()[-1].log_record.attributes
        post = json.loads(attrs["netbox.change.postchange_data"])
        self.assertEqual(post["prefix"], "10.97.0.0/24")
        self.assertNotIn("description", post)
        self.assertNotIn("netbox.change.prechange_data", attrs)


class DeviceBulkAuditTest(AuditCaptureMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="Otel Mfr", slug="otel-mfr")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="Otel Type", slug="otel-type")
        cls.role = DeviceRole.objects.create(name="Otel Role", slug="otel-role")
        cls.site = Site.objects.create(name="Otel Site", slug="otel-site")

    def test_bulk_create_shares_one_request_id(self):
        self.add_permissions("dcim.add_device", "dcim.view_device")
        payload = [
            {"name": f"otel-dev-{i}", "device_type": self.device_type.pk, "role": self.role.pk, "site": self.site.pk}
            for i in range(10)
        ]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("dcim-api:device-list"), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        records = [
            r for r in self.audit_records() if r.log_record.attributes["netbox.change.object_type"] == "dcim.device"
        ]
        self.assertEqual(len(records), 10)
        self.assertEqual({r.log_record.attributes["netbox.change.action"] for r in records}, {"create"})
        self.assertEqual(len({r.log_record.attributes["netbox.change.request_id"] for r in records}), 1)


class RollbackAuditTest(AuditCaptureMixin, TestCase):
    def _request(self):
        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = get_user_model().objects.create_user(username=f"otel-{uuid.uuid4().hex[:8]}")
        return request

    def test_committed_change_emits_one_record(self):
        with self.captureOnCommitCallbacks(execute=True), event_tracking(self._request()):
            Prefix.objects.create(prefix="10.96.0.0/24")
        self.assertEqual(len(self.audit_records()), 1)

    def test_rolled_back_change_emits_nothing(self):
        with self.captureOnCommitCallbacks(execute=True), event_tracking(self._request()):
            try:
                with transaction.atomic():
                    Prefix.objects.create(prefix="10.95.0.0/24")
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(self.audit_records(), [])
