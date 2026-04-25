"""Tests for ClawX scheduler internals: misfire grace, watchdog, listener, SIGHUP reload.

These exercise the regression-prone code paths that caused the
2026-04-09→10 silent-scheduler incident. We mock the apscheduler
BackgroundScheduler so tests stay fast and don't actually start threads.
"""
import logging
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


def test_request_reload_just_sets_flag(stub_clawx):
    """SIGHUP handler must NOT do blocking I/O — just flip the flag.

    Regression guard: if someone wires _reload_schedules back into the
    handler directly, the handler can deadlock on write_lock or
    scheduler internals. Async signal-safety forbids most blocking
    calls; defer the real work to _health_loop.
    """
    assert stub_clawx._reload_requested is False
    with patch.object(stub_clawx, "_reload_schedules") as reload_mock:
        stub_clawx._request_reload()
        # Handler must not have triggered the heavy work.
        reload_mock.assert_not_called()
    assert stub_clawx._reload_requested is True


def test_redact_secrets_telegram_token():
    """Telegram bot token format <8-12 digits>:<base64url chars> must be redacted."""
    text = "Forwarded: TELEGRAM_BOT_TOKEN=8174523344:AAH9aaPo1qwertyABCDEFG_HIJK-LMNopqr"
    out = clawx.redact_secrets(text)
    assert "8174523344:AAH" not in out
    assert "<REDACTED" in out


