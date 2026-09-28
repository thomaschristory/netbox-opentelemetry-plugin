"""Traces against a real NetBox. Run with `make test-netbox` or the CI netbox-integration job.

The test process has role `management`, which never installs traces, so each class installs the
traces and RQ modules itself with an in-memory span exporter. Requests go through the Django test
client, so the instrumentor's middleware is part of the handler the client builds per test.
"""

import dataclasses
import logging
import uuid

import django_rq
from core.models import Job
from django.db import connections
from django.urls import reverse
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from rest_framework import status
from rq import Queue, SimpleWorker
from utilities.testing import APITestCase

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.conf import RqConfig, TracesConfig
from netbox_opentelemetry_plugin.modules import rq as rq_module
from netbox_opentelemetry_plugin.modules.rq import RqModule
from netbox_opentelemetry_plugin.modules.traces import TracesModule
from tests_netbox import jobs, rss_feed

# Not one of NetBox's queues, so the dev stack's own worker never takes these jobs.
TEST_QUEUE = "netbox-otel-test"


class TracingMixin:
    @classmethod
    def setUpClass(cls):
        state = bootstrap._state
        assert state is not None and state.context is not None, "the plugin is not installed in this process"
        base = state.context
        cls.span_exporter = InMemorySpanExporter()
        sdk_provider = otel.build_tracer_provider(
            base.resource, cls.span_exporter, otel.build_sampler("always_on", 1.0), synchronous=True
        )
        settings = dataclasses.replace(
            base.settings,
            traces=TracesConfig(enabled=True),
            rq=RqConfig(enabled=True, patch_worker=True, propagate_context=True),
        )
        # role rqworker so the RQ module also wraps perform_job (run here by a SimpleWorker).
        cls.trace_ctx = dataclasses.replace(
            base,
            settings=settings,
            role=bootstrap.ROLE_RQWORKER,
            tracer_provider=otel.SwitchableTracerProvider(sdk_provider),
        )
        cls.traces_module = TracesModule()
        cls.traces_module.install(cls.trace_ctx)
        cls.rq_module = RqModule()
        cls.rq_module.install(cls.trace_ctx)
        # Modules are installed, and any connection left open by an earlier test class is closed,
        # before super().setUpClass(): TestCase's class-level atomic block (entered there) is what
        # opens this class's DB connection, and psycopg instrumentation only wraps *future*
        # psycopg.connect() calls. Closing first forces that connection open through the plugin's
        # own wrap (with its real capture_parameters/enable_commenter settings), instead of
        # leaving in place a connection made before any instrumentation existed.
        connections.close_all()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        # super().tearDownClass() (TestCase) rolls back the class atomic block and closes the
        # connections it opened. Modules are shut down after that, unwrapping psycopg.connect (and
        # the other instrumentors) for whatever connects next. Connections are closed again
        # afterwards so no later test class (for example test_audit.py) inherits a connection whose
        # cursor_factory still points at this class's now-shutdown TracerProvider: uninstrument()
        # only removes the wrap on future connects, not the cursor_factory already set on an open
        # connection object.
        super().tearDownClass()
        cls.rq_module.shutdown()
        cls.traces_module.shutdown()
        connections.close_all()

    def setUp(self):
        super().setUp()
        self.span_exporter.clear()

    def spans(self, kind=None):
        spans = self.span_exporter.get_finished_spans()
        return [s for s in spans if kind is None or s.kind is kind]

    def server_span(self):
        servers = self.spans(SpanKind.SERVER)
        self.assertEqual(len(servers), 1, [s.name for s in self.spans()])
        return servers[0]


class RequestSpanTest(TracingMixin, APITestCase):
    def test_api_request_span_carries_request_id_user_and_db_children(self):
        self.add_permissions("ipam.view_prefix")
        response = self.client.get(reverse("ipam-api:prefix-list"), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        span = self.server_span()
        self.assertTrue(span.name.startswith("GET api/ipam/prefixes"), span.name)
        self.assertEqual(span.attributes["netbox.request_id"], response["X-Request-ID"])
        self.assertEqual(span.attributes["enduser.id"], self.user.username)
        db = [s for s in self.spans(SpanKind.CLIENT) if s.attributes.get("db.system") == "postgresql"]
        self.assertTrue(db)
        for child in db:
            self.assertEqual(child.context.trace_id, span.context.trace_id)
            self.assertNotIn("db.statement.parameters", child.attributes)

    def test_query_string_never_leaves_the_process(self):
        self.add_permissions("ipam.view_prefix")
        path = reverse("ipam-api:prefix-list")
        # REQUEST_URI makes the WSGI helper record url.query, as gunicorn and uWSGI do.
        self.client.get(f"{path}?q=otel-secret-query", REQUEST_URI=f"{path}?q=otel-secret-query", **self.header)
        span = self.server_span()
        self.assertEqual(span.attributes.get("url.query"), "REDACTED")
        for exported in self.spans():
            self.assertNotIn("otel-secret-query", repr(dict(exported.attributes)))

    def test_excluded_url_has_no_span(self):
        self.client.get("/api/status/", **self.header)
        self.assertEqual(self.spans(SpanKind.SERVER), [])

    def test_log_inside_the_request_carries_its_trace_id(self):
        self.add_permissions("ipam.add_prefix")
        exporter = InMemoryLogRecordExporter()
        handler = otel.build_logging_handler(
            otel.build_logger_provider(self.trace_ctx.resource, exporter, synchronous=True), logging.INFO
        )
        views_logger = logging.getLogger("netbox.api.views")
        previous = views_logger.level
        views_logger.addHandler(handler)
        views_logger.setLevel(logging.INFO)
        try:
            response = self.client.post(
                reverse("ipam-api:prefix-list"), {"prefix": "10.98.0.0/24"}, format="json", **self.header
            )
        finally:
            views_logger.removeHandler(handler)
            views_logger.setLevel(previous)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        span = self.server_span()
        records = [r for r in exporter.get_finished_logs() if "Creating new prefix" in str(r.log_record.body)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].log_record.trace_id, span.context.trace_id)
        self.assertEqual(records[0].log_record.span_id, span.context.span_id)


