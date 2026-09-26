"""Audit records: one OTel log record per committed NetBox ObjectChange.

The pure functions and make_receiver() take their NetBox/Django dependencies as parameters, so
they can be tested without Django. AuditModule.install() wires the real post_save signal and
transaction.on_commit, and imports Django and NetBox only there.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from .. import otel
from ..conf import Settings
from .base import Context

AUDIT_SCOPE = "netbox_opentelemetry_plugin.audit"
EVENT_NAME = "netbox.object_change"
DISPATCH_UID = "netbox_opentelemetry_plugin.audit"

# include_data batches its SELECT: one query for every LOAD_CHUNK_SIZE pending changes rather
# than one query per change, so a large bulk edit does not turn into thousands of queries.
LOAD_CHUNK_SIZE = 1000

logger = logging.getLogger(otel.PLUGIN_LOGGER)

# Test seam: monkeypatched to simulate a fork (a new PID) without actually forking.
_getpid = os.getpid


@dataclass(frozen=True)
class ChangeSnapshot:
    pk: int
    time_ns: int
    action: str
    object_type: str
    object_id: int
    object_repr: str
    request_id: str
    message: str
    user_name: str
    related_object_type: str | None
    related_object_id: int | None


def snapshot(change, label_for: Callable[[int], str]) -> ChangeSnapshot:
    related_type_id = change.related_object_type_id
    return ChangeSnapshot(
        pk=change.pk,
        time_ns=_time_ns(change.time),
        action=str(change.action),
        object_type=label_for(change.changed_object_type_id),
        object_id=change.changed_object_id,
        object_repr=change.object_repr,
        request_id=str(change.request_id),
        message=change.message or "",
        user_name=change.user_name or "",
        related_object_type=label_for(related_type_id) if related_type_id else None,
        related_object_id=change.related_object_id if related_type_id else None,
    )


def _time_ns(t) -> int:
    """Exact integer nanoseconds since the epoch for an ObjectChange's `time`.

    `datetime.utctimetuple()` normalises an aware datetime to UTC and, for a naive one, returns
    its fields unchanged, so a naive datetime (NetBox always gives an aware one, but a future
    version might not) is treated as already being UTC rather than guessing a timezone. Using
    calendar.timegm plus the microsecond remainder keeps this exact, unlike
    `datetime.timestamp() * 1e9`, which rounds through a float.
    """
    if t is None:
        return time.time_ns()
    return calendar.timegm(t.utctimetuple()) * 10**9 + t.microsecond * 1000


def filter_fields(data, patterns: tuple[str, ...]):
    lowered = tuple(pattern.lower() for pattern in patterns)

    def walk(value):
        if isinstance(value, Mapping):
            return {key: walk(item) for key, item in value.items() if not any(p in str(key).lower() for p in lowered)}
        if isinstance(value, list | tuple):
            return [walk(item) for item in value]
        return value

    return walk(data)


def body_for(snap: ChangeSnapshot) -> str:
    return f"{snap.action} {snap.object_type} {snap.object_repr}"


def build_attributes(snap: ChangeSnapshot, data, exclude_fields) -> dict[str, object]:
    attributes: dict[str, object] = {
        "netbox.change.id": snap.pk,
        "netbox.change.action": snap.action,
        "netbox.change.object_type": snap.object_type,
        "netbox.change.object_id": snap.object_id,
        "netbox.change.object_repr": snap.object_repr,
        "netbox.change.request_id": snap.request_id,
    }
    if snap.message:
        attributes["netbox.change.message"] = snap.message
    if snap.related_object_type is not None and snap.related_object_id is not None:
        attributes["netbox.change.related_object_type"] = snap.related_object_type
        attributes["netbox.change.related_object_id"] = snap.related_object_id
    if snap.user_name:
        attributes["enduser.id"] = snap.user_name
    if data is not None:
        pre, post = data
        for key, value in (("prechange_data", pre), ("postchange_data", post)):
            if value is not None:
                filtered = filter_fields(value, tuple(exclude_fields))
                # No default=str: a value the JSON encoder cannot handle is a sign something
                # unexpected reached here, and is safer to fail into warn_once than to silently
                # stringify (and possibly export more than intended).
                attributes[f"netbox.change.{key}"] = json.dumps(filtered, sort_keys=True)
    return attributes


def _chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def make_receiver(ctx: Context, *, on_commit, label_for, load_data):
    # Per-thread, not per-request: transaction.on_commit runs its callbacks on the thread that
    # committed, which is the same thread the receiver ran on, so this is safe without locking.
    local = threading.local()
    warned_pid: int | None = None

    def warn_once(exc: BaseException) -> None:
        nonlocal warned_pid
        pid = _getpid()
        if warned_pid != pid:
            warned_pid = pid
            # Type name only: exception text could contain object data.
            logger.warning("OpenTelemetry: audit record failed: %s", type(exc).__name__)

    def pending() -> set[int]:
        pks = getattr(local, "pending", None)
        if pks is None:
            pks = set()
            local.pending = pks
        return pks

    def cache() -> dict[int, tuple]:
        loaded = getattr(local, "cache", None)
        if loaded is None:
            loaded = {}
            local.cache = loaded
        return loaded

    def load(pk: int):
        """Return this pk's (pre, post) data, batch-loading every pending pk on first use.

        The first commit callback in a transaction pays for one batched load of everything
        already pending (every change saved earlier in the same transaction); later callbacks in
        that same transaction find their data already cached. A pk left over from a transaction
        that rolled back (its post_save ran, but on_commit never called back) is requested again
        here, found missing, and dropped from `pending` regardless, so it cannot accumulate.
        """
        pks = list(pending())
        if pks:
            found: dict[int, tuple] = {}
            for chunk in _chunks(pks, LOAD_CHUNK_SIZE):
                found.update(load_data(chunk))
            cache().update(found)
            pending().difference_update(pks)
        return cache().pop(pk, None)

    def emit(snap: ChangeSnapshot) -> None:
        try:
            provider = ctx.logger_provider
            cfg = ctx.settings.audit
            if provider is None or not cfg.enabled:
                return
            data = load(snap.pk) if cfg.include_data and load_data is not None else None
            otel.emit_event(
                provider,
                AUDIT_SCOPE,
                event_name=EVENT_NAME,
                body=body_for(snap),
                attributes=build_attributes(snap, data, cfg.exclude_fields),
                timestamp_ns=snap.time_ns,
            )
        except Exception as exc:
            warn_once(exc)

    def receiver(sender, instance, created, **kwargs) -> None:
        if not created or kwargs.get("raw"):
            # A later save of an existing record is NetBox updating an M2M change within the same
            # request; its final data is read at commit time when include_data is on. A raw save
            # (loading a fixture) never runs inside NetBox's own change-logging transaction.
            return
        try:
            snap = snapshot(instance, label_for)
            if ctx.settings.audit.include_data:
                pending().add(snap.pk)
            on_commit(lambda: emit(snap), using=kwargs.get("using"))
        except Exception as exc:
            warn_once(exc)

    return receiver


class AuditModule:
    name = "audit"

    def __init__(self) -> None:
        self._disconnect: Callable[[], None] | None = None

    def enabled(self, settings: Settings) -> bool:
        return settings.audit.enabled

    def install(self, ctx: Context) -> None:
        try:
            from core.models import ObjectChange
            from django.contrib.contenttypes.models import ContentType
            from django.db import transaction
            from django.db.models.signals import post_save
        except ImportError:
            logger.debug("OpenTelemetry: NetBox models are not available; audit records are disabled")
            return

        def label_for(content_type_id: int) -> str:
            content_type = ContentType.objects.get_for_id(content_type_id)
            return f"{content_type.app_label}.{content_type.model}"

        def load_data(pks: list[int]) -> dict[int, tuple]:
            result: dict[int, tuple] = {}
            for chunk in _chunks(list(pks), LOAD_CHUNK_SIZE):
                result.update(
                    {
                        pk: (pre, post)
                        for pk, pre, post in ObjectChange.objects.filter(pk__in=chunk).values_list(
                            "pk", "prechange_data", "postchange_data"
                        )
                    }
                )
            return result

        receiver = make_receiver(ctx, on_commit=transaction.on_commit, label_for=label_for, load_data=load_data)
        post_save.connect(receiver, sender=ObjectChange, dispatch_uid=DISPATCH_UID, weak=False)
        self._disconnect = lambda: post_save.disconnect(sender=ObjectChange, dispatch_uid=DISPATCH_UID)

    def after_fork(self, ctx: Context) -> None:
        # The receiver reads ctx.logger_provider at commit time, so it follows the rebuilt provider.
        pass

    def shutdown(self) -> None:
        if self._disconnect is not None:
            self._disconnect()
            self._disconnect = None
