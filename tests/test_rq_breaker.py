"""The work-horse flush breaker (issue #4): horses skip their flush while exports keep failing."""

import logging
import mmap
import os
from types import SimpleNamespace

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from rq.worker.base import BaseWorker
from rq.worker.worker_classes import Worker

from netbox_opentelemetry_plugin import bootstrap, otel
from netbox_opentelemetry_plugin.modules import rq as rq_module
from tests.test_rq_module import ARGV_RQ, TRACES, USER, stubs  # noqa: F401  (fixture)

# Python 3.12+ warns when forking a process that has threads (the batch worker). Expected here.
FORK_WARNING = "ignore:.*use of fork\\(\\) may lead to deadlocks:DeprecationWarning"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def as_horse(monkeypatch):
    """Make this process look like a work-horse of the process that prepared the breaker."""
    parent = os.getpid()
    monkeypatch.setattr(os, "getppid", lambda: parent)


def _breaker(clock, threshold=3, cooldown=30.0):
    breaker = rq_module.FlushBreaker(threshold, cooldown, clock=clock)
    breaker.prepare()
    return breaker


def test_closed_until_threshold_consecutive_failures(clock, as_horse):
    breaker = _breaker(clock)
    assert breaker.should_skip() is False
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.should_skip() is False
    assert breaker.record_failure() is True  # opened
    assert breaker.should_skip() is True


def test_a_success_resets_the_failure_count(clock, as_horse):
    breaker = _breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.record_success() is None
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.should_skip() is False


def test_probe_after_cooldown_and_reopen_on_failure(clock, as_horse):
    breaker = _breaker(clock, threshold=2, cooldown=30.0)
    breaker.record_failure()
    assert breaker.record_failure() is True
    clock.now += 29.9
    assert breaker.should_skip() is True
    clock.now += 0.2
    assert breaker.should_skip() is False  # this horse probes with a full flush
    assert breaker.record_failure() is False  # still open: not reported as a new opening
    assert breaker.should_skip() is True
    clock.now += 30.1
    assert breaker.should_skip() is False


def test_success_after_skips_reports_what_was_dropped_and_closes(clock, as_horse):
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    breaker.record_skip(4)
    breaker.record_skip(0)
    breaker.record_skip(None)
    clock.now += 31
    summary = breaker.record_success()
    assert summary == rq_module.SkipSummary(skipped=3, dropped=4, uncounted=1)
    assert breaker.should_skip() is False
    breaker.record_failure()
    breaker.record_skip(1)
    clock.now += 31
    assert breaker.record_success() == rq_module.SkipSummary(skipped=1, dropped=1, uncounted=0)


def test_success_after_an_opening_without_skips_still_reports_recovery(clock, as_horse):
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 31
    assert breaker.record_success() == rq_module.SkipSummary(skipped=0, dropped=0, uncounted=0)


def test_inactive_outside_a_horse_of_the_preparing_process(clock):
    breaker = _breaker(clock, threshold=1)
    # os.getppid() is not this process: this is the worker parent itself, not its horse.
    assert breaker.record_failure() is False
    assert breaker.should_skip() is False


def test_inactive_until_prepared(clock, as_horse):
    breaker = rq_module.FlushBreaker(1, 30.0, clock=clock)
    assert breaker.record_failure() is False
    assert breaker.should_skip() is False


def test_prepare_allocates_once_per_process(clock, monkeypatch):
    breaker = rq_module.FlushBreaker(1, 30.0, clock=clock)
    breaker.prepare()
    first = breaker._mem
    breaker.prepare()
    assert breaker._mem is first
    monkeypatch.setattr(os, "getpid", lambda: -5)
    breaker.prepare()
    assert breaker._mem is not first


