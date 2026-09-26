import datetime
import json
import logging
import time
import uuid
from types import SimpleNamespace

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from netbox_opentelemetry_plugin import bootstrap, otel
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
    # 2026-09-26T12:00:00Z, verified with calendar.timegm(...).
    assert snap.time_ns == 1790424000 * 10**9
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


def test_related_object_omitted_unless_both_type_and_id_are_set():
    # related_object_id can be null even when related_object_type_id is set; neither should be
    # emitted unless both are present, and no attribute is ever emitted with a None value.
    snap = audit.snapshot(_change(related_object_type_id=2, related_object_id=None), LABELS.__getitem__)
    attrs = audit.build_attributes(snap, None, ())
    assert "netbox.change.related_object_type" not in attrs
    assert "netbox.change.related_object_id" not in attrs
    assert None not in attrs.values()


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
    loaded_calls = []

    def load_data(alias, pks):
        loaded_calls.append(sorted(pks))
        return {
            pk: ({"description": "old", "status": "active"}, {"description": "new", "status": "reserved"}) for pk in pks
        }

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(), created=True)
    assert loaded_calls == []
    callbacks[0]()
    attrs = exporter.get_finished_logs()[0].log_record.attributes
    assert loaded_calls == [[42]]
    assert json.loads(attrs["netbox.change.prechange_data"]) == {"status": "active"}
    assert json.loads(attrs["netbox.change.postchange_data"]) == {"status": "reserved"}


def test_batched_load_data_is_called_once_for_all_pending_pks_in_one_commit():
    ctx, exporter = _ctx(include_data=True, exclude=())
    calls = []

    def load_data(alias, pks):
        calls.append(sorted(pks))
        return {pk: (None, {"n": pk}) for pk in pks}

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    pks = list(range(10))
    for pk in pks:
        receiver(sender=None, instance=_change(pk=pk), created=True)
    for callback in callbacks:
        callback()

    assert calls == [pks]
    records = exporter.get_finished_logs()
    assert len(records) == 10
    seen = {json.loads(r.log_record.attributes["netbox.change.postchange_data"])["n"] for r in records}
    assert seen == set(pks)


def test_include_data_loads_are_scoped_to_the_database_alias():
    # A pk can exist on more than one database alias (netbox-branching writes ObjectChange to a
    # branch's own alias). Each alias's change must be loaded from, and attached with, that
    # alias's own data, not another alias's data for the same pk.
    ctx, exporter = _ctx(include_data=True, exclude=())
    calls = []

    def load_data(alias, pks):
        calls.append((alias, sorted(pks)))
        return {pk: (None, {"alias": alias, "n": pk}) for pk in pks}

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(pk=42), created=True, using="default")
    receiver(sender=None, instance=_change(pk=42), created=True, using="branch-1")
    for callback in callbacks:
        callback()

    assert sorted(calls) == [("branch-1", [42]), ("default", [42])]
    records = exporter.get_finished_logs()
    assert len(records) == 2
    by_alias = {json.loads(r.log_record.attributes["netbox.change.postchange_data"])["alias"]: r for r in records}
    assert set(by_alias) == {"default", "branch-1"}


def test_load_does_not_load_another_aliass_pending_keys_on_commit():
    # A branch alias's transaction can still be open, in the same thread, when a transaction on
    # the default alias commits (NetBox-branching's branch and the default database are separate
    # connections that can be interleaved). Loading the branch's pending key at that point would
    # read it before its own transaction is done, missing a later, same-transaction update to it
    # (the M2M case) and caching stale data.
    ctx, exporter = _ctx(include_data=True, exclude=())
    db = {"default": {1: (None, {"v": "d1"})}, "branch": {7: (None, {"v": "b7-initial"})}}
    calls = []

    def load_data(alias, pks):
        calls.append((alias, sorted(pks)))
        return {pk: db[alias][pk] for pk in pks if pk in db[alias]}

    callbacks = {"default": [], "branch": []}
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks[using].append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    # The branch transaction opens and writes change 7; the default transaction writes change 1
    # and commits while the branch transaction is still open.
    receiver(sender=None, instance=_change(pk=7), created=True, using="branch")
    receiver(sender=None, instance=_change(pk=1), created=True, using="default")
    for callback in callbacks["default"]:
        callback()

    # The default commit must not have touched the branch alias at all.
    assert calls == [("default", [1])]

    # The branch transaction updates its postchange_data (the M2M case) before it finally commits.
    db["branch"][7] = (None, {"v": "b7-final"})
    for callback in callbacks["branch"]:
        callback()

    assert calls == [("default", [1]), ("branch", [7])]
    records = {r.log_record.attributes["netbox.change.id"]: r for r in exporter.get_finished_logs()}
    assert json.loads(records[1].log_record.attributes["netbox.change.postchange_data"]) == {"v": "d1"}
    assert json.loads(records[7].log_record.attributes["netbox.change.postchange_data"]) == {"v": "b7-final"}