def test_redact_secrets_jwt():
    text = "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_part_xyz123"
    out = clawx.redact_secrets(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "<REDACTED:jwt>" in out


def test_redact_secrets_bearer():
    text = "curl -H 'Authorization: Bearer abcDEF1234567890xyz123_'"
    out = clawx.redact_secrets(text)
    assert "abcDEF1234567890xyz123_" not in out
    assert "Bearer <REDACTED>" in out


def test_redact_secrets_anthropic_key():
    text = "Set ANTHROPIC_API_KEY=sk-ant-abc123def456ghi789jkl012"
    out = clawx.redact_secrets(text)
    assert "sk-ant-abc123" not in out
    assert "<REDACTED" in out


def test_redact_secrets_passthrough_on_clean_text():
    """Normal heartbeat-style prompts must NOT be redacted."""
    text = "Read HEARTBEAT.md if it exists. Follow it strictly."
    assert clawx.redact_secrets(text) == text


def test_redact_secrets_handles_none_and_empty():
    assert clawx.redact_secrets("") == ""
    assert clawx.redact_secrets(None) is None or clawx.redact_secrets(None) == ""


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


# ── _send_telegram shared helper tests ──────────────────────────────

def test_send_telegram_noop_without_chat_id(stub_clawx):
    stub_clawx.config["compact_notify"] = {}
    stub_clawx._send_telegram("test message")
    stub_clawx.logger.error.assert_not_called()


def test_send_telegram_noop_with_empty_telegram_config(stub_clawx):
    stub_clawx.config["compact_notify"] = {"telegram": {}}
    stub_clawx._send_telegram("test message")
    stub_clawx.logger.error.assert_not_called()


def test_notify_compact_delegates_to_send_telegram(stub_clawx):
    with patch.object(stub_clawx, "_send_telegram") as mock_send:
        stub_clawx._notify_compact()
    mock_send.assert_called_once()
    args = mock_send.call_args
    assert "壓縮" in args[0][0] or "compact" in args[0][0].lower()


def test_notify_rate_limit_delegates_to_send_telegram(stub_clawx):
    with patch.object(stub_clawx, "_send_telegram") as mock_send:
        stub_clawx._notify_rate_limit("You've hit your limit")
    mock_send.assert_called_once()
    args = mock_send.call_args
    assert "rate limit" in args[0][0].lower() or "hit your limit" in args[0][0].lower()


# ── Rate-limit cooldown tests ──────────────────────────────────────

def test_rate_limit_uses_cooldown_not_one_shot(stub_clawx):
    """Rate limit detection should use cooldown, allowing re-detection later."""
    stub_clawx.master_fd = 99
    stub_clawx._ratelimit_buffer = bytearray()
    stub_clawx._ratelimit_cooldown_until = 0

    fake_os = _FakeOS()
    chunk = (
        b"You've hit your limit\n"
        b"/rate-limit-options\n"
        b"  1. Stop and wait for limit to reset\n"
        b"  2. Upgrade your plan\n"
    )
    with patch.object(clawx.os, "write", side_effect=fake_os.write), \
         patch.object(stub_clawx, "_notify_rate_limit"):
        stub_clawx._maybe_handle_rate_limit(chunk)

    assert fake_os.writes == [(99, b"1\r")]
    # Cooldown should be set (not a boolean flag)
    assert stub_clawx._ratelimit_cooldown_until > 0


def test_rate_limit_respects_cooldown(stub_clawx):
    """During cooldown, rate limit detection should be skipped."""
    import time as _time
    stub_clawx.master_fd = 99
    stub_clawx._ratelimit_buffer = bytearray()
    stub_clawx._ratelimit_cooldown_until = _time.time() + 9999  # far future

    fake_os = _FakeOS()
    chunk = (
        b"You've hit your limit\n"
        b"/rate-limit-options\n"
        b"  1. Stop and wait\n"
        b"  2. Upgrade\n"
    )
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        stub_clawx._maybe_handle_rate_limit(chunk)

    assert fake_os.writes == []  # Skipped due to cooldown


# ── Resume modal handler tests ─────────────────────────────────────

def test_maybe_handle_resume_modal_writes_choice_3(stub_clawx):
    """Single-chunk happy path: resume modal in one chunk → send '3'."""
    stub_clawx.master_fd = 99
    stub_clawx._resume_buffer = bytearray()
    stub_clawx._resume_cooldown_until = 0

    fake_os = _FakeOS()
    chunk = (
        b"This session is 2h 14m old and 117.6k tokens.\n"
        b"Resuming the full session will consume a substantial portion of your usage summary.\n\n"
        b"  1. Resume from summary (recommended)\n"
        b"  2. Resume full session as-is\n"
        b"  3. Don't ask me again\n"
    )
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        stub_clawx._maybe_handle_resume_modal(chunk)

    assert fake_os.writes == [(99, b"3\r")]
    assert stub_clawx._resume_cooldown_until > 0


def test_maybe_handle_resume_modal_accumulates_chunks(stub_clawx):
    """Regression (2026-04-12): modal text arrives in multiple PTY chunks.

    The handler must accumulate chunks into a rolling buffer — running
    detect_resume_modal on a single chunk misses the prompt when any
    single chunk doesn't contain both the keyword AND the '3. Don't ask'
    line. Ryan hit this after the morning restart.
    """
    stub_clawx.master_fd = 99
    stub_clawx._resume_buffer = bytearray()
    stub_clawx._resume_cooldown_until = 0

    fake_os = _FakeOS()
    chunks = [
        b"This session is 2h 14m old and 117.6k tokens.\n",
        b"Resuming the full session will consume a substantial portion ",
        b"of your usage summary.\n\n  1. Resume from summary (recommended)\n",
        b"  2. Resume full session as-is\n",
        b"  3. Don't ask me again\n",
    ]
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        for chunk in chunks:
            stub_clawx._maybe_handle_resume_modal(chunk)

    assert fake_os.writes == [(99, b"3\r")], (
        "Handler failed to detect resume modal spread across chunks"
    )


def test_maybe_handle_resume_modal_respects_cooldown(stub_clawx):
    import time as _time
    stub_clawx.master_fd = 99
    stub_clawx._resume_buffer = bytearray()
    stub_clawx._resume_cooldown_until = _time.time() + 9999

    fake_os = _FakeOS()
    chunk = (
        b"Resume full session as-is\n"
        b"  3. Don't ask me again\n"
    )
    with patch.object(clawx.os, "write", side_effect=fake_os.write):
        stub_clawx._maybe_handle_resume_modal(chunk)

    assert fake_os.writes == []


# ── Log rotation tests ─────────────────────────────────────────────

def test_setup_logging_uses_rotating_handler(tmp_path):
    from logging.handlers import RotatingFileHandler
    cfg = {"logging": {"dir": str(tmp_path / "logs"), "max_size_mb": 10, "rotate_count": 3}}
    logger = clawx.setup_logging(cfg)
    rotating_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating_handlers) >= 1
    handler = rotating_handlers[-1]
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 3
    # Cleanup: remove handlers to avoid leaking between tests
    for h in list(logger.handlers):
        logger.removeHandler(h)
    for h in list(logging.getLogger("apscheduler").handlers):
        logging.getLogger("apscheduler").removeHandler(h)


# ── Cron field validation (2026-04-13 audit HIGH #1 / MED #3 / MED #4) ──

def test_validate_cron_parts_rejects_wrong_field_count(stub_clawx):
    """MED #3: cron with != 5 fields must be rejected, not crash with IndexError."""
    parts, ok = stub_clawx._validate_cron_parts("bad", "0 0 0 0")  # 4 fields
    assert ok is False
    stub_clawx.logger.error.assert_called()

    parts, ok = stub_clawx._validate_cron_parts("bad6", "0 0 0 0 0 0")  # 6 fields
    assert ok is False


def test_validate_cron_parts_accepts_valid_5_field(stub_clawx):
    parts, ok = stub_clawx._validate_cron_parts("heartbeat", "*/30 * * * *")
    assert ok is True
    assert parts == ["*/30", "*", "*", "*", "*"]