def test_prepare_failure_leaves_the_breaker_inactive(clock, as_horse, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no shared memory")

    monkeypatch.setattr(mmap, "mmap", boom)
    breaker = rq_module.FlushBreaker(1, 30.0, clock=clock)
    with pytest.raises(OSError):
        breaker.prepare()
    assert breaker.record_failure() is False
    assert breaker.should_skip() is False


def _in_child(func) -> bytes:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            os.write(write_fd, func())
        finally:
            os._exit(0)
    os.close(write_fd)
    os.waitpid(pid, 0)
    data = os.read(read_fd, 64)
    os.close(read_fd)
    return data


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings(FORK_WARNING)
def test_state_written_by_one_forked_horse_is_seen_by_the_next():
    breaker = rq_module.FlushBreaker(2, 30.0)
    breaker.prepare()

    def fail():
        return b"1" if breaker.record_failure() else b"0"

    assert _in_child(fail) == b"0"
    assert _in_child(fail) == b"1"  # second consecutive failure, in another horse, opens it
    assert _in_child(lambda: b"1" if breaker.should_skip() else b"0") == b"1"


# Integration with the perform_job and fork_work_horse wraps.


@pytest.fixture
def outcomes(monkeypatch):
    """Scripted horse flushes. Each entry of `results` is one horse: {signal: (finished, ok, failed)}
    or (finished, ok, failed, failed_seconds), the flush result for that signal and the export
    results it adds. `flushed` records, per horse, the signals that were actually flushed."""
    state = SimpleNamespace(results=[], flushed=[], totals={"logs": [0, 0, 0.0], "traces": [0, 0, 0.0]})

    def fake_flush(timeout, skip=frozenset()):
        script = state.results[len(state.flushed)]
        done = {}
        for signal, (finished, exported_ok, exported_failed, *seconds) in script.items():
            if signal in skip:
                continue
            state.totals[signal][0] += exported_ok
            state.totals[signal][1] += exported_failed
            state.totals[signal][2] += seconds[0] if seconds else 0.0
            done[signal] = finished
        state.flushed.append(sorted(done))
        return done

    monkeypatch.setattr(bootstrap, "flush_signals", fake_flush)
    monkeypatch.setattr(otel, "export_outcomes", lambda signal: otel.ExportOutcomes(*state.totals[signal]))
    monkeypatch.setattr(otel, "buffered_records", lambda signal: 7 if signal == "logs" else 2)
    return state


def _run_horse():
    Worker.fork_work_horse(SimpleNamespace(), "job", "queue")  # the worker parent, before the fork
    return BaseWorker.perform_job(SimpleNamespace(is_horse=True), "job", "queue")


TIMED_OUT = (False, 0, 0)
EXPORTED = (True, 2, 0)
LOGS_DOWN = {"logs": TIMED_OUT}
LOGS_UP = {"logs": EXPORTED}


def test_horses_skip_the_flush_after_threshold_failures(stubs, outcomes, as_horse, caplog):  # noqa: F811
    outcomes.results = [LOGS_DOWN] * 5
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 3}}, env={}, argv=ARGV_RQ)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        for _ in range(5):
            assert _run_horse() is True
    assert outcomes.flushed == [["logs"]] * 3 + [[]] * 2
    opened = [r.getMessage() for r in caplog.records if "now skip it" in r.getMessage()]
    assert len(opened) == 1
    assert "flush of log records did not finish or failed 3 times in a row" in opened[0]
    assert len(stubs["perform"]) == 5


def test_an_outage_of_one_signal_does_not_skip_the_other(stubs, outcomes, as_horse):  # noqa: F811
    outcomes.results = [{"logs": EXPORTED, "traces": TIMED_OUT}] * 4
    bootstrap.install({**TRACES, "rq": {"flush_breaker_threshold": 2}}, env={}, argv=ARGV_RQ)
    for _ in range(4):
        _run_horse()
    assert outcomes.flushed == [["logs", "traces"]] * 2 + [["logs"]] * 2


def test_a_slow_failed_export_counts_even_when_the_flush_returns_in_time(stubs, outcomes, as_horse):  # noqa: F811
    # For example exporter.timeout below rq.flush_timeout: the flush returns after the exporter
    # spent its retries, the export failed.
    outcomes.results = [{"logs": (True, 0, 1, 1.0)}] * 3
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 2}}, env={}, argv=ARGV_RQ)
    for _ in range(3):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 2 + [[]]


