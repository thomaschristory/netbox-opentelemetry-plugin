"""Metrics against a real NetBox. Run with `make test-netbox` or the CI netbox-integration job.

The test process has role `management`, which never installs metrics, so each class builds a
meter provider with the plugin's allowlist and an in-memory reader, and installs the modules
itself. HTTP server metrics come from the real Django instrumentor through the Django test client,
job metrics from a real rq SimpleWorker, change counts from the plugin's installed audit receiver.
"""

import dataclasses
import logging
import uuid

import django_rq
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from ipam.models import Prefix
from netbox.context_managers import event_tracking
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from rest_framework import status
from rq import Queue, SimpleWorker
from utilities.testing import APITestCase

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.conf import MetricsConfig, RqConfig, TracesConfig
from netbox_opentelemetry_plugin.modules.rq import JOB_DURATION, JOBS, QUEUE_DEPTH, RqModule
from netbox_opentelemetry_plugin.modules.traces import TracesModule
from tests.otel_helpers import data_points, metric_names

TEST_QUEUE = "netbox-otel-metrics-test"


class MetricsMixin:
    @classmethod
    def setUpClass(cls):
        state = bootstrap._state
        assert state is not None and state.context is not None, "the plugin is not installed in this process"
        cls.base = state.context
        cls.reader = InMemoryMetricReader()
        cls.meter_provider = otel.SwitchableMeterProvider(otel.build_meter_provider(cls.base.resource, [cls.reader]))
        cls.saved = (cls.base.settings, cls.base.meter_provider)
        settings = dataclasses.replace(
            cls.base.settings,
            traces=TracesConfig(enabled=False),
            metrics=MetricsConfig(enabled=True, export_interval=3600),
            rq=RqConfig(enabled=True, patch_worker=True, propagate_context=False),
        )
        # The installed audit receiver reads these at commit time.
        cls.base.settings = settings
        cls.base.meter_provider = cls.meter_provider
        cls.ctx = dataclasses.replace(cls.base, role=bootstrap.ROLE_RQWORKER, tracer_provider=None)
        cls.instrumentation = TracesModule()
        cls.instrumentation.install(cls.ctx)
        cls.rq_module = RqModule()
        cls.rq_module.install(cls.ctx)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.rq_module.shutdown()
        cls.instrumentation.shutdown()
        cls.base.settings, cls.base.meter_provider = cls.saved

    def collect(self):
        return self.reader.get_metrics_data()


class HttpServerMetricsTest(MetricsMixin, APITestCase):
    def test_request_duration_is_recorded_with_route_and_nothing_else_leaks(self):
        self.add_permissions("dcim.view_site")
        response = self.client.get(reverse("dcim-api:site-list") + "?q=s3cret", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        data = self.collect()
        points = [
            p
            for p in data_points(data, "http.server.request.duration")
            if "dcim/sites" in p.attributes.get("http.route", "")
        ]
        self.assertEqual(len(points), 1)
        self.assertEqual(set(points[0].attributes), {"http.request.method", "http.route", "http.response.status_code"})
        self.assertNotIn("http.server.active_requests", metric_names(data))
        self.assertNotIn("s3cret", repr(data))

    def test_inbound_traceparent_is_not_continued_with_traces_off(self):
        # Traces are off here: the Django instrumentor runs for metrics only and must not make the
        # client's trace context current, so log records written in the view carry no trace id.
        inbound_trace_id = 0x0AF7651916CD43DD8448EB211C80319C
        self.add_permissions("ipam.add_prefix")
        exporter = InMemoryLogRecordExporter()
        handler = otel.build_logging_handler(
            otel.build_logger_provider(self.base.resource, exporter, synchronous=True), logging.INFO
        )
        views_logger = logging.getLogger("netbox.api.views")
        previous = views_logger.level
        views_logger.addHandler(handler)
        views_logger.setLevel(logging.INFO)
        try:
            response = self.client.post(
                reverse("ipam-api:prefix-list"),
                {"prefix": "10.97.0.0/24"},
                format="json",
                HTTP_TRACEPARENT=f"00-{inbound_trace_id:032x}-b7ad6b7169203331-01",
                **self.header,
            )
        finally:
            views_logger.removeHandler(handler)
            views_logger.setLevel(previous)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        points = [
            p
            for p in data_points(self.collect(), "http.server.request.duration")
            if "ipam/prefixes" in p.attributes.get("http.route", "")
        ]
        self.assertEqual(len(points), 1)
        records = [r for r in exporter.get_finished_logs() if "Creating new prefix" in str(r.log_record.body)]
        self.assertEqual(len(records), 1)
        self.assertNotEqual(records[0].log_record.trace_id, inbound_trace_id)
        self.assertFalse(records[0].log_record.trace_id)
        self.assertFalse(records[0].log_record.span_id)

    def test_excluded_url_records_no_duration(self):
        self.client.get("/api/status/", **self.header)
        points = [
            p
            for p in data_points(self.collect(), "http.server.request.duration")
            if "status" in p.attributes.get("http.route", "")
        ]
        self.assertEqual(points, [])


class HttpClientMetricsTest(MetricsMixin, APITestCase):
    def test_outbound_call_is_recorded_with_error_type(self):
        import requests

        with self.assertRaises(requests.RequestException):
            requests.get("http://127.0.0.1:9/refused?token=s3cret", timeout=2)
        data = self.collect()
        points = data_points(data, "http.client.request.duration")
        self.assertTrue(points)
        self.assertTrue(any(p.attributes.get("error.type") for p in points))
        self.assertNotIn("s3cret", repr(data))


class ChangeCounterTest(MetricsMixin, APITestCase):
    def _count(self, action, object_type):
        return sum(
            p.value
            for p in data_points(self.collect(), "netbox.object_changes")
            if p.attributes == {"netbox.change.action": action, "netbox.change.object_type": object_type}
        )

    def test_api_create_is_counted_once_committed(self):
        self.add_permissions("ipam.add_prefix", "ipam.view_prefix")
        before = self._count("create", "ipam.prefix")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("ipam-api:prefix-list"), {"prefix": "10.98.0.0/24"}, format="json", **self.header
            )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(self._count("create", "ipam.prefix") - before, 1)