class JobSpanTest(TracingMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.queue = Queue(TEST_QUEUE, connection=django_rq.get_connection("default"))
        self.queue.empty()

    def tearDown(self):
        self.queue.empty()
        super().tearDown()

    def _work(self):
        SimpleWorker([self.queue], connection=self.queue.connection).work(burst=True)

    def test_job_span_continues_the_enqueuing_trace_and_parents_outbound_http(self):
        tracer = self.trace_ctx.tracer_provider.get_tracer("tests")
        with tracer.start_as_current_span("enqueue", kind=SpanKind.SERVER) as parent:
            job = self.queue.enqueue(jobs.fetch, "http://127.0.0.1:9/hook?token=otel-secret-token")
        self.assertIn("traceparent", job.meta[rq_module.CONTEXT_META_KEY])
        self._work()
        consumer = [s for s in self.spans(SpanKind.CONSUMER)]
        self.assertEqual(len(consumer), 1)
        span = consumer[0]
        self.assertEqual(span.name, "rq.job tests_netbox.jobs.fetch")
        self.assertEqual(span.instrumentation_scope.name, rq_module.JOB_SCOPE)
        self.assertEqual(span.context.trace_id, parent.get_span_context().trace_id)
        self.assertEqual(span.parent.span_id, parent.get_span_context().span_id)
        self.assertEqual(span.attributes["messaging.system"], "rq")
        self.assertEqual(span.attributes["messaging.destination.name"], TEST_QUEUE)
        self.assertEqual(span.attributes["messaging.message.id"], job.id)
        self.assertIs(span.status.status_code, StatusCode.ERROR)  # connection refused
        http = [s for s in self.spans(SpanKind.CLIENT) if s.parent and s.parent.span_id == span.context.span_id]
        http = [s for s in http if "url.full" in s.attributes or "http.url" in s.attributes]
        self.assertEqual(len(http), 1)
        for exported in self.spans():
            text = repr(dict(exported.attributes)) + repr(exported.status.description)
            text += "".join(repr(dict(e.attributes)) for e in exported.events)
            self.assertNotIn("otel-secret-token", text)

    def test_netbox_job_attributes(self):
        netbox_job = Job.objects.create(name="otel integration", job_id=uuid.uuid4())
        self.queue.enqueue(jobs.noop, job=netbox_job)
        self._work()
        (span,) = self.spans(SpanKind.CONSUMER)
        self.assertEqual(span.attributes["netbox.job.id"], netbox_job.pk)
        self.assertEqual(span.attributes["netbox.job.name"], "otel integration")
        self.assertIsNone(span.parent)


class OutboundBaggageTest(TracingMixin, APITestCase):
    def test_outbound_call_carries_the_trace_context_but_not_inbound_baggage(self):
        inbound_trace_id = 0x0AF7651916CD43DD8448EB211C80319C
        self.client.force_login(self.user)
        with rss_feed.FeedServer() as feed:
            rss_feed.use_feed_dashboard(self.user, feed.url)
            response = self.client.get(
                "/",
                HTTP_TRACEPARENT=f"00-{inbound_trace_id:032x}-b7ad6b7169203331-01",
                HTTP_BAGGAGE="leak=otel-secret-baggage",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(feed.received_headers), 1)
        headers = feed.received_headers[0]
        span = self.server_span()
        self.assertEqual(span.context.trace_id, inbound_trace_id)
        self.assertEqual(headers["traceparent"].split("-")[1], f"{inbound_trace_id:032x}")
        self.assertNotIn("baggage", headers)
        self.assertNotIn("otel-secret-baggage", repr(headers))
