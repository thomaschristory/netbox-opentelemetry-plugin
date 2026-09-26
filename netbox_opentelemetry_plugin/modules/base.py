from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..conf import Settings

if TYPE_CHECKING:
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import TracerProvider


@dataclass
class Context:
    """Shared state handed to every module during install."""

    settings: Settings
    role: str
    resource: Resource
    logger_provider: LoggerProvider | None = None
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None


class Module(Protocol):
    name: str

    def enabled(self, settings: Settings) -> bool: ...

    def install(self, ctx: Context) -> None: ...

    def after_fork(self, ctx: Context) -> None: ...

    def shutdown(self) -> None: ...