class ChangeCounterRollbackTest(MetricsMixin, TestCase):
    def _request(self):
        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = get_user_model().objects.create_user(username=f"otel-{uuid.uuid4().hex[:8]}")
        return request

    def _count(self):
        return sum(
            p.value
            for p in data_points(self.collect(), "netbox.object_changes")
            if p.attributes == {"netbox.change.action": "create", "netbox.change.object_type": "ipam.prefix"}
        )

    def test_committed_change_is_counted_and_rolled_back_change_is_not(self):
        before = self._count()
        with self.captureOnCommitCallbacks(execute=True), event_tracking(self._request()):
            try:
                with transaction.atomic():
                    Prefix.objects.create(prefix="10.97.0.0/24")
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(self._count(), before)
        with self.captureOnCommitCallbacks(execute=True), event_tracking(self._request()):
            Prefix.objects.create(prefix="10.96.1.0/24")
        self.assertEqual(self._count() - before, 1)


class JobMetricsTest(MetricsMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.queue = Queue(TEST_QUEUE, connection=django_rq.get_connection("default"))
        self.queue.empty()

    def tearDown(self):
        self.queue.empty()
        super().tearDown()

    def _jobs(self):
        return {
            (p.attributes["code.function.name"], p.attributes["netbox.rq.job.outcome"]): p.value
            for p in data_points(self.collect(), JOBS)
            if p.attributes["messaging.destination.name"] == TEST_QUEUE
        }

    def test_simple_worker_records_finished_and_failed_jobs(self):
        self.queue.enqueue("tests_netbox.jobs.noop")
        self.queue.enqueue("tests_netbox.jobs.boom")
        SimpleWorker([self.queue], connection=self.queue.connection).work(burst=True)
        jobs = self._jobs()
        self.assertEqual(jobs.get(("tests_netbox.jobs.noop", "finished")), 1)
        self.assertEqual(jobs.get(("tests_netbox.jobs.boom", "failed")), 1)
        durations = [
            p
            for p in data_points(self.collect(), JOB_DURATION)
            if p.attributes["messaging.destination.name"] == TEST_QUEUE
        ]
        self.assertEqual(sum(p.count for p in durations), 2)


class QueueDepthTest(MetricsMixin, APITestCase):
    def test_every_configured_queue_is_reported(self):
        depths = {p.attributes["messaging.destination.name"]: p.value for p in data_points(self.collect(), QUEUE_DEPTH)}
        self.assertEqual(set(depths), set(django_settings.RQ_QUEUES))
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in depths.values()))