def test_the_slow_threshold_follows_a_short_flush_timeout(stubs, outcomes, as_horse):  # noqa: F811
    # With rq.flush_timeout 0.4 s, a failed export that took 0.1 s (a quarter of it) counts.
    outcomes.results = [{"logs": (True, 0, 1, 0.1)}] * 3
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 2, "flush_timeout": 0.4}}, env={}, argv=ARGV_RQ)
    for _ in range(3):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 2 + [[]]


def test_slow_failure_seconds_follows_the_shortest_timeout():
    assert rq_module.slow_failure_seconds(5.0, 10.0) == 1.0
    assert rq_module.slow_failure_seconds(0.4, 10.0) == pytest.approx(0.1)
    assert rq_module.slow_failure_seconds(5.0, 1.0) == pytest.approx(0.25)
    assert rq_module.slow_failure_seconds(0.4, 0.2) == pytest.approx(0.05)


def test_the_slow_threshold_follows_a_short_exporter_timeout(stubs, outcomes, as_horse):  # noqa: F811
    # exporter.timeout 1 s against a refused connection: the exporter waits one backoff of 0.8 to
    # 1.0 s and gives up before the next one. Such a flush stays under 1 s but must count.
    outcomes.results = [{"logs": (True, 0, 1, 0.9)}] * 4
    user = {**USER, "exporter": {**USER["exporter"], "timeout": 1}}
    bootstrap.install({**user, "rq": {"flush_breaker_threshold": 3, "flush_timeout": 5}}, env={}, argv=ARGV_RQ)
    for _ in range(4):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 3 + [[]]


def test_a_short_exporter_timeout_still_ignores_an_instant_rejection(stubs, outcomes, as_horse):  # noqa: F811
    outcomes.results = [{"logs": (True, 0, 1, 0.01)}] * 3
    user = {**USER, "exporter": {**USER["exporter"], "timeout": 1}}
    bootstrap.install({**user, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
    for _ in range(3):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 3


def test_the_slow_threshold_uses_the_exporter_timeout_of_each_signal(stubs, outcomes, as_horse):  # noqa: F811
    # OTEL_EXPORTER_OTLP_LOGS_TIMEOUT shortens only the logs exporter: 0.9 s counts for logs, while
    # traces keep the 10 s default and so the 1 s threshold.
    outcomes.results = [{"logs": (True, 0, 1, 0.9), "traces": (True, 0, 1, 0.9)}] * 3
    bootstrap.install(
        {**TRACES, "rq": {"flush_breaker_threshold": 2}}, env={"OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "1"}, argv=ARGV_RQ
    )
    for _ in range(3):
        _run_horse()
    assert outcomes.flushed == [["logs", "traces"]] * 2 + [["traces"]]


def test_an_instant_rejection_does_not_count(stubs, outcomes, as_horse):  # noqa: F811
    # For example HTTP 413 for a horse's single oversized batch: the exporter gives up at once, so
    # the flush cost no time and skipping later flushes would gain nothing.
    outcomes.results = [{"logs": (True, 0, 1, 0.01)}] * 4
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
    for _ in range(4):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 4


def test_an_instant_rejection_neither_resets_nor_adds_to_an_outage(stubs, outcomes, as_horse):  # noqa: F811
    rejected = {"logs": (True, 0, 1, 0.01)}
    outcomes.results = [LOGS_DOWN, rejected, LOGS_DOWN, LOGS_DOWN]
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 2}}, env={}, argv=ARGV_RQ)
    for _ in range(4):
        _run_horse()
    # The rejection left the streak at one: the third flush is the second failure.
    assert outcomes.flushed == [["logs"]] * 3 + [[]]


def test_a_flush_with_nothing_exported_changes_nothing(stubs, outcomes, as_horse):  # noqa: F811
    outcomes.results = [LOGS_DOWN, {"logs": (True, 0, 0)}, LOGS_DOWN, LOGS_DOWN, LOGS_DOWN]
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 3}}, env={}, argv=ARGV_RQ)
    for _ in range(5):
        _run_horse()
    # The empty flush neither reset nor added to the count: the fourth flush is the third failure.
    assert outcomes.flushed == [["logs"]] * 4 + [[]]


