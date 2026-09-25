from __future__ import annotations

import logging

from .. import otel
from ..conf import Settings
from .base import Context


class LogsModule:
    """Attach the OTel logging handler to the configured loggers."""

    name = "logs"

    def __init__(self) -> None:
        self._handler: logging.Handler | None = None
        self._attached: list[logging.Logger] = []
        self._previous_levels: dict[str, int] = {}

    def enabled(self, settings: Settings) -> bool:
        return settings.logs.enabled

    def install(self, ctx: Context) -> None:
        if self._handler is not None or ctx.logger_provider is None:
            return
        cfg = ctx.settings.logs
        handler = otel.build_logging_handler(ctx.logger_provider, cfg.level)
        for name in cfg.loggers:
            target = logging.getLogger(name)
            # Guards against a second install in the same process (for example a module reload).
            if any(isinstance(existing, otel.AllowlistLoggingHandler) for existing in target.handlers):
                continue
            if cfg.set_logger_levels and (target.level == logging.NOTSET or target.level > cfg.level):
                self._previous_levels[name] = target.level
                target.setLevel(cfg.level)
            target.addHandler(handler)
            self._attached.append(target)
        self._handler = handler

    def shutdown(self) -> None:
        if self._handler is None:
            return
        for target in self._attached:
            target.removeHandler(self._handler)
        for name, level in self._previous_levels.items():
            logging.getLogger(name).setLevel(level)
        self._attached.clear()
        self._previous_levels.clear()
        self._handler = None