def test_validate_cron_parts_warns_on_numeric_day_of_week_range(stub_clawx):
    """MED #4: numeric day_of_week like '1-5' means Tue-Sat in APScheduler,
    not Mon-Fri like POSIX cron. Loader must warn so future-us doesn't step
    on the 2026-04-13 tw-stock-preopen rake again."""
    parts, ok = stub_clawx._validate_cron_parts("tw", "30 8 * * 1-5")
    assert ok is True  # We accept it, but we warn
    warning_calls = stub_clawx.logger.warning.call_args_list
    assert any("0=Monday" in str(c) or "mon-fri" in str(c) for c in warning_calls), (
        f"expected a day_of_week warning, got warnings: {warning_calls}"
    )


def test_validate_cron_parts_no_warning_on_literal_day_of_week(stub_clawx):
    stub_clawx._validate_cron_parts("tw", "30 8 * * mon-fri")
    warning_calls = stub_clawx.logger.warning.call_args_list
    assert not any("0=Monday" in str(c) for c in warning_calls), (
        f"should not warn on literal form, got: {warning_calls}"
    )


def test_load_schedule_jobs_skips_bad_cron_instead_of_aborting(stub_clawx):
    """MED #3 + HIGH #1 amplifier: one malformed cron must NOT prevent
    other jobs from being registered. Previously, parts[4] on a 4-field
    string raised IndexError which bubbled up and (combined with
    remove_all_jobs() being called before validation) nuked every job.
    """
    stub_clawx.config["schedule"] = {
        "good": {"enabled": True, "cron": "*/30 * * * *", "prompt": "ok"},
        "bad": {"enabled": True, "cron": "0 0 0 0", "prompt": "broken"},  # 4 fields
        "alsogood": {"enabled": True, "cron": "0 22 * * 0", "prompt": "weekly"},
    }
    stub_clawx.scheduler = MagicMock()
    stub_clawx._load_schedule_jobs()  # Must not raise

    registered = [c.kwargs["id"] for c in stub_clawx.scheduler.add_job.call_args_list]
    assert "good" in registered
    assert "alsogood" in registered
    assert "bad" not in registered


# ── SIGHUP reload atomic-swap safety (2026-04-13 audit HIGH #1) ──

def test_reload_schedules_preserves_old_jobs_on_bad_new_config(stub_clawx):
    """HIGH #1: If the new config has a cron that CronTrigger rejects, the
    old schedule must remain intact. Previously, _reload_schedules called
    remove_all_jobs() *before* validating, so one typo would silently
    leave ClawX with 0 jobs — heartbeat dead, morning report dead."""
    stub_clawx.scheduler = MagicMock()
    # Pretend the staging pass surfaces a broken job. We simulate by
    # returning a new config whose enabled job has a cron CronTrigger
    # refuses.
    bad_cfg = {
        "claude": stub_clawx.config["claude"],
        "session": stub_clawx.config["session"],
        "schedule": {
            "broken": {
                "enabled": True,
                "cron": "* * * * 99",  # invalid day_of_week
                "prompt": "no",
            },
        },
        "logging": stub_clawx.config.get("logging", {}),
    }
    with patch.object(clawx, "load_config", return_value=bad_cfg):
        stub_clawx._reload_schedules()

    # The critical assertion: remove_all_jobs was NOT called. Old jobs
    # are still live.
    stub_clawx.scheduler.remove_all_jobs.assert_not_called()
    stub_clawx.logger.error.assert_called()


def test_reload_schedules_atomic_swap_on_valid_config(stub_clawx):
    """Happy path: valid new config → staged, then swapped."""
    stub_clawx.scheduler = MagicMock()
    stub_clawx.scheduler.get_jobs.return_value = ["x"]
    new_cfg = {
        "claude": stub_clawx.config["claude"],
        "session": stub_clawx.config["session"],
        "schedule": {
            "new_job": {
                "enabled": True,
                "cron": "15 9 * * mon-fri",
                "prompt": "hello",
            },
        },
        "logging": stub_clawx.config.get("logging", {}),
    }
    with patch.object(clawx, "load_config", return_value=new_cfg):
        stub_clawx._reload_schedules()

    # Old jobs cleared, new ones added.
    stub_clawx.scheduler.remove_all_jobs.assert_called_once()
    add_calls = stub_clawx.scheduler.add_job.call_args_list
    assert any(c.kwargs.get("id") == "new_job" for c in add_calls)
    # Config was updated to the new one.
    assert stub_clawx.config is new_cfg


