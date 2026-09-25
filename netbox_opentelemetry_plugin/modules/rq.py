"""RQ integration: flush the work-horse before it exits, and label its fork.

rq runs each job in a forked work-horse that leaves with os._exit, so atexit never runs there
and buffered log records would be lost. perform_job is wrapped with a bounded flush in a finally
block, and fork_work_horse announces the fork so the child is labelled rq_horse. Other forks of
the worker process (such as the RQ scheduler) keep the rqworker role.
"""

from __future__ import annotations

import functools
import inspect
import logging

from .. import otel
from ..conf import Settings
from .base import Context

WRAPPED_ATTR = "_netbox_otel_wrapped"
EXPECTED_PARAMS = ("self", "job", "queue")

logger = logging.getLogger(otel.PLUGIN_LOGGER)


class RqModule:
    name = "rq"

    def __init__(self) -> None:
        self._patched: list[tuple[type, str, object]] = []

    def enabled(self, settings: Settings) -> bool:
        return settings.rq.enabled and settings.rq.patch_worker

    def install(self, ctx: Context) -> None:
        from rq.worker.base import BaseWorker
        from rq.worker.worker_classes import Worker

        self._wrap(BaseWorker, "perform_job", _perform_job_wrapper(ctx))
        self._wrap(Worker, "fork_work_horse", _fork_work_horse_wrapper())

    def after_fork(self, ctx: Context) -> None:
        pass

    def shutdown(self) -> None:
        for cls, attr, original in reversed(self._patched):
            setattr(cls, attr, original)
        self._patched.clear()

    def _wrap(self, cls: type, attr: str, make_wrapper) -> None:
        original = cls.__dict__.get(attr)
        if original is None:
            logger.warning(
                "OpenTelemetry: rq %s.%s is missing; this part of the RQ integration is disabled",
                cls.__name__,
                attr,
            )
            return
        if getattr(original, WRAPPED_ATTR, False):
            return
        params = tuple(inspect.signature(original).parameters)
        if params != EXPECTED_PARAMS:
            logger.warning(
                "OpenTelemetry: rq %s.%s has an unexpected signature %s; this part of the RQ integration is disabled",
                cls.__name__,
                attr,
                params,
            )
            return
        wrapper = functools.update_wrapper(make_wrapper(original), original)
        setattr(wrapper, WRAPPED_ATTR, True)
        setattr(cls, attr, wrapper)
        self._patched.append((cls, attr, original))


def _perform_job_wrapper(ctx: Context):
    def make(original):
        def perform_job(self, job, queue):
            try:
                return original(self, job, queue)
            finally:
                # Only a forked work-horse exits (os._exit) right after this returns. A SimpleWorker
                # runs jobs in the long-lived worker process, whose batch processor flushes normally.
                if getattr(self, "is_horse", False):
                    from .. import bootstrap

                    timeout = ctx.settings.rq.flush_timeout
                    if not bootstrap.force_flush(timeout):
                        logger.warning(
                            "OpenTelemetry: log flush in the RQ work-horse did not finish within %s s", timeout
                        )

        return perform_job

    return make


def _fork_work_horse_wrapper():
    def make(original):
        def fork_work_horse(self, job, queue):
            from .. import bootstrap

            bootstrap.set_next_fork_role(bootstrap.ROLE_RQ_HORSE)
            try:
                return original(self, job, queue)
            finally:
                bootstrap.set_next_fork_role(None)

        return fork_work_horse

    return make
