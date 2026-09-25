"""Real os.fork() tests for the after-fork re-initialisation.

Each child runs a probe function, sends a JSON result back over a pipe and leaves with os._exit,
so no pytest machinery runs in the child.
"""

import json
import logging
import os

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from netbox_opentelemetry_plugin import bootstrap, otel

pytestmark = [
    pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork"),
    # Python 3.12+ warns when forking a process that has threads (the batch worker). Expected here.
    pytest.mark.filterwarnings("ignore:.*use of fork\\(\\) may lead to deadlocks:DeprecationWarning"),
]

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.fork"]}}
ARGV_WEB = ["gunicorn", "netbox.wsgi"]
ARGV_RQ = ["/opt/netbox/netbox/manage.py", "rqworker"]


@pytest.fixture(autouse=True)
def reset_state():
    bootstrap.shutdown()
    bootstrap._state = None
    yield
    bootstrap.shutdown()
    bootstrap._state = None


@pytest.fixture
def exporters(monkeypatch):
    created = []

    def factory(cfg):
        exporter = InMemoryLogRecordExporter()
        created.append(exporter)
        return exporter

    monkeypatch.setattr(otel, "build_log_exporter", factory)
    return created


def _otel_handlers(name):
    return [h for h in logging.getLogger(name).handlers if isinstance(h, otel.AllowlistLoggingHandler)]


def _run_in_child(probe):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            result = {"ok": True, **probe()}
        except BaseException as exc:
            result = {"ok": False, "error": repr(exc)}
        with os.fdopen(write_fd, "w") as fh:
            json.dump(result, fh)
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as fh:
        payload = fh.read()
    os.waitpid(pid, 0)
    result = json.loads(payload)
    assert result.pop("ok"), result.get("error")
    return result


def test_child_rebuilds_provider_resource_and_repoints_handler(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    parent_provider = ctx.logger_provider
    parent_instance = ctx.resource.attributes["service.instance.id"]
    logging.getLogger("t.fork").setLevel(logging.INFO)

    def probe():
        logging.getLogger("t.fork").info("from child")
        ctx.logger_provider.force_flush()
        handlers = _otel_handlers("t.fork")
        records = exporters[-1].get_finished_logs()
        return {
            "pid": os.getpid(),
            "new_provider": ctx.logger_provider is not parent_provider,
            "handler_count": len(handlers),
            "handler_uses_new_provider": handlers[0]._logger_provider is ctx.logger_provider,
            "instance_id": ctx.resource.attributes["service.instance.id"],
            "record_instance_ids": [r.resource.attributes["service.instance.id"] for r in records],
            "bodies": [r.log_record.body for r in records],
            "exporter_count": len(exporters),
        }

    result = _run_in_child(probe)
    assert result["new_provider"] is True
    assert result["handler_count"] == 1
    assert result["handler_uses_new_provider"] is True
    assert result["instance_id"].endswith(f"-{result['pid']}")
    assert result["instance_id"] != parent_instance
    assert result["bodies"] == ["from child"]
    assert result["record_instance_ids"] == [result["instance_id"]]
    assert result["exporter_count"] == 2
    assert ctx.logger_provider is parent_provider


def test_unflushed_parent_records_are_not_exported_by_child(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    lg = logging.getLogger("t.fork")
    lg.setLevel(logging.INFO)
    lg.info("before fork")

    def probe():
        lg.info("child only")
        ctx.logger_provider.force_flush()
        return {"bodies": [r.log_record.body for r in exporters[-1].get_finished_logs()]}

    assert _run_in_child(probe) == {"bodies": ["child only"]}
    ctx.logger_provider.force_flush()
    assert [r.log_record.body for r in exporters[0].get_finished_logs()] == ["before fork"]


def test_rqworker_child_becomes_rq_horse(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_RQ)
    assert ctx.role == bootstrap.ROLE_RQWORKER

    def probe():
        return {"role": ctx.role, "resource_role": ctx.resource.attributes["netbox.process.role"]}

    assert _run_in_child(probe) == {"role": "rq_horse", "resource_role": "rq_horse"}


def test_reinit_is_idempotent_within_a_process(exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)
    parent_provider = ctx.logger_provider
    bootstrap.reinit_after_fork()
    assert ctx.logger_provider is parent_provider
    assert len(exporters) == 1

    def probe():
        child_provider = ctx.logger_provider
        bootstrap.reinit_after_fork()
        return {"same_provider": ctx.logger_provider is child_provider, "exporter_count": len(exporters)}

    assert _run_in_child(probe) == {"same_provider": True, "exporter_count": 2}


def test_external_provider_is_not_rebuilt(monkeypatch, exporters):
    external = otel.build_logger_provider(
        otel.build_resource("ext", {}, service_version="x", plugin_version="x", role="web"),
        InMemoryLogRecordExporter(),
        synchronous=True,
    )
    monkeypatch.setattr(otel, "existing_logger_provider", lambda: external)
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def probe():
        return {"still_external": ctx.logger_provider is external, "exporter_count": len(exporters)}

    assert _run_in_child(probe) == {"still_external": True, "exporter_count": 0}


def test_rebuild_failure_warns_and_keeps_current_provider(monkeypatch, exporters):
    ctx = bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def probe():
        # The at-fork hook already re-initialised this child once; force a second rebuild that fails.
        current = ctx.logger_provider
        messages = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        logging.getLogger("netbox_opentelemetry_plugin").addHandler(ListHandler())

        def boom(cfg):
            raise RuntimeError("no exporter in child")

        otel.build_log_exporter = boom
        bootstrap._state.pid = -1  # force a re-init as if we had just forked
        bootstrap.reinit_after_fork()
        return {"kept": ctx.logger_provider is current, "messages": messages}

    result = _run_in_child(probe)
    assert result["kept"] is True
    assert any("re-initialisation after fork failed" in m for m in result["messages"])


def test_lock_is_free_in_child(exporters):
    bootstrap.install(USER, env={}, argv=ARGV_WEB)

    def probe():
        acquired = bootstrap._lock.acquire(timeout=1)
        if acquired:
            bootstrap._lock.release()
        return {"acquired": acquired}

    assert _run_in_child(probe) == {"acquired": True}