def test_a_successful_probe_restores_the_flush_and_reports_the_drop(
    stubs,  # noqa: F811
    outcomes,
    as_horse,
    monkeypatch,
    caplog,
):
    clock = Clock()
    monkeypatch.setattr(rq_module.time, "monotonic", clock)
    outcomes.results = [LOGS_DOWN, LOGS_DOWN, LOGS_DOWN, LOGS_UP, LOGS_UP]
    bootstrap.install(
        {**USER, "rq": {"flush_breaker_threshold": 1, "flush_breaker_cooldown": 10}}, env={}, argv=ARGV_RQ
    )
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        _run_horse()  # fails, opens
        _run_horse()  # skipped, 7 records dropped
        _run_horse()  # skipped
        clock.now += 11
        _run_horse()  # probe succeeds
        _run_horse()  # full flush again
    assert outcomes.flushed == [["logs"], [], [], ["logs"], ["logs"]]
    (recovered,) = [r.getMessage() for r in caplog.records if "succeeded again" in r.getMessage()]
    assert "flush of log records succeeded again" in recovered
    assert "2 flushes were skipped, dropping 14 buffered log records" in recovered


def test_uncountable_records_are_reported_as_such(stubs, outcomes, as_horse, monkeypatch, caplog):  # noqa: F811
    clock = Clock()
    monkeypatch.setattr(rq_module.time, "monotonic", clock)
    monkeypatch.setattr(otel, "buffered_records", lambda signal: None)
    outcomes.results = [LOGS_DOWN, LOGS_DOWN, LOGS_UP]
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        _run_horse()
        _run_horse()
        clock.now += 31
        _run_horse()
    (recovered,) = [r.getMessage() for r in caplog.records if "succeeded again" in r.getMessage()]
    assert "could not be counted in 1 of those flushes" in recovered


def test_threshold_zero_always_flushes(stubs, outcomes, as_horse):  # noqa: F811
    outcomes.results = [LOGS_DOWN] * 6
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 0}}, env={}, argv=ARGV_RQ)
    for _ in range(6):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 6


def test_shared_memory_failure_keeps_the_full_flush(stubs, outcomes, as_horse, monkeypatch, caplog):  # noqa: F811
    def boom(*args, **kwargs):
        raise OSError("no shared memory")

    monkeypatch.setattr(mmap, "mmap", boom)
    outcomes.results = [LOGS_DOWN] * 5
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        for _ in range(5):
            assert _run_horse() is True
    assert outcomes.flushed == [["logs"]] * 5
    assert stubs["fork"] == [bootstrap.ROLE_RQ_HORSE] * 5
    warnings = [r for r in caplog.records if "could not set up" in r.getMessage()]
    assert len(warnings) == 1


def test_breaker_errors_never_break_the_job(stubs, outcomes, as_horse, monkeypatch):  # noqa: F811
    outcomes.results = [LOGS_DOWN] * 3
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)

    def boom(*args, **kwargs):
        raise RuntimeError("breaker exploded")

    monkeypatch.setattr(rq_module.FlushBreaker, "should_skip", boom)
    monkeypatch.setattr(rq_module.FlushBreaker, "record_failure", boom)
    for _ in range(3):
        assert _run_horse() is True
    assert outcomes.flushed == [["logs"]] * 3


