"""
Test suite for ClawX.

Strategy:
- Replace `claude` with a mock bash script (tests/mock_claude.sh) that logs
  everything ClawX sends it to a per-test log file.
- Each test gets a fresh tmp dir with a copy of clawx.py + custom config.json.
- Most tests use subprocess to launch ClawX as a black box; a few import the
  module directly to inspect internals (build_command, _setup_schedules).

Run with:  pytest tests/test_clawx.py -v
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAWX_SRC = REPO_ROOT / "clawx.py"
MOCK_CLAUDE = REPO_ROOT / "tests" / "mock_claude.sh"


# ============================================================================
# Helpers
# ============================================================================

def make_config(mock_log_path, project_dir=".", schedule=None, resume_last=False,
                model="opus", extra_args=None, restart_attempts=3):
    """Build a ClawX config.json dict pointing at the mock binary."""
    cfg = {
        "claude": {
            "command": str(MOCK_CLAUDE),
            "project_dir": project_dir,
            "model": model,
            "permission_mode": None,
            "resume_last": resume_last,
            "mcp_config": None,
            "extra_args": extra_args or [],
        },
        "session": {
            "auto_restart": True,
            "max_restart_attempts": restart_attempts,
            "restart_delay_seconds": 1,
            "health_check_interval": 2,
            "health_check_timeout": 5,
        },
        "schedule": schedule or {},
        "logging": {"dir": "logs", "max_size_mb": 50, "rotate_count": 5},
        "_test_mock_log": str(mock_log_path),  # ignored by ClawX, used by tests
    }
    return cfg


def setup_workdir(tmp_path, config):
    """Copy clawx.py + write config.json + create mock log path."""
    work = tmp_path / "claw"
    work.mkdir()
    shutil.copy(CLAWX_SRC, work / "clawx.py")
    (work / "config.json").write_text(json.dumps(config, indent=2))
    return work


def launch_clawx(workdir, mock_log):
    """Spawn ClawX as a subprocess. MOCK_LOG env var pipes to mock binary."""
    env = os.environ.copy()
    env["MOCK_LOG"] = str(mock_log)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(workdir / "clawx.py")],
        cwd=str(workdir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return proc


def wait_for_file(path, timeout=5):
    """Wait until path exists, return True/False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.05)
    return False


def wait_for_log_contains(log_path, needle, timeout=5):
    """Wait until log file contains needle, return True/False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(log_path).exists():
            content = Path(log_path).read_text(errors="ignore")
            if needle in content:
                return True
        time.sleep(0.05)
    return False


def stop_clawx(proc, timeout=3):
    """Gracefully terminate the ClawX subprocess."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def workdir(tmp_path):
    """Per-test temp workdir + mock log path."""
    mock_log = tmp_path / "mock.log"
    config = make_config(mock_log)
    work = setup_workdir(tmp_path, config)
    yield work, mock_log


@pytest.fixture
def running_clawx(workdir):
    """Start ClawX, yield (proc, workdir, mock_log), tear down."""
    work, mock_log = workdir
    proc = launch_clawx(work, mock_log)
    # Wait for mock to actually start (proves PTY spawn worked)
    assert wait_for_log_contains(mock_log, "MOCK_STARTED", timeout=5), \
        "Mock claude never started — ClawX failed to spawn it"
    yield proc, work, mock_log
    stop_clawx(proc)


# ============================================================================
# CORE FUNCTIONALITY TESTS
# ============================================================================

class TestSmoke:
    """Test 1: Basic startup smoke test."""

    def test_fifo_pid_logs_created(self, running_clawx):
        proc, work, mock_log = running_clawx
        assert (work / "mono.fifo").exists(), "FIFO not created"
        assert (work / "mono.pid").exists(), "PID file not created"
        assert (work / "logs").exists(), "logs/ dir not created"
        log_files = list((work / "logs").glob("clawx-*.log"))
        assert log_files, "No clawx-*.log file written"

    def test_pid_file_contains_valid_pid(self, running_clawx):
        proc, work, mock_log = running_clawx
        pid_text = (work / "mono.pid").read_text().strip()
        assert pid_text.isdigit(), f"PID file has non-numeric content: {pid_text}"
        # PID should match a real process
        os.kill(int(pid_text), 0)  # raises OSError if no such process

    def test_mock_received_correct_args(self, running_clawx):
        proc, work, mock_log = running_clawx
        content = mock_log.read_text()
        assert "--add-dir" in content, "Project dir flag missing"
        assert "--model opus" in content, "Model flag missing"
        assert "--dangerously-skip-permissions" in content, \
            "Default permission mode flag missing"


# ============================================================================
# INJECTION TESTS
# ============================================================================

