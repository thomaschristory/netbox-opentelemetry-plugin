"""Audit records: one OTel log record per committed NetBox ObjectChange.

The pure functions and make_receiver() take their NetBox/Django dependencies as parameters, so
they can be tested without Django. AuditModule.install() wires the real post_save signal and
transaction.on_commit, and imports Django and NetBox only there.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .. import otel
from ..conf import Settings
from .base import Context

AUDIT_SCOPE = "netbox_opentelemetry_plugin.audit"
EVENT_NAME = "netbox.object_change"
DISPATCH_UID = "netbox_opentelemetry_plugin.audit"

logger = logging.getLogger(otel.PLUGIN_LOGGER)


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
        time_ns=int(change.time.timestamp() * 1e9) if change.time else time.time_ns(),
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
    if snap.related_object_type is not None:
        attributes["netbox.change.related_object_type"] = snap.related_object_type
        attributes["netbox.change.related_object_id"] = snap.related_object_id
    if snap.user_name:
        attributes["enduser.id"] = snap.user_name
    if data is not None:
        pre, post = data
        for key, value in (("prechange_data", pre), ("postchange_data", post)):
            if value is not None:
                filtered = filter_fields(value, tuple(exclude_fields))
                attributes[f"netbox.change.{key}"] = json.dumps(filtered, sort_keys=True, default=str)
    return attributes


def make_receiver(ctx: Context, *, on_commit, label_for, load_data):
    warned = False

    def warn_once(exc: BaseException) -> None:
        nonlocal warned
        if not warned:
            warned = True
            # Type name only: exception text could contain object data.
            logger.warning("OpenTelemetry: audit record failed: %s", type(exc).__name__)

    def emit(snap: ChangeSnapshot) -> None:
        try:
            provider = ctx.logger_provider
            cfg = ctx.settings.audit
            if provider is None or not cfg.enabled:
                return
            data = load_data(snap.pk) if cfg.include_data and load_data is not None else None
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
        if not created:
            # A later save of an existing record is NetBox updating an M2M change within the same
            # request; its final data is read at commit time when include_data is on.
            return
        try:
            snap = snapshot(instance, label_for)
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

        def load_data(pk: int):
            return ObjectChange.objects.filter(pk=pk).values_list("prechange_data", "postchange_data").first()

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
