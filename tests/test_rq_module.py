import logging
from types import SimpleNamespace

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from rq.worker.base import BaseWorker
from rq.worker.worker_classes import Worker

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.modules import rq as rq_module

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.rq"]}}
ARGV_RQ = ["/opt/netbox/netbox/manage.py", "rqworker"]
ARGV_WEB = ["granian", "netbox.granian:application"]


@pytest.fixture
def stubs(monkeypatch):
    """Replace the real rq methods with stubs for the test, restoring them explicitly afterwards.

    Restoration is explicit (not monkeypatch) so it runs after bootstrap.shutdown() has unwrapped.
    """
    real_perform = BaseWorker.__dict__["perform_job"]
    real_fork = Worker.__dict__["fork_work_horse"]
    calls = {"perform": [], "fork": []}

    def perform_job(self, job, queue):
        calls["perform"].append(job)
        if job == "raise":
            raise RuntimeError("job failed")
        return True

    def fork_work_horse(self, job, queue):
        calls["fork"].append(bootstrap._next_fork_role)

    BaseWorker.perform_job = perform_job
    Worker.fork_work_horse = fork_work_horse
    exporter = InMemoryLogRecordExporter()
    monkeypatch.setattr(otel, "build_log_exporter", lambda cfg: exporter)
    bootstrap.shutdown()
    bootstrap._state = None
    yield calls
    bootstrap.shutdown()
    bootstrap._state = None
    BaseWorker.perform_job = real_perform
    Worker.fork_work_horse = real_fork


@pytest.fixture
def flushes(monkeypatch):
    recorded = []

    def fake_flush(timeout):
        recorded.append(timeout)
        return True

    monkeypatch.setattr(bootstrap, "force_flush", fake_flush)
    return recorded


def _horse():
    return SimpleNamespace(is_horse=True)


def test_not_installed_outside_rqworker(stubs):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)
    assert not getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False)


def test_installed_in_rqworker(stubs):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False) is True
    assert getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False) is True


def test_perform_job_flushes_in_horse(stubs, flushes):
    bootstrap.install({**USER, "rq": {"flush_timeout": 3}}, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job(_horse(), "job", "queue") is True
    assert flushes == [3.0]


def test_perform_job_does_not_flush_outside_horse(stubs, flushes):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    BaseWorker.perform_job(SimpleNamespace(is_horse=False), "job", "queue")
    assert flushes == []


def test_perform_job_flushes_when_job_raises(stubs, flushes):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    with pytest.raises(RuntimeError):
        BaseWorker.perform_job(_horse(), "raise", "queue")
    assert flushes == [5.0]


def test_flush_timeout_logs_a_warning(stubs, monkeypatch, caplog):
    monkeypatch.setattr(bootstrap, "force_flush", lambda timeout: False)
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        BaseWorker.perform_job(_horse(), "job", "queue")
    assert any("did not finish" in r.getMessage() for r in caplog.records)


def test_fork_work_horse_sets_and_clears_the_hint(stubs):
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    Worker.fork_work_horse(SimpleNamespace(), "job", "queue")
    assert stubs["fork"] == [bootstrap.ROLE_RQ_HORSE]
    assert bootstrap._next_fork_role is None


def test_signature_mismatch_skips_wrap(stubs, caplog):
    def perform_job(self, job):
        return True

    BaseWorker.perform_job = perform_job
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert BaseWorker.perform_job is perform_job
    assert getattr(Worker.fork_work_horse, rq_module.WRAPPED_ATTR, False) is True
    assert any("unexpected signature" in r.getMessage() for r in caplog.records)


def test_patch_worker_false_disables(stubs):
    bootstrap.install({**USER, "rq": {"patch_worker": False}}, env={}, argv=ARGV_RQ)
    assert not getattr(BaseWorker.perform_job, rq_module.WRAPPED_ATTR, False)


def test_not_wrapped_twice_and_shutdown_restores(stubs):
    original = BaseWorker.__dict__["perform_job"]
    bootstrap.install(USER, env={}, argv=ARGV_RQ)
    module = rq_module.RqModule()
    module.install(bootstrap._state.context)
    assert BaseWorker.__dict__["perform_job"].__wrapped__ is original
    module.shutdown()
    bootstrap.shutdown()
    bootstrap._state = None
    assert BaseWorker.__dict__["perform_job"] is original


def test_real_rq_signatures_match():
    # Guards against an rq upgrade changing the methods we wrap.
    import inspect

    from rq.worker.base import BaseWorker as RealBase
    from rq.worker.worker_classes import Worker as RealWorker

    assert tuple(inspect.signature(RealBase.__dict__["perform_job"]).parameters) == rq_module.EXPECTED_PARAMS
    assert tuple(inspect.signature(RealWorker.__dict__["fork_work_horse"]).parameters) == rq_module.EXPECTED_PARAMS