class TestFifoInject:
    """Test 2-3: Injecting messages via FIFO and CLI."""

    def test_fifo_inject_direct(self, running_clawx):
        proc, work, mock_log = running_clawx
        fifo = work / "mono.fifo"
        with open(str(fifo), "w") as f:
            f.write("HELLO_FIFO\n")
        assert wait_for_log_contains(mock_log, "RECV: HELLO_FIFO", timeout=3), \
            "Mock did not receive FIFO injection"

    def test_cli_inject_command(self, running_clawx):
        proc, work, mock_log = running_clawx
        result = subprocess.run(
            [sys.executable, str(work / "clawx.py"), "inject", "HELLO_CLI"],
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, f"inject command failed: {result.stderr}"
        assert wait_for_log_contains(mock_log, "RECV: HELLO_CLI", timeout=3), \
            "Mock did not receive CLI injection"

    def test_rapid_injects_no_drops(self, running_clawx):
        proc, work, mock_log = running_clawx
        fifo = work / "mono.fifo"
        for i in range(10):
            with open(str(fifo), "w") as f:
                f.write(f"BURST_{i}\n")
            time.sleep(0.05)
        # Verify all 10 arrived
        deadline = time.time() + 5
        while time.time() < deadline:
            content = mock_log.read_text(errors="ignore")
            if all(f"RECV: BURST_{i}" in content for i in range(10)):
                return
            time.sleep(0.1)
        content = mock_log.read_text(errors="ignore")
        missing = [i for i in range(10) if f"RECV: BURST_{i}" not in content]
        pytest.fail(f"Missing burst messages: {missing}")

    def test_inject_when_no_session(self, tmp_path):
        """`inject` command should fail gracefully if FIFO doesn't exist."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log)
        work = setup_workdir(tmp_path, config)
        result = subprocess.run(
            [sys.executable, str(work / "clawx.py"), "inject", "test"],
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0, "Inject without session should fail"
        assert "FIFO not found" in result.stdout or "FIFO not found" in result.stderr


# ============================================================================
# CONTROL TESTS
# ============================================================================

class TestStop:
    """Test 4: Stop command."""

    def test_stop_command_sends_sigterm(self, running_clawx):
        proc, work, mock_log = running_clawx
        # Capture child PID before stop
        child_pid = int((work / "mono.pid").read_text().strip())
        result = subprocess.run(
            [sys.executable, str(work / "clawx.py"), "stop"],
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert wait_for_log_contains(mock_log, "MOCK_TERM", timeout=3), \
            "Mock did not receive SIGTERM after stop command"


# ============================================================================
# PATH RESOLUTION TEST
# ============================================================================

class TestPathResolution:
    """Test 5: claude binary path resolution."""

    def test_absolute_path_works(self, tmp_path):
        """Mock is referenced by absolute path — should work directly."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log)  # uses absolute MOCK_CLAUDE path
        work = setup_workdir(tmp_path, config)
        proc = launch_clawx(work, mock_log)
        try:
            assert wait_for_log_contains(mock_log, "MOCK_STARTED", timeout=5)
        finally:
            stop_clawx(proc)

    def test_missing_command_fails_clean(self, tmp_path):
        """Bogus command should raise FileNotFoundError, not segfault."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log)
        config["claude"]["command"] = "nonexistent_xyz_12345"
        work = setup_workdir(tmp_path, config)
        proc = launch_clawx(work, mock_log)
        try:
            proc.wait(timeout=10)
            stderr = proc.stderr.read().decode()
            assert "not found" in stderr.lower(), f"Expected 'not found' in stderr, got: {stderr}"
        finally:
            stop_clawx(proc)


# ============================================================================
# DEPENDENCY ENFORCEMENT
# ============================================================================

class TestApschedulerRequired:
    """Test: missing apscheduler should hard-fail at startup, not silently skip."""

    def test_missing_apscheduler_aborts_with_message(self, tmp_path):
        """
        Simulate apscheduler not being installed by injecting a sitecustomize.py
        that blocks the apscheduler module via sys.modules sentinels. ClawX should
        exit non-zero with a clear error message instead of running with broken
        scheduling.
        """
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log)
        work = setup_workdir(tmp_path, config)

        # Block apscheduler imports for any python launched with PYTHONPATH=work
        (work / "sitecustomize.py").write_text(
            "import sys\n"
            "for name in list(sys.modules):\n"
            "    if name == 'apscheduler' or name.startswith('apscheduler.'):\n"
            "        del sys.modules[name]\n"
            "for name in [\n"
            "    'apscheduler',\n"
            "    'apscheduler.schedulers',\n"
            "    'apscheduler.schedulers.background',\n"
            "    'apscheduler.triggers',\n"
            "    'apscheduler.triggers.cron',\n"
            "]:\n"
            "    sys.modules[name] = None\n"
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(work)
        env["MOCK_LOG"] = str(mock_log)

        # Use `stop` subcommand so we don't need a full PTY session — it still
        # imports clawx.py top-level (which is where the apscheduler check lives).
        result = subprocess.run(
            [sys.executable, str(work / "clawx.py"), "stop"],
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode != 0, \
            f"Expected non-zero exit when apscheduler missing, got 0. stdout={result.stdout!r} stderr={result.stderr!r}"
        combined = (result.stdout + result.stderr).lower()
        assert "apscheduler" in combined, \
            f"Expected 'apscheduler' in error output, got: {combined}"


# ============================================================================
# SCHEDULE TESTS (internals + smoke)
# ============================================================================

class TestSchedule:
    """Test 6-8: APScheduler integration."""

    def test_schedule_registered(self, tmp_path, monkeypatch):
        """Schedules from config should be registered with the scheduler."""
        mock_log = tmp_path / "mock.log"
        schedule = {
            "test_job": {
                "enabled": True,
                "cron": "*/5 * * * *",
                "prompt": "test prompt",
            }
        }
        config = make_config(mock_log, schedule=schedule)
        work = setup_workdir(tmp_path, config)

        # Import clawx as module
        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        try:
            instance = clawx_mod.ClawX()
            instance._setup_schedules()
            assert instance.scheduler is not None
            jobs = instance.scheduler.get_jobs()
            assert any(j.id == "test_job" for j in jobs), \
                f"test_job not in scheduler jobs: {[j.id for j in jobs]}"
            instance.scheduler.shutdown(wait=False)
        finally:
            sys.path.remove(str(work))
            del sys.modules["clawx"]

    def test_schedule_disabled_not_registered(self, tmp_path, monkeypatch):
        mock_log = tmp_path / "mock.log"
        schedule = {
            "disabled_job": {
                "enabled": False,
                "cron": "*/5 * * * *",
                "prompt": "should not run",
            }
        }
        config = make_config(mock_log, schedule=schedule)
        work = setup_workdir(tmp_path, config)

        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        try:
            instance = clawx_mod.ClawX()
            instance._setup_schedules()
            jobs = instance.scheduler.get_jobs()
            assert not any(j.id == "disabled_job" for j in jobs), \
                "Disabled job should not be registered"
            instance.scheduler.shutdown(wait=False)
        finally:
            sys.path.remove(str(work))
            del sys.modules["clawx"]

    def test_run_scheduled_injects_prompt(self, running_clawx):
        """Directly invoke _run_scheduled to verify it injects via FIFO path."""
        proc, work, mock_log = running_clawx
        # Inject a "scheduled-style" message via FIFO (this is what _run_scheduled does)
        fifo = work / "mono.fifo"
        with open(str(fifo), "w") as f:
            f.write("SCHEDULED_TEST_PROMPT\n")
        assert wait_for_log_contains(mock_log, "RECV: SCHEDULED_TEST_PROMPT", timeout=3)


# ============================================================================
# SIGHUP RELOAD TESTS
# ============================================================================

class TestReloadSchedules:
    """Hot-reload via _reload_schedules(): add/remove/modify jobs without restart."""

    def _import_clawx(self, work, monkeypatch):
        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        return clawx_mod

    def _cleanup(self, work):
        if str(work) in sys.path:
            sys.path.remove(str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]

    def _rewrite_schedule(self, work, mock_log, schedule):
        """Overwrite work/config.json with a new schedule."""
        cfg = make_config(mock_log, schedule=schedule)
        (work / "config.json").write_text(json.dumps(cfg, indent=2))

    def test_initial_load_skips_disabled(self, tmp_path, monkeypatch):
        """Initial _setup_schedules loads enabled jobs and skips disabled ones."""
        mock_log = tmp_path / "mock.log"
        schedule = {
            "heartbeat": {"enabled": True,  "cron": "*/30 * * * *", "prompt": "ping"},
            "old-job":   {"enabled": True,  "cron": "0 9 * * *",    "prompt": "old"},
            "disabled":  {"enabled": False, "cron": "0 0 * * *",    "prompt": "off"},
        }
        config = make_config(mock_log, schedule=schedule)
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            inst._setup_schedules()
            job_ids = {j.id for j in inst.scheduler.get_jobs()}
            assert job_ids == {"heartbeat", "old-job"}, f"Got: {job_ids}"
            inst.scheduler.shutdown(wait=False)
        finally:
            self._cleanup(work)

    def test_reload_adds_removes_modifies(self, tmp_path, monkeypatch):
        """Reload picks up added jobs, drops removed jobs, and updates cron changes."""
        mock_log = tmp_path / "mock.log"
        initial = {
            "heartbeat": {"enabled": True, "cron": "*/30 * * * *", "prompt": "ping"},
            "old-job":   {"enabled": True, "cron": "0 9 * * *",    "prompt": "old"},
        }
        config = make_config(mock_log, schedule=initial)
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            inst._setup_schedules()

            # Rewrite: heartbeat cron changes, old-job removed, new-job added
            self._rewrite_schedule(work, mock_log, {
                "heartbeat": {"enabled": True, "cron": "*/15 * * * *",  "prompt": "ping"},
                "new-job":   {"enabled": True, "cron": "28 10 * * 1-6", "prompt": "morning"},
            })
            inst._reload_schedules()

            jobs = {j.id: str(j.trigger) for j in inst.scheduler.get_jobs()}
            assert set(jobs.keys()) == {"heartbeat", "new-job"}, f"Got: {set(jobs.keys())}"
            assert "*/15" in jobs["heartbeat"], \
                f"heartbeat cron didn't update: {jobs['heartbeat']}"
            inst.scheduler.shutdown(wait=False)
        finally:
            self._cleanup(work)

    def test_reload_empty_schedule_clears_jobs(self, tmp_path, monkeypatch):
        """Reloading with an empty schedule removes all jobs."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, schedule={
            "job1": {"enabled": True, "cron": "0 0 * * *", "prompt": "x"},
        })
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            inst._setup_schedules()
            assert len(inst.scheduler.get_jobs()) == 1

            self._rewrite_schedule(work, mock_log, {})
            inst._reload_schedules()
            assert len(inst.scheduler.get_jobs()) == 0
            inst.scheduler.shutdown(wait=False)
        finally:
            self._cleanup(work)

    def test_reload_broken_json_does_not_crash(self, tmp_path, monkeypatch):
        """Reload with invalid JSON must not raise — scheduler stays alive."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, schedule={
            "job": {"enabled": True, "cron": "0 0 * * *", "prompt": "x"},
        })
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            inst._setup_schedules()

            # Corrupt the config file
            (work / "config.json").write_text("{ this is not valid json")

            # Must not raise
            inst._reload_schedules()

            # Scheduler instance still usable
            assert inst.scheduler is not None
            inst.scheduler.shutdown(wait=False)
        finally:
            self._cleanup(work)

    def test_reload_before_setup_is_safe(self, tmp_path, monkeypatch):
        """Calling _reload_schedules before _setup_schedules must not crash."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, schedule={
            "job": {"enabled": True, "cron": "0 0 * * *", "prompt": "x"},
        })
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            # Deliberately skip _setup_schedules
            inst._reload_schedules()  # should warn + no-op, not raise
            assert inst.scheduler is None
        finally:
            self._cleanup(work)

    def test_reload_production_like_multiple_jobs(self, tmp_path, monkeypatch):
        """Reload handles a production-style schedule with multiple named jobs."""
        mock_log = tmp_path / "mock.log"
        prod = {
            "heartbeat": {
                "enabled": True,
                "cron": "*/30 * * * *",
                "prompt": "Read HEARTBEAT.md if it exists. Follow it strictly.",
            },
            "ema530-morning-report": {
                "enabled": True,
                "cron": "28 10 * * 1-6",
                "prompt": "Run morning report.",
            },
        }
        config = make_config(mock_log, schedule=prod)
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            inst._setup_schedules()
            expected = {"heartbeat", "ema530-morning-report"}
            assert {j.id for j in inst.scheduler.get_jobs()} == expected

            # Reloading the same config should leave the job set unchanged
            inst._reload_schedules()
            assert {j.id for j in inst.scheduler.get_jobs()} == expected
            inst.scheduler.shutdown(wait=False)
        finally:
            self._cleanup(work)