def test_rolled_back_alias_keys_do_not_accumulate_in_cache_via_another_aliass_commits():
    # If the branch transaction above instead rolls back, its pending key must never be pulled
    # into the cache by the default alias's commits (only load() calls for the branch alias
    # itself may do that); it must stay in `pending` alone. Before the fix, every default commit
    # batch-loaded every pending key regardless of alias, so a repeatedly rolled-back branch key
    # was re-cached on every default commit and never popped, growing the cache without bound.
    ctx, exporter = _ctx(include_data=True, exclude=())
    calls = []

    def load_data(alias, pks):
        calls.append((alias, sorted(pks)))
        return {pk: (None, {"n": pk}) for pk in pks}

    callbacks = {"default": [], "branch": []}
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks[using].append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    # Kept under LOAD_CHUNK_SIZE (1000) on purpose: the final branch commit below must batch
    # every rolled-back branch key plus the new one in a single load_data call, not split across
    # chunks, so calls[-1] alone can be checked against the whole accumulated set.
    iterations = 500
    for i in range(iterations):
        receiver(sender=None, instance=_change(pk=10000 + i), created=True, using="branch")
        receiver(sender=None, instance=_change(pk=1), created=True, using="default")
        for callback in callbacks["default"]:
            callback()
        callbacks["default"].clear()
        callbacks["branch"].clear()  # the branch transaction rolled back: its callback is discarded

    # Every default commit loaded only its own single pending key; the branch alias was never
    # queried, so none of its rolled-back keys were ever loaded into the cache.
    # (Checked as length/membership, not full-list equality: a failing equality assertion between
    # two large lists makes pytest's diff prohibitively slow.)
    assert len(calls) == iterations
    assert all(alias == "default" and pks == [1] for alias, pks in calls)

    # A real branch commit afterwards loads exactly the branch keys still pending (all the
    # rolled-back ones plus this new one, since none of them were ever popped by a load), proving
    # they sat only in `pending`, never in `cache`, the whole time.
    receiver(sender=None, instance=_change(pk=99999), created=True, using="branch")
    for callback in callbacks["branch"]:
        callback()
    last_alias, last_pks = calls[-1]
    assert last_alias == "branch"
    assert len(last_pks) == iterations + 1
    assert set(last_pks) == set(range(10000, 10000 + iterations)) | {99999}


def test_missing_pk_in_loaded_data_yields_no_data_attributes():
    ctx, exporter = _ctx(include_data=True, exclude=())

    def load_data(alias, pks):
        return {}  # the row is gone (for example a concurrent delete)

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(), created=True)
    callbacks[0]()
    attrs = exporter.get_finished_logs()[0].log_record.attributes
    assert "netbox.change.prechange_data" not in attrs
    assert "netbox.change.postchange_data" not in attrs


def test_rolled_back_pks_are_cleared_from_pending_on_the_next_load():
    # pk 999 stands in for a change whose transaction rolled back: the receiver ran (adding it to
    # the thread's pending set) but Django never calls its on_commit callback, so callbacks[0] is
    # simply never invoked here.
    ctx, exporter = _ctx(include_data=True, exclude=())
    calls = []

    def load_data(alias, pks):
        calls.append(sorted(pks))
        return {pk: (None, {"n": pk}) for pk in pks if pk != 999}

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(pk=999), created=True)
    receiver(sender=None, instance=_change(pk=5), created=True)
    callbacks[1]()  # only pk 5 commits; callbacks[0] (pk 999) is discarded, as on a rollback
    assert calls == [[5, 999]]

    receiver(sender=None, instance=_change(pk=6), created=True)
    callbacks[2]()
    assert calls == [[5, 999], [6]]


def test_raw_save_is_ignored():
    ctx, exporter = _ctx()
    callbacks = []
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: callbacks.append(func), label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=True, raw=True)
    assert callbacks == []


def test_non_json_serializable_data_warns_and_emits_nothing(caplog):
    ctx, exporter = _ctx(include_data=True, exclude=())

    class Unserializable:
        pass

    def load_data(alias, pks):
        return {pk: (None, {"bad": Unserializable()}) for pk in pks}

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(), created=True)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        callbacks[0]()
    assert len(exporter.get_finished_logs()) == 0
    warnings = [r for r in caplog.records if r.name == "netbox_opentelemetry_plugin"]
    assert len(warnings) == 1


