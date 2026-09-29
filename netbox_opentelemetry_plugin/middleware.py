"""Adds the NetBox request id and the authenticated user to the active request span.

Registered through PluginConfig.middleware, so NetBox appends it after its own middleware: it
runs inside CoreMiddleware (request.id is set) and inside the span the Django instrumentor
started. The user is read after the view, because API token users are authenticated by DRF
inside the view. Without a recording span it does not annotate, not even evaluate request.user.

Before the view it also records the request span's context on the request, for any valid span
(recording or not). Django logs 4xx and 5xx responses after that span has ended, with the request
in extra=, and the log handler reads the context back from there.
"""

from __future__ import annotations

import logging

from .conf import PLUGIN_LOGGER

logger = logging.getLogger(PLUGIN_LOGGER)

_warned = False
_capture_warned = False


class RequestSpanMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            _capture(request)
        except Exception as exc:
            _warn_capture_once(exc)
        response = self.get_response(request)
        try:
            _annotate(request)
        except Exception as exc:
            _warn_once(exc)
        return response


def _capture(request) -> None:
    # Imported here: a broken OpenTelemetry install must not break NetBox's middleware chain.
    from . import otel

    otel.remember_request_span_context(request)


def _annotate(request) -> None:
    # Imported here: a broken OpenTelemetry install must not break NetBox's middleware chain.
    from . import otel

    if not otel.current_span_is_recording():
        return
    attributes: dict[str, str] = {}
    request_id = getattr(request, "id", None)
    if request_id is not None:
        attributes["netbox.request_id"] = str(request_id)
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        username = user.get_username()
        if username:
            attributes["enduser.id"] = str(username)
    if attributes:
        otel.annotate_current_span(attributes)


def _warn_once(exc: BaseException) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    # The exception type only: its message could contain request data.
    logger.warning("OpenTelemetry: could not annotate the request span: %s", type(exc).__name__)


def _warn_capture_once(exc: BaseException) -> None:
    global _capture_warned
    if _capture_warned:
        return
    _capture_warned = True
    # The exception type only: its message could contain request data.
    logger.warning("OpenTelemetry: could not store the trace context on the request: %s", type(exc).__name__)