# ============================================================================
# CONFIG / BUILD COMMAND TESTS
# ============================================================================

class TestBuildCommand:
    """Test 17-20: Verifying build_command() honors all config flags."""

    def _import_clawx(self, work, monkeypatch):
        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        return clawx_mod

    def _cleanup(self, work):
        sys.path.remove(str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]

    def test_resume_last_true_adds_continue(self, tmp_path, monkeypatch):
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, resume_last=True)
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            cmd = clawx_mod.ClawX().build_command()
            assert "--continue" in cmd, f"--continue missing from cmd: {cmd}"
        finally:
            self._cleanup(work)

    def test_resume_last_false_omits_continue(self, tmp_path, monkeypatch):
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, resume_last=False)
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            cmd = clawx_mod.ClawX().build_command()
            assert "--continue" not in cmd
        finally:
            self._cleanup(work)

    def test_custom_model_in_cmd(self, tmp_path, monkeypatch):
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, model="sonnet")
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            cmd = clawx_mod.ClawX().build_command()
            assert "--model" in cmd
            idx = cmd.index("--model")
            assert cmd[idx + 1] == "sonnet"
        finally:
            self._cleanup(work)

    def test_extra_args_passthrough(self, tmp_path, monkeypatch):
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, extra_args=["--foo", "bar", "--baz"])
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            cmd = clawx_mod.ClawX().build_command()
            assert "--foo" in cmd and "bar" in cmd and "--baz" in cmd
        finally:
            self._cleanup(work)