def test_full_attribute_set_with_include_data_for_update():
    ctx, exporter = _ctx(include_data=True, exclude=())

    def load_data(alias, pks):
        return {pk: ({"status": "active"}, {"status": "reserved"}) for pk in pks}

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=load_data,
    )
    receiver(sender=None, instance=_change(), created=True)
    callbacks[0]()
    attrs = dict(exporter.get_finished_logs()[0].log_record.attributes)
    assert attrs == {
        "netbox.change.id": 42,
        "netbox.change.action": "update",
        "netbox.change.object_type": "ipam.prefix",
        "netbox.change.object_id": 7,
        "netbox.change.object_repr": "10.0.0.0/24",
        "netbox.change.request_id": str(REQUEST_ID),
        "enduser.id": "admin",
        "netbox.change.prechange_data": json.dumps({"status": "active"}, sort_keys=True),
        "netbox.change.postchange_data": json.dumps({"status": "reserved"}, sort_keys=True),
    }


def test_receiver_emits_record_with_correct_timestamp_severity_and_body():
    ctx, exporter = _ctx()
    callbacks = []
    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: callbacks.append(func), label_for=LABELS.__getitem__, load_data=None
    )
    receiver(sender=None, instance=_change(), created=True)
    callbacks[0]()
    record = exporter.get_finished_logs()[0].log_record
    assert record.timestamp == 1790424000 * 10**9
    assert record.severity_text == "INFO"
    assert record.body == "update ipam.prefix 10.0.0.0/24"


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
    assert "boom" not in warnings[0].getMessage()


def test_commit_time_failure_never_raises_and_warns_once(caplog):
    ctx, exporter = _ctx(include_data=True)

    def broken_load(alias, pks):
        raise RuntimeError("db down")

    callbacks = []
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: callbacks.append(func),
        label_for=LABELS.__getitem__,
        load_data=broken_load,
    )
    receiver(sender=None, instance=_change(), created=True)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        callbacks[0]()  # must not raise
    warnings = [r for r in caplog.records if r.name == "netbox_opentelemetry_plugin"]
    assert len(warnings) == 1
    assert len(exporter.get_finished_logs()) == 0


def test_using_kwarg_is_forwarded_to_on_commit():
    ctx, exporter = _ctx()
    received = {}
    receiver = audit.make_receiver(
        ctx,
        on_commit=lambda func, using=None: received.setdefault("using", using),
        label_for=LABELS.__getitem__,
        load_data=None,
    )
    receiver(sender=None, instance=_change(), created=True, using="replica")
    assert received["using"] == "replica"


def test_warn_once_is_keyed_on_pid(monkeypatch, caplog):
    ctx, exporter = _ctx()

    def broken_label(_):
        raise RuntimeError("boom")

    receiver = audit.make_receiver(
        ctx, on_commit=lambda func, using=None: func(), label_for=broken_label, load_data=None
    )
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        monkeypatch.setattr(audit, "_getpid", lambda: 111)
        receiver(sender=None, instance=_change(), created=True)
        receiver(sender=None, instance=_change(), created=True)
        # A fork gives the child a new PID; it must be able to warn once on its own.
        monkeypatch.setattr(audit, "_getpid", lambda: 222)
        receiver(sender=None, instance=_change(), created=True)
    warnings = [r for r in caplog.records if r.name == "netbox_opentelemetry_plugin"]
    assert len(warnings) == 2


def test_bulk_commit_does_not_drop_audit_records(monkeypatch):
    class SlowExporter(InMemoryLogRecordExporter):
        def export(self, batch):
            time.sleep(0.005)
            return super().export(batch)

    exp = SlowExporter()
    monkeypatch.setattr(otel, "build_log_exporter", lambda cfg: exp)
    bootstrap.shutdown()
    bootstrap._state = None
    try:
        ctx = bootstrap.install(
            {"exporter": {"endpoint": "http://collector:4318"}},
            env={},
            argv=["granian", "netbox.granian:application"],
        )
        assert ctx is not None
        receiver = audit.make_receiver(
            ctx, on_commit=lambda func, using=None: func(), label_for=LABELS.__getitem__, load_data=None
        )
        for pk in range(5000):
            receiver(sender=None, instance=_change(pk=pk), created=True)
        ctx.logger_provider.force_flush()
        assert len(exp.get_finished_logs()) == 5000
    finally:
        bootstrap.shutdown()
        bootstrap._state = None


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
