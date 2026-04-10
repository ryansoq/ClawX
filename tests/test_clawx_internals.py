"""Tests for ClawX scheduler internals: misfire grace, watchdog, listener, SIGHUP reload.

These exercise the regression-prone code paths that caused the
2026-04-09→10 silent-scheduler incident. We mock the apscheduler
BackgroundScheduler so tests stay fast and don't actually start threads.
"""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import clawx
from clawx import ClawX


@pytest.fixture
def stub_clawx(tmp_path):
    """Build a ClawX instance with config patched in, no real I/O.

    We construct under a patch context, then *replace* self.logger with
    a fresh MagicMock so log assertions work even after the context
    exits. (The original setup_logging may have been called by sibling
    test modules and bound a real FileHandler to the "ClawX" logger;
    patching the function alone isn't enough — the instance attribute
    needs to be a Mock for assert_called to work.)
    """
    cfg = {
        "claude": {
            "command": "echo",
            "project_dir": str(tmp_path),
            "resume_last": False,
        },
        "session": {
            "auto_restart": False,
            "max_restart_attempts": 0,
            "health_check_interval": 1,
        },
        "schedule": {
            "heartbeat": {
                "enabled": True,
                "cron": "*/30 * * * *",
                "prompt": "ping",
            },
            "weekly": {
                "enabled": True,
                "cron": "0 22 * * 0",
                "prompt": "weekly",
            },
            "off": {
                "enabled": False,
                "cron": "0 0 * * *",
                "prompt": "nope",
            },
        },
        "logging": {"dir": str(tmp_path / "logs")},
    }
    with patch.object(clawx, "load_config", return_value=cfg), \
         patch.object(clawx, "setup_logging", return_value=MagicMock()):
        cx = ClawX()
    cx.logger = MagicMock()
    return cx


def test_load_schedule_jobs_uses_generous_misfire_and_coalesce(stub_clawx):
    """Regression: misfire_grace_time MUST be generous and coalesce MUST be on.

    The 2026-04-09 outage was caused by APScheduler's 1-second default
    silently dropping jobs under GIL contention. After Ryan asked us to
    drop the external sentinel and rely on the internal watchdog only,
    we bumped the grace to 1200s (20 minutes) so the in-process recovery
    has plenty of headroom. If anyone reverts these args this test fails.
    """
    stub_clawx.scheduler = MagicMock()
    stub_clawx._load_schedule_jobs()

    # Two enabled jobs, one disabled (skipped).
    assert stub_clawx.scheduler.add_job.call_count == 2
    for call in stub_clawx.scheduler.add_job.call_args_list:
        kwargs = call.kwargs
        assert kwargs["misfire_grace_time"] >= 600, (
            "misfire_grace_time must be >= 600s — see clawx.py comment "
            "for incident history"
        )
        assert kwargs["coalesce"] is True


def test_on_job_event_updates_liveness(stub_clawx):
    stub_clawx.last_job_event_at = None
    fake_event = SimpleNamespace(
        code=clawx.EVENT_JOB_EXECUTED, job_id="heartbeat", exception=None
    )
    stub_clawx._on_job_event(fake_event)
    assert stub_clawx.last_job_event_at is not None
    assert isinstance(stub_clawx.last_job_event_at, datetime)


def test_on_job_event_logs_errors(stub_clawx):
    fake_event = SimpleNamespace(
        code=clawx.EVENT_JOB_ERROR, job_id="heartbeat", exception=RuntimeError("boom")
    )
    stub_clawx._on_job_event(fake_event)
    stub_clawx.logger.error.assert_called_once()
    assert stub_clawx.last_job_event_at is not None


def test_on_job_event_logs_missed(stub_clawx):
    fake_event = SimpleNamespace(
        code=clawx.EVENT_JOB_MISSED, job_id="heartbeat", exception=None
    )
    stub_clawx._on_job_event(fake_event)
    stub_clawx.logger.warning.assert_called_once()


def test_watchdog_noop_when_no_scheduler(stub_clawx):
    stub_clawx.scheduler = None
    stub_clawx.last_job_event_at = datetime.now() - timedelta(hours=10)
    # Should not raise.
    stub_clawx._scheduler_watchdog()


def test_watchdog_noop_when_recent_event(stub_clawx):
    stub_clawx.scheduler = MagicMock()
    stub_clawx.last_job_event_at = datetime.now() - timedelta(minutes=5)
    with patch.object(stub_clawx, "_reload_schedules") as reload_mock:
        stub_clawx._scheduler_watchdog()
    reload_mock.assert_not_called()


def test_watchdog_reloads_when_stale(stub_clawx):
    stub_clawx.scheduler = MagicMock()
    stub_clawx.last_job_event_at = datetime.now() - timedelta(minutes=120)
    with patch.object(stub_clawx, "_reload_schedules") as reload_mock:
        stub_clawx._scheduler_watchdog()
    reload_mock.assert_called_once()


def test_watchdog_idle_threshold_is_generous():
    """Regression: the idle threshold must be ≥ one full heartbeat cycle
    plus the misfire grace, so a single delayed tick never trips a false
    self-heal. We dropped the external sentinel — this is the only line
    of defense, so it has to be conservative.
    """
    assert ClawX.SCHEDULER_WATCHDOG_IDLE_SECONDS >= 60 * 60