# ============================================================================
# RUN_ONESHOT COMMAND RESOLUTION
# ============================================================================

class TestRunOneshot:
    """run_oneshot must resolve the command path the same way build_command does."""

    def _import_clawx(self, work, monkeypatch):
        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        return clawx_mod

    def _cleanup(self, work):
        if str(work) in sys.path:
            sys.path.remove(str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]

    def test_oneshot_missing_command_raises(self, tmp_path, monkeypatch):
        """run_oneshot with a bogus command should raise FileNotFoundError."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log)
        config["claude"]["command"] = "nonexistent_xyz_99999"
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            with pytest.raises(FileNotFoundError, match="not found"):
                clawx_mod.run_oneshot("hello")
        finally:
            self._cleanup(work)

    def test_oneshot_resolves_absolute_path(self, tmp_path, monkeypatch):
        """run_oneshot with an absolute mock path should resolve it."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log)
        # MOCK_CLAUDE is already absolute — run_oneshot should find it
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            # We can't actually run it (mock_claude.sh blocks on stdin),
            # but _resolve_command should find it.
            resolved = clawx_mod._resolve_command(str(MOCK_CLAUDE))
            assert resolved is not None
        finally:
            self._cleanup(work)


# ============================================================================
# TRANSCRIPT TEST
# ============================================================================

class TestTranscript:
    """Test 15: Transcript file captures session output."""

    def test_transcript_file_created(self, running_clawx):
        proc, work, mock_log = running_clawx
        # Give ClawX a moment to write transcript
        time.sleep(0.5)
        transcripts = list((work / "logs").glob("transcript-*.log"))
        assert transcripts, "No transcript-*.log file created"