def test_reload_schedules_survives_config_parse_error(stub_clawx):
    """Unchanged behavior from original test — ValueError from load_config
    must be caught and old schedule preserved."""
    stub_clawx.scheduler = MagicMock()
    with patch.object(clawx, "load_config", side_effect=ValueError("bad json")):
        stub_clawx._reload_schedules()
    stub_clawx.scheduler.remove_all_jobs.assert_not_called()
    stub_clawx.logger.error.assert_called()


def test_stage_schedule_jobs_returns_triggers(stub_clawx):
    cfg = {
        "schedule": {
            "a": {"enabled": True, "cron": "*/30 * * * *", "prompt": "p1"},
            "b": {"enabled": False, "cron": "0 0 * * *", "prompt": "disabled"},
            "c": {"enabled": True, "cron": "0 9 * * mon-fri", "prompt": "p2"},
        }
    }
    staged = stub_clawx._stage_schedule_jobs(cfg)
    names = [name for name, _, _ in staged]
    assert "a" in names
    assert "c" in names
    assert "b" not in names  # disabled
    for _, trigger, _ in staged:
        assert trigger is not None


def test_stage_schedule_jobs_raises_on_bad_field_count(stub_clawx):
    """Strict mode: reload staging must raise on any validation failure so
    the caller can preserve the old schedule instead of silently losing it."""
    cfg = {
        "schedule": {
            "bad_count": {"enabled": True, "cron": "0 0 0", "prompt": "3 fields"},
        }
    }
    with pytest.raises(ValueError, match="5 fields"):
        stub_clawx._stage_schedule_jobs(cfg)


def test_stage_schedule_jobs_raises_on_invalid_cron_value(stub_clawx):
    cfg = {
        "schedule": {
            "bad_dow": {"enabled": True, "cron": "* * * * 99", "prompt": "invalid dow"},
        }
    }
    with pytest.raises(ValueError):
        stub_clawx._stage_schedule_jobs(cfg)


def test_stage_schedule_jobs_ignores_disabled(stub_clawx):
    """Disabled jobs are skipped — even a broken disabled job must not
    fail the staging pass."""
    cfg = {
        "schedule": {
            "off": {"enabled": False, "cron": "garbage garbage", "prompt": "no"},
            "good": {"enabled": True, "cron": "*/30 * * * *", "prompt": "ok"},
        }
    }
    staged = stub_clawx._stage_schedule_jobs(cfg)
    assert len(staged) == 1
    assert staged[0][0] == "good"


# ── Restart counter reset (2026-04-13 audit HIGH #2) ──

def test_maybe_reset_restart_count_zeros_after_threshold(stub_clawx):
    """HIGH #2: once the current child has been alive for longer than
    RESTART_COUNT_RESET_SECONDS, restart_count must zero so a later burst
    of crashes can still be recovered."""
    from datetime import datetime as _dt, timedelta as _td

    stub_clawx.restart_count = 2
    stub_clawx.last_restart_at = _dt.now() - _td(
        seconds=stub_clawx.RESTART_COUNT_RESET_SECONDS + 60
    )
    stub_clawx._maybe_reset_restart_count()
    assert stub_clawx.restart_count == 0
    stub_clawx.logger.info.assert_called()


def test_maybe_reset_restart_count_noop_when_recent(stub_clawx):
    """Boundary: if the current child hasn't been alive long enough,
    counter stays put (otherwise a rapid death after a rapid restart
    would silently reset the counter and defeat max_restart_attempts)."""
    from datetime import datetime as _dt, timedelta as _td

    stub_clawx.restart_count = 2
    stub_clawx.last_restart_at = _dt.now() - _td(seconds=60)
    stub_clawx._maybe_reset_restart_count()
    assert stub_clawx.restart_count == 2


def test_maybe_reset_restart_count_noop_when_zero(stub_clawx):
    """No restarts = nothing to reset."""
    from datetime import datetime as _dt, timedelta as _td

    stub_clawx.restart_count = 0
    stub_clawx.last_restart_at = _dt.now() - _td(hours=10)
    stub_clawx._maybe_reset_restart_count()
    assert stub_clawx.restart_count == 0


def test_maybe_reset_restart_count_noop_when_no_last_restart(stub_clawx):
    """Pre-first-spawn (last_restart_at is None) must not crash."""
    stub_clawx.restart_count = 2
    stub_clawx.last_restart_at = None
    stub_clawx._maybe_reset_restart_count()
    assert stub_clawx.restart_count == 2  # untouched


def test_restart_reset_threshold_is_reasonable():
    """Sanity: the reset threshold should be generous enough that a truly
    unstable child doesn't look stable by accident, but short enough that
    a rebooted-once session doesn't carry a stuck counter for hours."""
    assert 10 * 60 <= ClawX.RESTART_COUNT_RESET_SECONDS <= 2 * 3600