def test_watchdog_skips_when_no_frequent_jobs(stub_clawx):
    """Long-gap-only schedules (e.g. weekly) should not trip the watchdog."""
    stub_clawx.config["schedule"] = {
        "weekly": {"enabled": True, "cron": "0 22 * * 0", "prompt": "weekly"}
    }
    stub_clawx.scheduler = MagicMock()
    stub_clawx.last_job_event_at = datetime.now() - timedelta(days=2)
    with patch.object(stub_clawx, "_reload_schedules") as reload_mock:
        stub_clawx._scheduler_watchdog()
    reload_mock.assert_not_called()


def test_reload_schedules_clears_then_reloads(stub_clawx):
    stub_clawx.scheduler = MagicMock()
    stub_clawx.scheduler.get_jobs.return_value = ["a", "b"]
    with patch.object(stub_clawx, "_load_schedule_jobs") as load_mock:
        stub_clawx._reload_schedules()
    stub_clawx.scheduler.remove_all_jobs.assert_called_once()
    load_mock.assert_called_once()


def test_reload_schedules_survives_bad_config(stub_clawx):
    """If config.json has invalid JSON the reload must not crash ClawX."""
    stub_clawx.scheduler = MagicMock()
    with patch.object(clawx, "load_config", side_effect=ValueError("bad json")):
        stub_clawx._reload_schedules()  # Must not raise.
    stub_clawx.logger.error.assert_called()


def test_reload_schedules_handles_uninitialized_scheduler(stub_clawx):
    stub_clawx.scheduler = None
    # Should log a warning and return cleanly.
    stub_clawx._reload_schedules()
    stub_clawx.logger.warning.assert_called()


def test_notify_compact_noop_without_chat_id(stub_clawx):
    """_notify_compact must silently return when no chat_id is configured.
    This is the default state — most users don't set up Telegram notifications.
    """
    stub_clawx.config["compact_notify"] = {}
    # Must not raise, must not log errors.
    stub_clawx._notify_compact()
    stub_clawx.logger.error.assert_not_called()


def test_notify_compact_noop_with_empty_telegram_config(stub_clawx):
    stub_clawx.config["compact_notify"] = {"telegram": {}}
    stub_clawx._notify_compact()
    stub_clawx.logger.error.assert_not_called()


class _FakeOS:
    """Capture os.write calls so we can assert what was sent to the PTY."""
    def __init__(self):
        self.writes = []

    def write(self, fd, data):
        self.writes.append((fd, data))
        return len(data)


def test_maybe_handle_startup_modal_writes_choice(stub_clawx):
    stub_clawx.master_fd = 99
    stub_clawx.started_at = datetime.now()
    stub_clawx._startup_modal_active = True
    stub_clawx._startup_modal_handled = False
    stub_clawx._startup_buffer = bytearray()

    fake_os = _FakeOS()
    chunk = (
        b"Auto-compact prompt:\n"
        b"  1. compact\n"
        b"  2. summarize\n"
        b"  3. skip\n"
    )
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        stub_clawx._maybe_handle_startup_modal(chunk)

    assert stub_clawx._startup_modal_handled is True
    assert stub_clawx._startup_modal_active is False
    assert fake_os.writes == [(99, b"3\r")]


def test_maybe_handle_startup_modal_inactive_is_noop(stub_clawx):
    stub_clawx.master_fd = 99
    stub_clawx.started_at = datetime.now()
    stub_clawx._startup_modal_active = False
    fake_os = _FakeOS()
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        stub_clawx._maybe_handle_startup_modal(
            b"compact?\n  1. a\n  2. b\n"
        )
    assert fake_os.writes == []


def test_maybe_handle_startup_modal_window_expires(stub_clawx):
    stub_clawx.master_fd = 99
    stub_clawx.started_at = datetime.now() - timedelta(
        seconds=clawx.STARTUP_MODAL_WINDOW_SECONDS + 5
    )
    stub_clawx._startup_modal_active = True
    stub_clawx._startup_buffer = bytearray()
    fake_os = _FakeOS()
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        stub_clawx._maybe_handle_startup_modal(
            b"compact?\n  1. a\n  2. b\n"
        )
    assert stub_clawx._startup_modal_active is False
    assert fake_os.writes == []


def test_maybe_handle_startup_modal_caps_buffer(stub_clawx):
    stub_clawx.master_fd = 99
    stub_clawx.started_at = datetime.now()
    stub_clawx._startup_modal_active = True
    stub_clawx._startup_buffer = bytearray()
    fake_os = _FakeOS()
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        # Feed lots of irrelevant data so the buffer would overflow.
        stub_clawx._maybe_handle_startup_modal(b"x" * (clawx.STARTUP_MODAL_BUFFER_LIMIT + 1024))
    assert len(stub_clawx._startup_buffer) <= clawx.STARTUP_MODAL_BUFFER_LIMIT
    # No false positive.
    assert fake_os.writes == []