# ============================================================================
# CLEANUP TEST
# ============================================================================

class TestCleanup:
    """Test 21: Clean shutdown removes PID file."""

    def test_pid_file_removed_after_shutdown(self, workdir):
        work, mock_log = workdir
        proc = launch_clawx(work, mock_log)
        assert wait_for_log_contains(mock_log, "MOCK_STARTED", timeout=5)
        assert (work / "mono.pid").exists()
        stop_clawx(proc)
        # Give cleanup a moment
        time.sleep(0.3)
        assert not (work / "mono.pid").exists(), \
            "PID file should be removed after clean shutdown"


# ============================================================================
# COMPACT WATCHER TESTS
# ============================================================================

class TestCompactWatcher:
    """CompactWatcher tails Claude Code session JSONL files and fires a
    callback when a ``compact_boundary`` event appears. The feature's purpose
    is to restore agent identity (re-read AGENTS.md) and notify the user over
    Telegram *right after* Claude auto-compacts the context.

    Tests use a tmp sessions dir and fake .jsonl files — no real Claude
    sessions are touched.
    """

    def _import_clawx(self, work, monkeypatch):
        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        return clawx_mod

    def _cleanup(self, work):
        if str(work) in sys.path:
            sys.path.remove(str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]

    def _make_watcher(self, clawx_mod, sessions_dir):
        """Build a CompactWatcher with a capturing on_compact callback."""
        events = []

        def handler(evt):
            events.append(evt)

        logger = logging.getLogger("TestCompactWatcher")
        logger.addHandler(logging.NullHandler())
        watcher = clawx_mod.CompactWatcher(
            sessions_dir=str(sessions_dir),
            logger=logger,
            on_compact=handler,
        )
        return watcher, events

    def _make_compact_line(self, uuid="evt-1", pre_tokens=179131,
                           session_id="sess-a", ts="2026-04-08T17:22:33.947Z"):
        return json.dumps({
            "parentUuid": None,
            "isSidechain": False,
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "timestamp": ts,
            "uuid": uuid,
            "level": "info",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": pre_tokens,
                "preCompactDiscoveredTools": ["TaskCreate", "TaskList"],
            },
            "sessionId": session_id,
            "cwd": "/home/ymchang/clawd",
            "version": "2.1.96",
        }) + "\n"

    def _make_user_line(self, uuid="user-1"):
        return json.dumps({
            "type": "user",
            "uuid": uuid,
            "message": {"role": "user", "content": "hello"},
        }) + "\n"

    # --- resolve helper --------------------------------------------------

    def test_resolve_sessions_dir_from_project(self, tmp_path, monkeypatch):
        """``resolve_sessions_dir_from_project`` turns /a/b/c into
        ~/.claude/projects/-a-b-c (Claude Code's slug convention)."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            result = clawx_mod.CompactWatcher.resolve_sessions_dir_from_project(
                "/home/ymchang/clawd"
            )
            expected = Path.home() / ".claude" / "projects" / "-home-ymchang-clawd"
            assert result == expected
        finally:
            self._cleanup(work)

    # --- detection & dedup -----------------------------------------------

    def test_ignores_historical_events_on_first_scan(self, tmp_path, monkeypatch):
        """Existing content in session files when the watcher starts up
        MUST NOT fire the callback — otherwise every ClawX start would
        re-trigger past compacts."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text(self._make_compact_line("old-1"))

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()
            assert events == [], "Historical events must not fire the callback"
        finally:
            self._cleanup(work)

    def test_detects_new_compact_event_appended_after_start(self, tmp_path, monkeypatch):
        """A compact_boundary line appended AFTER the watcher has baseline'd
        the file must fire the callback exactly once."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text(self._make_user_line("u1"))

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()  # baseline existing content
            assert events == []

            # Append a compact event
            with open(jsonl, "a") as f:
                f.write(self._make_compact_line("evt-new", pre_tokens=188063))
            watcher._scan_once()

            assert len(events) == 1
            assert events[0]["subtype"] == "compact_boundary"
            assert events[0]["uuid"] == "evt-new"
        finally:
            self._cleanup(work)

    def test_passes_full_event_including_metadata(self, tmp_path, monkeypatch):
        """The callback must receive the full parsed JSON, including
        compactMetadata so downstream handlers can read preTokens."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text("")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()

            with open(jsonl, "a") as f:
                f.write(self._make_compact_line("evt-meta", pre_tokens=179131))
            watcher._scan_once()

            assert len(events) == 1
            meta = events[0].get("compactMetadata")
            assert meta is not None
            assert meta["preTokens"] == 179131
            assert meta["trigger"] == "auto"
        finally:
            self._cleanup(work)

    def test_dedups_same_event_uuid(self, tmp_path, monkeypatch):
        """If the same compact_boundary uuid shows up twice (e.g. after a
        file rewrite/truncate re-read), it must only fire once."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text("")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()

            with open(jsonl, "a") as f:
                f.write(self._make_compact_line("same-uuid"))
            watcher._scan_once()

            # Force re-read of same content by resetting position
            watcher.file_positions[jsonl] = 0
            watcher._scan_once()

            assert len(events) == 1, \
                f"Same UUID fired {len(events)} times; expected 1"
        finally:
            self._cleanup(work)

    def test_ignores_non_compact_system_events(self, tmp_path, monkeypatch):
        """Other event types (user messages, regular system events) must
        not fire the compact callback."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text("")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()

            lines = [
                self._make_user_line("u1"),
                json.dumps({"type": "system", "subtype": "hook_call", "uuid": "h1"}) + "\n",
                json.dumps({"type": "assistant", "uuid": "a1"}) + "\n",
            ]
            with open(jsonl, "a") as f:
                f.writelines(lines)
            watcher._scan_once()

            assert events == []
        finally:
            self._cleanup(work)

    def test_handles_malformed_json_line(self, tmp_path, monkeypatch):
        """A truncated or malformed JSON line must be skipped without
        crashing. The next valid compact event must still fire."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text("")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()

            with open(jsonl, "a") as f:
                f.write('{"type": "system", "subtype": "compact_bound\n')  # truncated
                f.write(self._make_compact_line("good-evt"))
            watcher._scan_once()

            assert len(events) == 1
            assert events[0]["uuid"] == "good-evt"
        finally:
            self._cleanup(work)

    def test_handler_exception_does_not_crash_scan(self, tmp_path, monkeypatch):
        """If the on_compact callback raises, the watcher must log and
        continue — a crashing handler must not kill the background thread."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl = sessions / "sess-a.jsonl"
        jsonl.write_text("")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            call_count = {"n": 0}

            def bad_handler(evt):
                call_count["n"] += 1
                raise RuntimeError("simulated handler failure")

            logger = logging.getLogger("TestCompactWatcherBadHandler")
            logger.addHandler(logging.NullHandler())
            watcher = clawx_mod.CompactWatcher(
                sessions_dir=str(sessions),
                logger=logger,
                on_compact=bad_handler,
            )
            watcher._scan_once()

            with open(jsonl, "a") as f:
                f.write(self._make_compact_line("evt-1"))
                f.write(self._make_compact_line("evt-2"))
            watcher._scan_once()  # must not raise

            # Both events should have reached the handler even though it raised
            assert call_count["n"] == 2
        finally:
            self._cleanup(work)

    def test_missing_sessions_dir_does_not_crash(self, tmp_path, monkeypatch):
        """If the sessions dir doesn't exist (Claude hasn't created it yet),
        _scan_once must be a no-op, not a crash."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions-never-created"  # deliberately missing

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()  # must not raise
            assert events == []
        finally:
            self._cleanup(work)

    def test_handles_multiple_session_files_independently(self, tmp_path, monkeypatch):
        """Multiple .jsonl files in the sessions dir must each be tracked
        with their own read position."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        jsonl_a = sessions / "sess-a.jsonl"
        jsonl_b = sessions / "sess-b.jsonl"
        jsonl_a.write_text("")
        jsonl_b.write_text("")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            watcher, events = self._make_watcher(clawx_mod, sessions)
            watcher._scan_once()

            with open(jsonl_a, "a") as f:
                f.write(self._make_compact_line("a-evt", session_id="sess-a"))
            with open(jsonl_b, "a") as f:
                f.write(self._make_compact_line("b-evt", session_id="sess-b"))
            watcher._scan_once()

            uuids = {e["uuid"] for e in events}
            assert uuids == {"a-evt", "b-evt"}
        finally:
            self._cleanup(work)

    # --- ClawX integration (on_compact wiring) ---------------------------

    def test_clawx_on_compact_injects_agents_md_and_notifies(self, tmp_path, monkeypatch):
        """ClawX._on_compact must (1) inject an AGENTS.md re-read prompt
        (internal, keeps identity) and (2) call _notify_compact for the
        user-facing Telegram notification. Inject MUST happen first so the
        identity prompt is queued before the notification side-effect."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            call_order = []

            def fake_inject(text):
                call_order.append(("inject", text))
                return True

            def fake_notify(evt):
                call_order.append(("notify", evt.get("compactMetadata", {}).get("preTokens")))

            inst.inject = fake_inject
            inst._notify_compact = fake_notify

            fake_event = json.loads(self._make_compact_line("evt-int", pre_tokens=188063))
            inst._on_compact(fake_event)

            # inject must come first, notify second
            assert len(call_order) == 2, f"Expected 2 calls, got {call_order}"
            assert call_order[0][0] == "inject"
            assert "AGENTS.md" in call_order[0][1]
            assert call_order[1] == ("notify", 188063)
        finally:
            self._cleanup(work)


# ============================================================================
# TELEGRAM PREREQ CHECK TESTS
# ============================================================================

class TestTelegramPrereqs:
    """ClawX._check_telegram_prereqs() inspects the Telegram plugin setup
    on startup and logs a warning if something will prevent the bot from
    talking. It does NOT install anything. The log file is the only
    surface — user reads it when something's off.

    Two failure modes it catches:
      1. bun runtime not on PATH AND not in common install locations
         → the Telegram plugin's MCP server can't launch at all.
      2. bun is installed at ~/.bun/bin/bun (common WSL case) but the
         plugin's .mcp.json still has bare ``"command": "bun"`` → Claude
         Code will fail to spawn the MCP with "bun: not found".
    """

    def _import_clawx(self, work, monkeypatch):
        monkeypatch.chdir(work)
        sys.path.insert(0, str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]
        import clawx as clawx_mod
        return clawx_mod

    def _cleanup(self, work):
        if str(work) in sys.path:
            sys.path.remove(str(work))
        if "clawx" in sys.modules:
            del sys.modules["clawx"]

    def _write_plugin_mcp(self, plugin_dir, command_value):
        """Write a telegram plugin .mcp.json with the given command value."""
        plugin_dir.mkdir(parents=True, exist_ok=True)
        mcp = {
            "mcpServers": {
                "telegram": {
                    "command": command_value,
                    "args": ["run", "--cwd", "${CLAUDE_PLUGIN_ROOT}", "start"],
                }
            }
        }
        (plugin_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2))

    def _capture_warnings(self, inst):
        """Capture warning-level log records from the ClawX instance logger."""
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        h = _Capture()
        h.setLevel(logging.WARNING)
        inst.logger.addHandler(h)
        return records

    # --- telegram detection logic ---------------------------------------

    def test_no_telegram_channel_skips_check(self, tmp_path, monkeypatch):
        """No --channels plugin:telegram in extra_args → no prereq check,
        no warnings, no crash even if bun is missing from the planet."""
        mock_log = tmp_path / "mock.log"
        config = make_config(mock_log, extra_args=[])  # no telegram
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            records = self._capture_warnings(inst)
            # Bun detection hook returns None (missing) — must still not warn
            inst._check_telegram_prereqs(
                bun_finder=lambda: None,
                plugin_cache_dir=tmp_path / "nonexistent",
            )
            assert records == [], \
                f"No warnings expected with no telegram channel, got: " \
                f"{[r.getMessage() for r in records]}"
        finally:
            self._cleanup(work)

    # --- bun missing case ------------------------------------------------

    def test_bun_missing_logs_install_instructions(self, tmp_path, monkeypatch):
        """Telegram channel enabled + bun not found anywhere → must log a
        WARNING containing the official install command so the user can
        fix it. No auto-install."""
        mock_log = tmp_path / "mock.log"
        config = make_config(
            mock_log,
            extra_args=["--channels", "plugin:telegram@claude-plugins-official"],
        )
        work = setup_workdir(tmp_path, config)
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            records = self._capture_warnings(inst)
            inst._check_telegram_prereqs(
                bun_finder=lambda: None,
                plugin_cache_dir=tmp_path / "plugins-cache",
            )
            assert len(records) >= 1, "Expected a WARNING about missing bun"
            msg = " ".join(r.getMessage() for r in records)
            assert "bun" in msg.lower()
            # The install command from bun.sh must be in the message
            assert "bun.sh/install" in msg, \
                f"Expected install URL in warning, got: {msg}"
        finally:
            self._cleanup(work)

    # --- bun in PATH + .mcp.json happy path -----------------------------

    def test_bun_in_path_with_correct_mcp_no_warning(self, tmp_path, monkeypatch):
        """Telegram channel enabled + bun in PATH + .mcp.json uses bare
        "bun" (works because it's in PATH) → no warning."""
        mock_log = tmp_path / "mock.log"
        config = make_config(
            mock_log,
            extra_args=["--channels", "plugin:telegram@claude-plugins-official"],
        )
        work = setup_workdir(tmp_path, config)
        plugin_cache = tmp_path / "plugins-cache"
        plugin_dir = plugin_cache / "claude-plugins-official" / "telegram" / "0.0.4"
        self._write_plugin_mcp(plugin_dir, "bun")

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            records = self._capture_warnings(inst)
            # bun_finder returns "bun" signalling it's in PATH
            inst._check_telegram_prereqs(
                bun_finder=lambda: "bun",
                plugin_cache_dir=plugin_cache,
            )
            assert records == [], \
                f"No warning expected (bun in PATH + bare bun in mcp), got: " \
                f"{[r.getMessage() for r in records]}"
        finally:
            self._cleanup(work)

    # --- bun installed but not in PATH + .mcp.json still bare ----------

    def test_bun_home_but_mcp_bare_logs_patch_instruction(self, tmp_path, monkeypatch):
        """The most common WSL case: user installed bun via curl | bash,
        it sits at ~/.bun/bin/bun, but the telegram plugin's .mcp.json
        still has bare "bun" which will fail because ~/.bun/bin isn't in
        Claude's PATH. WARN with the exact sed/jq command (or just the
        path the user needs to set)."""
        mock_log = tmp_path / "mock.log"
        config = make_config(
            mock_log,
            extra_args=["--channels", "plugin:telegram@claude-plugins-official"],
        )
        work = setup_workdir(tmp_path, config)
        plugin_cache = tmp_path / "plugins-cache"
        plugin_dir = plugin_cache / "claude-plugins-official" / "telegram" / "0.0.4"
        self._write_plugin_mcp(plugin_dir, "bun")

        fake_bun_abs = tmp_path / "fake-bun" / "bin" / "bun"
        fake_bun_abs.parent.mkdir(parents=True)
        fake_bun_abs.write_text("#!/bin/sh\nexit 0\n")
        fake_bun_abs.chmod(0o755)

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            records = self._capture_warnings(inst)
            # bun_finder returns absolute path (bun NOT in PATH, found in home)
            inst._check_telegram_prereqs(
                bun_finder=lambda: str(fake_bun_abs),
                plugin_cache_dir=plugin_cache,
            )
            assert len(records) >= 1, \
                "Expected a WARNING about bare bun in .mcp.json"
            msg = " ".join(r.getMessage() for r in records)
            # Message must reference the absolute bun path the user should use
            assert str(fake_bun_abs) in msg, \
                f"Expected absolute bun path in warning, got: {msg}"
            # Message must point at the actual .mcp.json file to patch
            assert ".mcp.json" in msg
        finally:
            self._cleanup(work)

    # --- .mcp.json already patched → no warning -------------------------

    def test_bun_home_with_mcp_already_absolute_no_warning(self, tmp_path, monkeypatch):
        """User already patched .mcp.json to use absolute bun path → no
        warning even though bun isn't in PATH. This is the known-good
        state Ryan is already running in."""
        mock_log = tmp_path / "mock.log"
        config = make_config(
            mock_log,
            extra_args=["--channels", "plugin:telegram@claude-plugins-official"],
        )
        work = setup_workdir(tmp_path, config)
        plugin_cache = tmp_path / "plugins-cache"
        plugin_dir = plugin_cache / "claude-plugins-official" / "telegram" / "0.0.4"

        fake_bun_abs = tmp_path / "fake-bun" / "bin" / "bun"
        fake_bun_abs.parent.mkdir(parents=True)
        fake_bun_abs.write_text("#!/bin/sh\nexit 0\n")
        fake_bun_abs.chmod(0o755)

        # .mcp.json uses the absolute path — already good
        self._write_plugin_mcp(plugin_dir, str(fake_bun_abs))

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            records = self._capture_warnings(inst)
            inst._check_telegram_prereqs(
                bun_finder=lambda: str(fake_bun_abs),
                plugin_cache_dir=plugin_cache,
            )
            assert records == [], \
                f"No warning expected (already patched), got: " \
                f"{[r.getMessage() for r in records]}"
        finally:
            self._cleanup(work)

    # --- plugin not yet installed → silent skip -------------------------

    def test_plugin_not_yet_installed_silent_skip(self, tmp_path, monkeypatch):
        """Telegram channel configured but the plugin cache dir doesn't
        exist yet (Claude Code will fetch it on first run). Skip the
        .mcp.json check silently — complaining about a file that will
        exist in a minute is noise."""
        mock_log = tmp_path / "mock.log"
        config = make_config(
            mock_log,
            extra_args=["--channels", "plugin:telegram@claude-plugins-official"],
        )
        work = setup_workdir(tmp_path, config)

        fake_bun_abs = tmp_path / "fake-bun" / "bin" / "bun"
        fake_bun_abs.parent.mkdir(parents=True)
        fake_bun_abs.write_text("#!/bin/sh\nexit 0\n")
        fake_bun_abs.chmod(0o755)

        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            records = self._capture_warnings(inst)
            # bun exists, plugin dir doesn't
            inst._check_telegram_prereqs(
                bun_finder=lambda: str(fake_bun_abs),
                plugin_cache_dir=tmp_path / "nonexistent-plugin-cache",
            )
            assert records == [], \
                f"No warning expected (plugin not yet installed), got: " \
                f"{[r.getMessage() for r in records]}"
        finally:
            self._cleanup(work)

    # --- default bun_finder works (integration-y) -----------------------

    def test_default_bun_finder_resolves_path_or_home(self, tmp_path, monkeypatch):
        """_find_bun() (the real one, no injection) must return either a
        PATH-resolved ``bun``, an absolute path to ~/.bun/bin/bun, or None.
        Never raise, never return something else."""
        mock_log = tmp_path / "mock.log"
        work = setup_workdir(tmp_path, make_config(mock_log))
        clawx_mod = self._import_clawx(work, monkeypatch)
        try:
            inst = clawx_mod.ClawX()
            result = inst._find_bun()
            # Either None (missing) or a truthy string
            assert result is None or (isinstance(result, str) and result)
        finally:
            self._cleanup(work)


# ============================================================================
# Notes on tests we DID NOT include and why
# ============================================================================
#
# - test_schedule_actually_fires:
#     Cron parser uses 5-field minute granularity, smallest interval is 1 min.
#     Would require ~65s wait. Skipped to keep suite fast; covered indirectly
#     by test_run_scheduled_injects_prompt + test_schedule_registered.
#
# - test_auto_restart_on_crash:
#     Auto-restart logic is gated by health_check_interval (min 1s in test cfg)
#     and restart_delay_seconds. Also needs CRASH command sent then waited on.
#     Doable but flaky on slow CI; left as a manual test scenario.
#
# - test_sigwinch_forwarding:
#     Hard to verify deterministically — would need to inspect child's TIOCGWINSZ
#     which requires reading the PTY slave from outside. Skipped.
#
# - test_max_restarts_gives_up:
#     Same as auto-restart — timing dependent. Note for future suite.


# ============================================================================
# Allow direct execution: `python3 tests/test_clawx.py`
# ============================================================================

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
