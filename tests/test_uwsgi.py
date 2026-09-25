import logging
import sys
import types

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from netbox_opentelemetry_plugin import bootstrap, otel

USER = {"exporter": {"endpoint": "http://collector:4318"}, "logs": {"loggers": ["t.uwsgi"]}}
ARGV = ["uwsgi", "--master"]


@pytest.fixture(autouse=True)
def reset_state():
    bootstrap.shutdown()
    bootstrap._state = None
    yield
    bootstrap.shutdown()
    bootstrap._state = None


@pytest.fixture
def exporter(monkeypatch):
    exp = InMemoryLogRecordExporter()
    monkeypatch.setattr(otel, "build_log_exporter", lambda cfg: exp)
    return exp


@pytest.fixture
def fake_uwsgi(monkeypatch):
    module = types.ModuleType("uwsgi")
    module.opt = {"enable-threads": True}
    monkeypatch.setitem(sys.modules, "uwsgi", module)
    monkeypatch.delitem(sys.modules, "pyuwsgi", raising=False)
    return module


def _plugin_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "netbox_opentelemetry_plugin"]


def test_install_registers_post_fork_hook(fake_uwsgi, exporter):
    bootstrap.install(USER, env={}, argv=ARGV)
    assert callable(fake_uwsgi.post_fork_hook)
    assert getattr(fake_uwsgi.post_fork_hook, "_netbox_otel", False) is True


def test_existing_post_fork_hook_is_chained(fake_uwsgi, exporter):
    calls = []
    fake_uwsgi.post_fork_hook = lambda: calls.append("previous")
    bootstrap.install(USER, env={}, argv=ARGV)
    fake_uwsgi.post_fork_hook()
    assert calls == ["previous"]


def test_hook_is_not_wrapped_twice(fake_uwsgi, exporter):
    calls = []
    fake_uwsgi.post_fork_hook = lambda: calls.append("previous")
    bootstrap.install(USER, env={}, argv=ARGV)
    first = fake_uwsgi.post_fork_hook
    bootstrap.shutdown()
    bootstrap._state = None
    bootstrap.install(USER, env={}, argv=ARGV)
    assert fake_uwsgi.post_fork_hook is first
    fake_uwsgi.post_fork_hook()
    assert calls == ["previous"]


def test_hook_runs_the_child_reinit(fake_uwsgi, exporter, monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap, "_after_fork_in_child", lambda: calls.append("reinit"))
    bootstrap.install(USER, env={}, argv=ARGV)
    fake_uwsgi.post_fork_hook()
    assert calls == ["reinit"]


def test_threads_warning_when_classic_uwsgi_without_threads(fake_uwsgi, exporter, caplog):
    fake_uwsgi.opt = {}
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(USER, env={}, argv=ARGV)
    assert _plugin_warnings(caplog) == [bootstrap.UWSGI_THREADS_WARNING]


def test_no_threads_warning_under_pyuwsgi(fake_uwsgi, exporter, caplog, monkeypatch):
    fake_uwsgi.opt = {}
    monkeypatch.setitem(sys.modules, "pyuwsgi", types.ModuleType("pyuwsgi"))
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        bootstrap.install(USER, env={}, argv=ARGV)
    assert _plugin_warnings(caplog) == []


@pytest.mark.parametrize(
    ("opt", "embedded", "disabled"),
    [
        ({}, False, True),
        ({"enable-threads": True}, False, False),
        ({"enable-threads": b"true"}, False, False),
        ({"enable-threads": b"false"}, False, True),
        ({"threads": b"4"}, False, False),
        ({"threads": b"0"}, False, True),
        ({}, True, False),
    ],
)
def test_uwsgi_threads_disabled(opt, embedded, disabled):
    assert bootstrap.uwsgi_threads_disabled(opt, embedded) is disabled


def test_no_uwsgi_means_no_integration(exporter, monkeypatch):
    monkeypatch.delitem(sys.modules, "uwsgi", raising=False)
    assert bootstrap.install(USER, env={}, argv=["granian"]) is not None
