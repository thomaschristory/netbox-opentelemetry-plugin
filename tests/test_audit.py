import datetime
import json
import logging
import uuid
from types import SimpleNamespace

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from netbox_opentelemetry_plugin import otel
from netbox_opentelemetry_plugin.conf import AuditConfig, Settings
from netbox_opentelemetry_plugin.modules import audit
from netbox_opentelemetry_plugin.modules.base import Context

LABELS = {1: "ipam.prefix", 2: "dcim.device"}
REQUEST_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _change(**overrides):
    values = {
        "pk": 42,
        "time": datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC),
        "action": "update",
        "changed_object_type_id": 1,
        "changed_object_id": 7,
        "object_repr": "10.0.0.0/24",
        "request_id": REQUEST_ID,
        "message": "",
        "user_name": "admin",
        "related_object_type_id": None,
        "related_object_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ctx(include_data=False, exclude=("password", "secret", "token", "key")):
    exporter = InMemoryLogRecordExporter()
    resource = otel.build_resource("netbox", {}, service_version="4.7.1", plugin_version="0.1.0", role="web")
    provider = otel.build_logger_provider(resource, exporter, synchronous=True)
    settings = Settings(
        enabled=True, audit=AuditConfig(enabled=True, include_data=include_data, exclude_fields=exclude)
    )
    return Context(settings=settings, role="web", resource=resource, logger_provider=provider), exporter


def test_filter_fields_is_recursive_and_case_insensitive():
    data = {
        "name": "x",
        "Password": "p",
        "nested": {"api_token": "t", "keep": 1, "list": [{"secret_key": "s", "ok": True}]},
    }
    assert audit.filter_fields(data, ("password", "token", "key")) == {
        "name": "x",
        "nested": {"keep": 1, "list": [{"ok": True}]},
    }


def test_filter_fields_leaves_scalars_and_none():
    assert audit.filter_fields(None, ("x",)) is None
    assert audit.filter_fields("value", ("x",)) == "value"


def test_snapshot_and_body():
    snap = audit.snapshot(_change(), LABELS.__getitem__)
    assert snap.object_type == "ipam.prefix"
    assert snap.request_id == str(REQUEST_ID)
    assert snap.time_ns == int(datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC).timestamp() * 1e9)
    assert snap.related_object_type is None
    assert audit.body_for(snap) == "update ipam.prefix 10.0.0.0/24"


def test_build_attributes_minimal():
    snap = audit.snapshot(_change(), LABELS.__getitem__)
    assert audit.build_attributes(snap, None, ()) == {
        "netbox.change.id": 42,
        "netbox.change.action": "update",
        "netbox.change.object_type": "ipam.prefix",
        "netbox.change.object_id": 7,
        "netbox.change.object_repr": "10.0.0.0/24",
        "netbox.change.request_id": str(REQUEST_ID),
        "enduser.id": "admin",
    }


def test_build_attributes_optional_fields():
    snap = audit.snapshot(
        _change(message="bulk edit", related_object_type_id=2, related_object_id=9, user_name=""), LABELS.__getitem__
    )
    attrs = audit.build_attributes(snap, None, ())
    assert attrs["netbox.change.message"] == "bulk edit"
    assert attrs["netbox.change.related_object_type"] == "dcim.device"
    assert attrs["netbox.change.related_object_id"] == 9
    assert "enduser.id" not in attrs


def test_build_attributes_with_data_filters_and_skips_nulls():
    snap = audit.snapshot(_change(action="create"), LABELS.__getitem__)
    attrs = audit.build_attributes(snap, (None, {"prefix": "10.0.0.0/24", "secret_note": "x"}), ("secret",))
    assert "netbox.change.prechange_data" not in attrs
    assert json.loads(attrs["netbox.change.postchange_data"]) == {"prefix": "10.0.0.0/24"}


def test_receiver_emits_on_commit_only():
    ctx, exporter = _ctx()
    callbacks = []
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: callbacks.append(func), label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=True)
    assert len(exporter.get_finished_logs()) == 0
    callbacks[0]()
    record = exporter.get_finished_logs()[0]
    assert record.log_record.event_name == audit.EVENT_NAME
    assert record.instrumentation_scope.name == audit.AUDIT_SCOPE
    assert record.log_record.attributes["netbox.change.id"] == 42


def test_receiver_ignores_updates_of_existing_records():
    ctx, exporter = _ctx()
    callbacks = []
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: callbacks.append(func), label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=False)
    assert callbacks == []


def test_discarded_callbacks_emit_nothing():
    ctx, exporter = _ctx()
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: None, label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=True)
    assert len(exporter.get_finished_logs()) == 0


def test_include_data_loads_at_commit_time_and_filters():
    ctx, exporter = _ctx(include_data=True, exclude=("description",))
    loaded = []

    def load_data(pk):
        loaded.append(pk)
        return ({"description": "old", "status": "active"}, {"description": "new", "status": "reserved"})

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(), created=True)
    assert loaded == []
    callbacks[0]()
    attrs = exporter.get_finished_logs()[0].log_record.attributes
    assert loaded == [42]
    assert json.loads(attrs["netbox.change.prechange_data"]) == {"status": "active"}
    assert json.loads(attrs["netbox.change.postchange_data"]) == {"status": "reserved"}


def test_receiver_uses_the_current_provider_at_commit_time():
    ctx, first = _ctx()
    callbacks = []
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: callbacks.append(func), label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=True)
    second = InMemoryLogRecordExporter()
    ctx.logger_provider = otel.build_logger_provider(ctx.resource, second, synchronous=True)
    callbacks[0]()
    assert len(first.get_finished_logs()) == 0
    assert len(second.get_finished_logs()) == 1


def test_failures_never_raise_and_warn_once(caplog):
    ctx, exporter = _ctx()

    def broken_label(_):
        raise RuntimeError("boom")

    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: func(), label_for=broken_label, load_data=None
    )
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        receiver(sender=None, instance=_change(), created=True)
        receiver(sender=None, instance=_change(), created=True)
    warnings = [r for r in caplog.records if r.name == "netbox_opentelemetry_plugin"]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()


def test_audit_disabled_at_commit_time_emits_nothing():
    ctx, exporter = _ctx()
    callbacks = []
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: callbacks.append(func), label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=True)
    ctx.settings = Settings(enabled=True, audit=AuditConfig(enabled=False))
    callbacks[0]()
    assert len(exporter.get_finished_logs()) == 0


def test_module_install_without_netbox_is_inert():
    ctx, _ = _ctx()
    module = audit.AuditModule()
    module.install(ctx)  # Django/NetBox are not importable in unit tests
    module.shutdown()


def test_module_enabled_follows_settings():
    assert audit.AuditModule().enabled(Settings(enabled=True, audit=AuditConfig(enabled=True))) is True
    assert audit.AuditModule().enabled(Settings(enabled=True)) is False