def test_a_rejected_batch_among_successful_exports_is_not_an_outage(stubs, outcomes, as_horse):  # noqa: F811
    # For example one oversized batch rejected with HTTP 413 while the other batches went through.
    outcomes.results = [{"logs": (True, 2, 1)}] * 3
    bootstrap.install({**USER, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
    for _ in range(3):
        _run_horse()
    assert outcomes.flushed == [["logs"]] * 3


def _external_logger_provider():
    resource = otel.build_resource("ext", {}, service_version="x", plugin_version="x", role="rqworker")
    return otel.build_logger_provider(resource, InMemoryLogRecordExporter(), synchronous=True)


def _external_tracer_provider():
    resource = otel.build_resource("ext", {}, service_version="x", plugin_version="x", role="rqworker")
    return otel.build_tracer_provider(
        resource, InMemorySpanExporter(), otel.build_sampler("always_on", 1.0), synchronous=True
    )


BOTH_DOWN = {"logs": TIMED_OUT, "traces": TIMED_OUT}


def test_no_breaker_for_a_logger_provider_configured_outside_the_plugin(
    stubs,  # noqa: F811
    outcomes,
    as_horse,
    monkeypatch,
):
    # Its exports are not counted, so a flush could never count as a success and close the breaker.
    external = _external_logger_provider()
    monkeypatch.setattr(otel, "existing_logger_provider", lambda: external)
    outcomes.results = [BOTH_DOWN] * 4
    try:
        bootstrap.install({**TRACES, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
        for _ in range(4):
            _run_horse()
    finally:
        external.shutdown()
    # Logs are always flushed; the traces provider is the plugin's own and keeps its breaker.
    assert outcomes.flushed == [["logs", "traces"]] + [["logs"]] * 3


def test_no_breaker_for_a_tracer_provider_configured_outside_the_plugin(
    stubs,  # noqa: F811
    outcomes,
    as_horse,
    monkeypatch,
):
    external = _external_tracer_provider()
    monkeypatch.setattr(otel, "existing_tracer_provider", lambda: external)
    outcomes.results = [BOTH_DOWN] * 4
    try:
        bootstrap.install({**TRACES, "rq": {"flush_breaker_threshold": 1}}, env={}, argv=ARGV_RQ)
        for _ in range(4):
            _run_horse()
    finally:
        external.shutdown()
    assert outcomes.flushed == [["logs", "traces"]] + [["traces"]] * 3


class _SwitchableLogExporter(InMemoryLogRecordExporter):
    """Fails every export while `down`, taking `clock` 2 s forward like an exporter retrying an
    unreachable endpoint, and counts the exports it was asked to make."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.down = True
        self.exports = 0

    def export(self, batch):
        self.exports += 1
        if self.down:
            self.clock.now += 2.0
            return LogRecordExportResult.FAILURE
        return super().export(batch)


def test_breaker_with_the_real_flush_and_export_counters(stubs, as_horse, monkeypatch, caplog):  # noqa: F811
    # No scripted outcomes: the plugin's own batch LoggerProvider, counting exporter and flush.
    # The batch worker thread also exports on its own timer, counted from when it started rather
    # than from an emit; a one hour schedule delay leaves force_flush as the only export, so the
    # export counts and the dropped record count below are exact.
    monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "3600000")
    clock = Clock()
    # Patches time.monotonic itself: the breaker and the export timing in otel both read it.
    monkeypatch.setattr(rq_module.time, "monotonic", clock)
    exporter = _SwitchableLogExporter(clock)
    monkeypatch.setattr(otel, "build_log_exporter", lambda cfg: exporter)
    bootstrap.install(
        {**USER, "rq": {"flush_breaker_threshold": 2, "flush_breaker_cooldown": 30}}, env={}, argv=ARGV_RQ
    )
    job_logger = logging.getLogger("t.rq")

    def horse():
        job_logger.warning("job record")
        _run_horse()

    with caplog.at_level(logging.WARNING, logger="netbox_opentelemetry_plugin"):
        horse()
        horse()
        assert exporter.exports == 2
        assert any("now skip it" in r.getMessage() for r in caplog.records)
        horse()  # skipped: nothing exported
        assert exporter.exports == 2
        exporter.down = False
        clock.now += 31
        horse()  # the probe succeeds and closes the breaker
        assert exporter.exports == 3
        horse()
        assert exporter.exports == 4
    (recovered,) = [r.getMessage() for r in caplog.records if "succeeded again" in r.getMessage()]
    assert "1 flushes were skipped, dropping 1 buffered log records" in recovered
