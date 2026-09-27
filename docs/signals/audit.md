# Audit records

With `audit.enabled` (default `True`), the plugin exports one OpenTelemetry log record for every committed NetBox `ObjectChange`, the row NetBox itself creates for every create, update and delete of an object it tracks.

## What produces a record

The plugin connects to `post_save` on `core.models.ObjectChange`, and only acts when `created` is `True`: a later `created=False` save of the same row, which NetBox uses to fold an M2M change into the record it already created earlier in the same request, is ignored by the receiver itself, as is a raw save (for example loading a fixture). Emission is deferred with `transaction.on_commit`, so a change that is rolled back produces nothing.

Records are emitted through the OTel Logger API directly, independently of the logs module and of stdout, using the shared logs exporter settings (`logs.endpoint`, then `exporter.*`). The underlying `LoggerProvider` is built whenever either `logs` or `audit` is enabled and an endpoint resolves for it, so audit records still export with `logs.enabled = False`.

## Scope, event, severity, timestamp

- Instrumentation scope: `netbox_opentelemetry_plugin.audit`.
- Event name: `netbox.object_change`.
- Severity: always `INFO`, fixed, not affected by `logs.level`.
- Timestamp: the `ObjectChange` row's own `time` field, the moment NetBox recorded the change, not the moment the record is emitted or exported.

## Body

`"<action> <app_label>.<model> <object_repr>"`, for example:

```
update dcim.device edge-rtr-01
```

## Attributes

Only these attributes are ever set, and only when noted:

| Attribute | Type | When present |
|---|---|---|
| `netbox.change.id` | int | always |
| `netbox.change.action` | str | always |
| `netbox.change.object_type` | str (`app_label.model`) | always |
| `netbox.change.object_id` | int | always |
| `netbox.change.object_repr` | str | always |
| `netbox.change.request_id` | str | always |
| `netbox.change.message` | str | only when non-empty |
| `netbox.change.related_object_type` | str (`app_label.model`) | only when a related object is set |
| `netbox.change.related_object_id` | int | only when a related object is set |
| `enduser.id` | str (the change's user name) | only when non-empty |
| `netbox.change.prechange_data` | str (JSON) | only with `audit.include_data`, and only when the stored value is not null |
| `netbox.change.postchange_data` | str (JSON) | only with `audit.include_data`, and only when the stored value is not null |

A change with no message or no related object omits those attributes entirely, rather than sending an empty value.

## Including field data

Setting `audit.include_data = True` adds `netbox.change.prechange_data` and `netbox.change.postchange_data`, each the change's stored value encoded with `json.dumps(..., sort_keys=True)`.

Before encoding, both are filtered recursively through `audit.exclude_fields`: any key whose name contains one of the listed strings, matched case-insensitively, is dropped, together with everything nested under it. The default list is `password`, `secret`, `token`, `key`.

```python
PLUGINS_CONFIG = {
    "netbox_opentelemetry_plugin": {
        "exporter": {"endpoint": "http://collector:4318"},
        "audit": {
            "include_data": True,
            "exclude_fields": ["password", "secret", "token", "key", "config_context", "local_context_data"],
        },
    },
}
```

With `include_data` on, the pre- and post-change data is read from the database at commit time, not from the `post_save` instance, and it is batched per thread: the first commit on a given thread pays for one query covering every already-pending change on that thread, up to 1000 at a time, rather than one query per change. This is also what lets a later, same-request update to `postchange_data` (the M2M case above) reach the record already queued for that change. `ObjectChange` rows are emitted regardless of which database alias they were written to, including a netbox-branching branch's own alias; with `include_data`, the data is read back from that same alias, batched separately per alias, so a primary key that exists on more than one alias never picks up another alias's data.

## Background jobs

Audit records for changes made inside an RQ job or custom script are flushed by the same mechanism as log lines (see [Logs](logs.md#background-jobs)): both wait on the work-horse before it exits, up to `rq.flush_timeout`.

## Failure handling

The receiver and the commit callback each catch every exception, so a failure here never stops the save that triggered it. On failure, one warning is logged per process, naming only the exception type, never its message, since the message could contain object data.

## Sizing

While audit is on, the plugin sizes the underlying log record queue at 20,000 records per process (shared with the logs module), instead of the OTel SDK's smaller default, since a single bulk edit can queue many records at once. A single commit larger than that can still drop records; the SDK reports this only on its own logger, which the plugin never exports (see [Logs, feedback loop](logs.md#feedback-loop)). This applies only to a `LoggerProvider` the plugin builds itself; see Known limitations below for what changes when one configured outside the plugin is reused instead.

With `include_data`, a record can also grow large for an object with big JSON fields. Most Collectors reject a request above their configured body size limit, which drops the whole batch that record was in, not just that one record. Keep `include_data` off unless you need it, or add large fields such as `config_context` and `local_context_data` to `audit.exclude_fields`.

## Known limitations

- If NetBox updates an M2M change record in a transaction later than the one that created it, the data sent with the first record does not include that later update. This only matters with `include_data`; a same-transaction M2M update is covered above.
- The 20,000-record queue is shared with the logs module: a burst of audit records can crowd out log lines buffered in the same process, and vice versa.
- With a `LoggerProvider` configured outside the plugin reused instead of one the plugin builds itself (provider detection, see [How it works](../how-it-works.md#an-sdk-configured-outside-the-plugin)), its queue is not resized: the 20,000-record figure above applies only to a `LoggerProvider` the plugin builds itself.

See the [configuration reference](../configuration.md#reference) for every `audit.*` setting and its default, and [Data safety](../data-safety.md) for what `include_data` changes about what leaves the process.
