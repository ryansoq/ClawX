#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "apscheduler>=3.0",
# ]
# ///
"""
ClawX: Thin PTY wrapper around Claude Code CLI.

Renders Claude's interactive UI exactly as-is. Adds:
  - Inject messages via FIFO pipe (echo "hello" > mono.fifo)
  - Scheduled prompts via apscheduler (config.json)
  - Auto-restart on crash
  - Log transcript to file

Usage:
    python clawx.py                  # Start interactive session (PTY passthrough)
    python clawx.py inject "msg"     # Inject a message into running session via FIFO
    python clawx.py stop             # Gracefully stop session
    python clawx.py restart          # Self-restart: stop then relaunch in same terminal
    python clawx.py prompt "text"    # One-shot: run prompt via -p mode, print result
    python clawx.py replay <file>    # Parse transcript log, annotate events, output clean text
    python clawx.py --no-continue    # Start fresh session (ignore resume_last config)
    python clawx.py -nc              # Short form of --no-continue
    python clawx.py --continue       # Force resume session (ignore resume_last=false in config)
    python clawx.py -c               # Short form of --continue
"""

import json
import sys
import os
import pty
import re
import time
import signal
import select
import struct
import fcntl
import termios
import logging
import shutil
from pathlib import Path
from datetime import datetime
from threading import Thread, Event, Lock

# Required: scheduling is core to ClawX (heartbeat, cron jobs).
# Hard-fail at startup if apscheduler is missing so users don't silently
# lose heartbeat behavior.
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
except ImportError:
    sys.stderr.write(
        "\nError: apscheduler is required but not installed.\n\n"
        "Install with one of:\n"
        "  pip install apscheduler\n"
        "  uv pip install apscheduler\n\n"
        "Or run ClawX with uv (auto-installs deps from PEP 723 metadata):\n"
        "  uv run clawx.py\n\n"
    )
    sys.exit(1)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
PID_FILE = BASE_DIR / "mono.pid"          # ClawX (parent) PID
CHILD_PID_FILE = BASE_DIR / "mono-child.pid"  # Claude (child) PID
FIFO_PATH = BASE_DIR / "mono.fifo"
LOG_DIR = BASE_DIR / "logs"
RESTART_EXIT_CODE = 42

# Modal-prompt detection: strip ANSI escape sequences before pattern matching
# so colored TUI output doesn't fool the detector.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
# How long after Claude spawn we keep watching for a startup modal.
STARTUP_MODAL_WINDOW_SECONDS = 60
# Cap on buffer size we keep for detection (Claude's startup output is small).
STARTUP_MODAL_BUFFER_LIMIT = 32 * 1024


def detect_startup_modal(buf: bytes):
    """Detect a startup modal numbered-choice prompt in PTY output.

    Claude Code with ``--continue`` may show an auto-compact prompt at
    session resume. Without a human at the terminal that dialog blocks
    the session forever. This detector spots the prompt so ClawX can
    auto-select the safest option (last numbered choice = "do nothing").

    Strategy: ANSI-strip the buffer, require BOTH:
      1. A context keyword ("compact" / "summarize" / "auto-compact")
      2. At least 2 distinct numbered options at start-of-line

    To avoid false positives from code diffs and conversation text,
    options must appear at line start (with optional whitespace) —
    not embedded in diff line numbers like "117 +".

    Returns the highest option number detected (the conventional
    "skip / leave alone" slot in 3-option Claude prompts), or None
    if no modal is present.
    """
    if not buf:
        return None
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", errors="replace").lower()
    if not any(kw in text for kw in ("compact", "summarize", "auto-compact")):
        return None
    # Reject if this looks like a code diff (line numbers like "117 +")
    if re.search(r"\d{2,}\s+[+\-]\s", text):
        return None
    numbers = set()
    # Match options at line start: optional whitespace/cursor (> or ❯), then digit
    for match in re.finditer(r"(?:^|[\n\r])[\s>❯]*([1-9])[.)\]]", text):
        numbers.add(int(match.group(1)))
    if len(numbers) < 2:
        return None
    return max(numbers)


def _find_active_session_jsonl():
    """Return the most recently modified Claude session jsonl, or None.

    Claude CLI writes one jsonl per session under
    ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``. ClawX
    runs a single interactive session at a time, so the most-recently
    modified file across all project dirs is the active session.
    """
    proj_root = Path.home() / ".claude" / "projects"
    if not proj_root.exists():
        return None
    candidates = list(proj_root.rglob("*.jsonl"))
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def _verify_compact_in_jsonl(window_seconds: int = 120) -> bool:
    """Ground-truth check: did Claude CLI actually write a compact summary?

    Claude CLI marks a real compaction by writing a jsonl entry with
    ``isCompactSummary: true`` *before* rendering the banner to the PTY
    (observed: ~3 ms lead). Checking the jsonl tail for a recent such
    entry gives us a ground truth the PTY text match can't provide.

    Returns True only if the jsonl's tail contains a compact summary
    entry whose timestamp is within ``window_seconds`` of now.
    """
    jsonl = _find_active_session_jsonl()
    if jsonl is None:
        return False
    try:
        with open(jsonl, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 32 * 1024)
            f.seek(size - chunk)
            tail = f.read()
    except OSError:
        return False
    now = time.time()
    for raw in tail.splitlines()[-10:]:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not entry.get("isCompactSummary"):
            continue
        ts = entry.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = now - dt.timestamp()
        except ValueError:
            continue
        if -30 <= age <= window_seconds:
            return True
    return False


def detect_compact_event(buf: bytes):
    """Detect a compact notification in PTY output.

    When Claude Code auto-compacts context it prints a sparkle-marked
    banner line. We detect real compactions in two stages:

      1. **PTY regex pre-filter** (cheap): sparkle marker + adjacent
         "conversation compacted" words, after ANSI stripping. This
         rejects most noise but can still false-positive when the
         assistant's own text quotes the banner (Claude CLI's TUI
         renders assistant replies back through the PTY, and the
         detector sees its own words echoed).

      2. **JSONL ground-truth verification**: cross-check with
         ``~/.claude/projects/**/*.jsonl`` — Claude CLI writes an
         ``isCompactSummary: true`` entry for real compactions only,
         and that file is not influenced by assistant text. If the
         tail of the active session jsonl has a compact summary
         within the last ~120 s, it's real.

    Both stages must pass. Self-references (assistant quoting the
    banner) pass stage 1 but fail stage 2 because no jsonl summary
    exists. Real system events pass both because Claude writes the
    summary ~3 ms before rendering the banner.

    Returns True if detected, None otherwise.
    """
    if not buf:
        return None
    # Replace ANSI sequences with a space so cursor-moves don't merge words.
    text = _ANSI_RE.sub(b" ", buf).decode("utf-8", errors="replace").lower()
    if not re.search(r"✻\s{0,8}conversation\s{1,8}compacted", text):
        return None
    # Reject diff context: line numbers like "215 +" indicate code, not real events
    if re.search(r"\d{2,}\s+[+\-]\s", text):
        return None
    # Round 3 — jsonl ground-truth gate. Assistant self-references
    # pass the regex but fail here because no real compact summary
    # was written.
    if not _verify_compact_in_jsonl():
        return None
    return True


def detect_rate_limit_modal(buf: bytes):
    """Detect a rate-limit modal in PTY output.

    When Claude Code hits the usage cap it shows a blocking prompt:
        "You've hit your limit · resets 12am (Asia/Taipei)"
        /rate-limit-options
        1. Stop and wait for limit to reset
        2. Upgrade your plan

    To avoid false positives from code diffs containing these keywords
    as string literals (e.g. b"rate-limit-options"), we require both a
    keyword AND start-of-line numbered options, and reject diff context.

    Returns 1 (wait for reset) if detected, None otherwise.
    """
    if not buf:
        return None
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", errors="replace").lower()
    if not any(kw in text for kw in (
        "rate-limit-options",
        "hit your limit",
        "you've hit your limit",
        "wait for limit to reset",
    )):
        return None
    # Reject if this looks like a code diff (line numbers like "117 +")
    if re.search(r"\d{2,}\s+[+\-]\s", text):
        return None
    numbers = set()
    # Match options at line start: optional whitespace/cursor (> or ❯), then digit
    for match in re.finditer(r"(?:^|[\n\r])[\s>❯]*([1-9])[.)\]]", text):
        numbers.add(int(match.group(1)))
    if len(numbers) < 2:
        return None
    # Pick option 1 = "Stop and wait for limit to reset"
    return 1


def detect_resume_modal(buf: bytes):
    """Detect Claude's resume-mode selection modal in PTY output.

    When `claude --continue` is run on a long session, Claude Code
    shows a blocking prompt asking how to resume:

        This session is 2h 14m old and 117.6k tokens.
        Resuming the full session will consume a substantial portion
        of your usage summary.

        1. Resume from summary (recommended)
        2. Resume full session as-is
        3. Don't ask me again

    We auto-select 3 (Don't ask me again) so this never blocks us again.

    Requires BOTH:
      - a keyword phrase ("resume from summary" / "resume full session")
      - a start-of-line numbered "3." option whose text is
        "don't ask me again", which is how the modal is actually rendered.
    This rejects plain prose that just mentions the phrases.

    Returns 3 if detected, None otherwise.
    """
    if not buf:
        return None
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", errors="replace").lower()
    if "resume from summary" not in text and "resume full session" not in text:
        return None
    # Reject diff context (line numbers like "117 +")
    if re.search(r"\d{2,}\s+[+\-]\s", text):
        return None
    # Require a start-of-line "3. Don't ask me again" style option.
    # Matches: "  3. don't ask me again", "❯ 3) dont ask me again", etc.
    if not re.search(
        r"(?:^|[\n\r])[\s>❯]*3[.)\]]\s*don['\u2019]?t ask me again",
        text,
    ):
        return None
    return 3


def detect_feedback_modal(buf: bytes):
    """Detect Claude's session feedback modal in PTY output.

    Claude Code occasionally shows:
        How is Claude doing this session? (optional)
        1: Bad  2: Fine  3: Good  0: Dismiss

    This blocks the session until a choice is made. We auto-dismiss.

    Returns 0 (Dismiss) if detected, None otherwise.
    """
    if not buf:
        return None
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", errors="replace").lower()
    if "how is claude doing" not in text:
        return None
    # Reject diff context
    if re.search(r"\d{2,}\s+[+\-]\s", text):
        return None
    # Check for dismiss option (0: Dismiss)
    if "dismiss" in text:
        return 0
    return None


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def setup_logging(config=None):
    from logging.handlers import RotatingFileHandler

    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"clawx-{datetime.now().strftime('%Y%m%d')}.log"

    log_cfg = (config or {}).get("logging", {})
    max_bytes = log_cfg.get("max_size_mb", 50) * 1024 * 1024
    backup_count = log_cfg.get("rotate_count", 5)

    fh = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] [%(levelname)s] %(message)s"))

    logger = logging.getLogger("ClawX")
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)

    # Also capture apscheduler internals so silent scheduler bugs become visible.
    aps_logger = logging.getLogger("apscheduler")
    aps_logger.setLevel(logging.INFO)
    aps_logger.addHandler(fh)

    return logger


def set_winsize(fd):
    """Copy current terminal size to the PTY."""
    try:
        sz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)
    except Exception:
        # Fallback: 80x24
        sz = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)


    # CompactWatcher (JSONL polling) removed in favour of PTY-based
    # compact detection via detect_compact_event() + master_fd stream.
    # See _maybe_handle_compact() in ClawX class.


class ClawX:
    """PTY-based Claude Code wrapper."""

    # CLI overrides (mutually exclusive):
    #   force_no_continue → skip --continue even if config says resume_last
    #   force_continue    → add --continue even if config says resume_last=false
    force_no_continue = False
    force_continue = False

    def __init__(self):
        self.config = load_config()
        self.logger = setup_logging(self.config)
        self.master_fd = None
        self.child_pid = None
        self.stop_event = Event()
        self.write_lock = Lock()
        self.started_at = None
        self.scheduler = None
        self.restart_count = 0
        self.last_restart_at = None  # Set on every _spawn_claude; used by
                                     # _health_loop to reset restart_count
                                     # after sustained uptime (HIGH #2 fix).
        self.restart_requested = False
        # Scheduler liveness tracking — used by watchdog to detect silent
        # apscheduler death (jobs registered but never fire).
        self.last_job_event_at = None
        # Startup modal detection state. Reset on each _spawn_claude.
        self._startup_buffer = bytearray()
        self._startup_modal_active = False
        self._startup_modal_handled = False
        # Rate-limit modal detection — runs continuously, not just at startup.
        self._ratelimit_buffer = bytearray()
        self._ratelimit_cooldown_until = 0  # epoch; prevent rapid re-fires
        self._feedback_cooldown_until = 0   # epoch; prevent feedback loop
        self._resume_cooldown_until = 0     # epoch; prevent resume-modal loop
        self._resume_buffer = bytearray()   # accumulator for multi-chunk modal
        # Compact detection via PTY stream (replaced JSONL-based CompactWatcher).
        self._compact_buffer = bytearray()
        self._compact_cooldown_until = 0  # epoch; suppress rapid re-fires

    def build_command(self):
        """Build the claude CLI command."""
        cfg = self.config["claude"]
        raw_cmd = cfg["command"]
        resolved = _resolve_command(raw_cmd)
        if resolved is None:
            raise FileNotFoundError(f"Command '{raw_cmd}' not found in PATH or common locations")
        cmd = [resolved]

        # Project directory (resolve to absolute)
        cmd.extend(["--add-dir", self._get_project_dir()])

        # Model
        if cfg.get("model"):
            cmd.extend(["--model", cfg["model"]])

        # Permission mode — default to dangerously-skip-permissions
        if cfg.get("permission_mode"):
            cmd.extend(["--permission-mode", cfg["permission_mode"]])
        else:
            cmd.append("--dangerously-skip-permissions")

        # Resume last session.
        # Precedence: CLI flag > config
        #   -c  / --continue    → force resume (overrides resume_last=false)
        #   -nc / --no-continue → force fresh (overrides resume_last=true)
        #   neither             → honor config["claude"]["resume_last"]
        if self.force_continue:
            cmd.append("--continue")
        elif self.force_no_continue:
            pass
        elif cfg.get("resume_last"):
            cmd.append("--continue")

        # MCP config
        if cfg.get("mcp_config"):
            cmd.extend(["--mcp-config", cfg["mcp_config"]])

        # Extra args
        for arg in cfg.get("extra_args", []):
            cmd.append(arg)

        return cmd

    def inject(self, text):
        """Inject text into Claude's stdin via the PTY master.

        Split the write into two os.write calls — text first, then
        \\r alone with a short delay between. Rationale: writing
        ``text + \\r`` as a single chunk trips Ink's TextInput paste
        heuristic once the payload exceeds ~30-40 bytes; the trailing
        \\r then gets absorbed as a newline-in-paste instead of an
        Enter keystroke, so the prompt deposits into the input box
        but never submits. Empirically reproduced 2026-04-15 with the
        171-byte heartbeat prompt — every cron tick piled up, then an
        unrelated short echo > mono.fifo flushed the whole stack as
        one merged user message. Splitting the write lets Ink drain
        and re-arm between the two calls so the \\r reads as a fresh
        keystroke.
        """
        if self.master_fd is None:
            self.logger.error("No active session")
            return False
        with self.write_lock:
            try:
                os.write(self.master_fd, text.encode("utf-8"))
                time.sleep(0.1)
                os.write(self.master_fd, b"\r")
                self.logger.info(f"[Inject] {text[:200]}")
                return True
            except Exception as e:
                self.logger.error(f"[Inject] Failed: {e}")
                return False

    def _setup_fifo(self):
        """Create FIFO for external injection."""
        if FIFO_PATH.exists():
            if not FIFO_PATH.is_fifo():
                FIFO_PATH.unlink()
                os.mkfifo(str(FIFO_PATH))
        else:
            os.mkfifo(str(FIFO_PATH))
        self.logger.info(f"FIFO ready: {FIFO_PATH}")

    def _fifo_reader(self):
        """Read from FIFO and inject into Claude."""
        while not self.stop_event.is_set():
            try:
                # Open blocks until a writer connects
                with open(str(FIFO_PATH), "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not self.stop_event.is_set():
                            self.logger.info(f"[FIFO] Received: {line[:200]}")
                            self.inject(line)
            except Exception as e:
                if not self.stop_event.is_set():
                    self.logger.error(f"[FIFO] Error: {e}")
                    time.sleep(1)

    def _setup_schedules(self):
        """Set up cron-based schedules."""
        self.scheduler = BackgroundScheduler()
        self.scheduler.add_listener(
            self._on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
        )
        self._load_schedule_jobs()
        self.scheduler.start()
        self.last_job_event_at = datetime.now()

    def _on_job_event(self, event):
        """Track scheduler liveness from any job firing (success/error/missed)."""
        self.last_job_event_at = datetime.now()
        if event.code == EVENT_JOB_ERROR:
            self.logger.error(
                f"[Schedule] Job '{event.job_id}' raised: {event.exception}"
            )
        elif event.code == EVENT_JOB_MISSED:
            self.logger.warning(f"[Schedule] Job '{event.job_id}' MISSED")

    # Cron-style day_of_week strings we accept without warning. POSIX crontab
    # uses 0=Sunday, but APScheduler uses 0=Monday — a silent off-by-one trap
    # that caused the 2026-04-13 tw-stock-preopen miss. We accept numeric
    # forms for compatibility but warn so the user knows to switch to literals.
    _CRON_FIELDS = ("minute", "hour", "day", "month", "day_of_week")
    _DAY_OF_WEEK_LITERALS = re.compile(
        r"^[\*\?/,\-mtwfsuonehrdia0-6]+$", re.IGNORECASE
    )
    _DAY_OF_WEEK_NUMERIC_RANGE = re.compile(r"^[0-6](?:[,\-][0-6])+$")

    def _validate_cron_parts(self, name: str, cron_expr: str):
        """Split and validate a 5-field cron expression.

        Returns (parts, ok). If ok is False, the job must be skipped by the
        caller. We log here so a bad job is visible in the log but does not
        abort the whole reload (protects the HIGH #1 silent-wipe case).
        """
        parts = cron_expr.split()
        if len(parts) != 5:
            self.logger.error(
                f"Scheduled '{name}': cron must have 5 fields "
                f"(minute hour day month day_of_week), got {len(parts)}: "
                f"{cron_expr!r} — skipping this job"
            )
            return parts, False

        dow = parts[4]
        # Warn on ambiguous numeric day_of_week — the POSIX/APScheduler
        # off-by-one trap. Literals (mon-fri) are unambiguous.
        if self._DAY_OF_WEEK_NUMERIC_RANGE.match(dow):
            self.logger.warning(
                f"Scheduled '{name}': day_of_week={dow!r} is numeric — "
                f"APScheduler uses 0=Monday (NOT 0=Sunday like POSIX cron). "
                f"If you meant Mon-Fri, use 'mon-fri' instead of '1-5'."
            )
        return parts, True

    def _load_schedule_jobs(self):
        """Load (or reload) jobs from self.config into self.scheduler.

        Caller is responsible for clearing existing jobs first if reloading.
        Individual job failures are logged and skipped — they never abort
        the whole reload (otherwise one bad cron would wipe every job; see
        _reload_schedules for the atomic-swap safety net that complements
        this per-job isolation).
        """
        schedules = self.config.get("schedule", {})

        for name, sched in schedules.items():
            if not sched.get("enabled", False):
                continue
            cron_expr = sched.get("cron", "")
            prompt = sched.get("prompt", "")

            parts, ok = self._validate_cron_parts(name, cron_expr)
            if not ok:
                continue

            try:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
            except Exception as e:
                self.logger.error(
                    f"Scheduled '{name}': CronTrigger rejected {cron_expr!r}: "
                    f"{e} — skipping this job"
                )
                continue

            # Generous misfire_grace_time so jobs don't get silently dropped
            # if the scheduler thread is briefly slow. APScheduler's default
            # is 1 second, which is too tight under any GIL contention and
            # was the root cause of the 2026-04-09 silent-scheduler bug.
            # 1200s = 20 minutes of slack — comfortably more than any GIL
            # stall we've ever observed, and still under the 30-min heartbeat
            # cadence so we never run a heartbeat that's a full cycle late.
            self.scheduler.add_job(
                self._run_scheduled,
                trigger,
                args=[name, prompt],
                id=name,
                name=name,
                misfire_grace_time=1200,
                coalesce=True,
            )
            self.logger.info(f"Scheduled '{name}': {cron_expr}")

    # Watchdog idle threshold: 90 minutes covers (a) one full 30-min cycle
    # being missed plus (b) the 20-min misfire grace plus (c) generous
    # buffer so a single slow tick doesn't trigger a false self-heal.
    SCHEDULER_WATCHDOG_IDLE_SECONDS = 90 * 60

    def _scheduler_watchdog(self):
        """Detect silently-dead apscheduler and self-heal via reload.

        If we've registered any sub-hourly job (e.g. heartbeat */30) but
        have seen ZERO job events in the last SCHEDULER_WATCHDOG_IDLE_SECONDS,
        the scheduler thread is wedged. Reload schedules to recover (this
        is what manual SIGHUP does). This is the *only* recovery path we
        rely on — no external sentinel needed.
        """
        if not self.scheduler or not self.last_job_event_at:
            return
        # Only watchdog if at least one job is sub-hourly. Otherwise long
        # gaps between jobs are normal.
        has_frequent_job = False
        for sched in self.config.get("schedule", {}).values():
            if not sched.get("enabled"):
                continue
            cron_min = sched.get("cron", "").split()[0]
            if "/" in cron_min or "," in cron_min or "*" == cron_min:
                has_frequent_job = True
                break
        if not has_frequent_job:
            return

        idle = (datetime.now() - self.last_job_event_at).total_seconds()
        if idle > self.SCHEDULER_WATCHDOG_IDLE_SECONDS:
            self.logger.error(
                f"[Watchdog] Scheduler idle for {idle/60:.1f}min — reloading"
            )
            self._reload_schedules()
            self.last_job_event_at = datetime.now()

    def _reload_schedules(self, *_):
        """Reload schedules from config.json without restarting ClawX.

        Triggered by SIGHUP. Uses a staging pass so that a bad new config
        cannot wipe the running schedule — the old implementation called
        remove_all_jobs() *before* validating the new config, so one typo
        would silently kill heartbeat + morning report until manual fix
        (HIGH #1 in the 2026-04-13 audit). The fix:

          1. Reload config, keep a backup of the old one
          2. Dry-run validate every new job (CronTrigger build) into a
             staging list; if any job explodes, log and bail — old schedule
             untouched
          3. Only after the staging pass fully succeeds do we swap: remove
             all old jobs, install the new ones. Both steps run under the
             apscheduler's own lock so there's no window where 0 jobs exist
             if the process is queried mid-swap

        Individual per-job failures still use _load_schedule_jobs's
        skip-and-log behavior, but THAT path is used only for "partial
        validity" (e.g. user intentionally has a malformed job in config
        that they want ignored); SIGHUP reload is stricter — any failure
        in staging aborts the whole swap, because at SIGHUP time we know
        the intent is "apply new config or nothing."
        """
        self.logger.info("[SIGHUP] Reloading schedules from config.json...")
        old_config = self.config
        try:
            new_config = load_config()
        except Exception as e:
            self.logger.error(f"[SIGHUP] Reload failed: config parse error: {e}")
            return
        if self.scheduler is None:
            # Nothing to reload into yet; just update the stored config so
            # the next _setup_schedules uses it.
            self.config = new_config
            self.logger.warning("[SIGHUP] Scheduler not initialized yet, skipping")
            return

        # Staging pass: build every trigger without touching live state.
        try:
            staged = self._stage_schedule_jobs(new_config)
        except Exception as e:
            self.logger.error(
                f"[SIGHUP] Reload aborted — staging failed: {e} "
                f"(old schedule preserved)"
            )
            return

        # Staging OK — commit. We temporarily set self.config to new so
        # _load_schedule_jobs reads the right schedules, then restore on
        # failure (defense-in-depth; staging pass should have caught
        # everything).
        try:
            self.scheduler.remove_all_jobs()
            self.config = new_config
            self._load_schedule_jobs()
        except Exception as e:
            self.logger.error(
                f"[SIGHUP] Reload commit failed: {e} — restoring old config"
            )
            self.config = old_config
            try:
                self.scheduler.remove_all_jobs()
                self._load_schedule_jobs()
            except Exception as e2:
                self.logger.error(f"[SIGHUP] Old-config restore ALSO failed: {e2}")
            return

        n = len(self.scheduler.get_jobs())
        self.logger.info(
            f"[SIGHUP] Reload OK ({n} active jobs; {len(staged)} staged)"
        )

    def _stage_schedule_jobs(self, config: dict) -> list:
        """Build every enabled job's CronTrigger without touching the live
        scheduler. Used by _reload_schedules to validate a new config
        before committing.

        STRICT mode: any validation failure (bad field count, unparsable
        day_of_week, CronTrigger rejection) raises. The caller
        (_reload_schedules) catches the exception and aborts the swap,
        preserving the live schedule. This is the HIGH #1 safety net —
        SIGHUP reload is all-or-nothing, which is the right semantics for
        "apply new config."

        Note: _load_schedule_jobs (used at initial startup) is lenient
        and skips individual broken jobs; the two paths intentionally
        differ. Startup wants to come up with as much as it can;
        SIGHUP wants to either fully apply the user's intent or fully
        decline it.

        Returns a list of (name, trigger, prompt) tuples for every
        enabled job.
        """
        schedules = config.get("schedule", {})
        if not isinstance(schedules, dict):
            raise TypeError(
                f"config.schedule must be a dict, got {type(schedules).__name__}"
            )
        staged = []
        for name, sched in schedules.items():
            if not isinstance(sched, dict):
                raise TypeError(
                    f"schedule['{name}'] must be a dict, got "
                    f"{type(sched).__name__}"
                )
            if not sched.get("enabled", False):
                continue
            cron_expr = sched.get("cron", "")
            prompt = sched.get("prompt", "")
            parts = cron_expr.split()
            if len(parts) != 5:
                raise ValueError(
                    f"'{name}': cron must have 5 fields, got {len(parts)}: "
                    f"{cron_expr!r}"
                )
            # Warn (but don't reject) numeric day_of_week ranges — legal
            # but easy to get wrong per APScheduler's 0=Monday semantics.
            if self._DAY_OF_WEEK_NUMERIC_RANGE.match(parts[4]):
                self.logger.warning(
                    f"[SIGHUP staging] '{name}': day_of_week={parts[4]!r} "
                    f"is numeric — APScheduler uses 0=Monday (NOT POSIX's "
                    f"0=Sunday). Use 'mon-fri' if you meant weekdays."
                )
            try:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
            except Exception as e:
                raise ValueError(
                    f"'{name}': CronTrigger rejected {cron_expr!r}: {e}"
                ) from e
            staged.append((name, trigger, prompt))
        return staged

    def _find_bun(self):
        """Return an invocable bun identifier or None if bun is missing.

        Preference order:
          1. ``bun`` resolved via PATH (return the string ``"bun"``)
          2. Absolute path at ``~/.bun/bin/bun`` (common WSL/curl-install)
          3. Absolute path at ``~/.local/bin/bun``
          4. None — bun not found anywhere we know to look

        Never raises.
        """
        try:
            if shutil.which("bun"):
                return "bun"
        except Exception:
            pass
        for candidate in [
            Path.home() / ".bun" / "bin" / "bun",
            Path.home() / ".local" / "bin" / "bun",
        ]:
            try:
                if candidate.exists() and os.access(candidate, os.X_OK):
                    return str(candidate)
            except OSError:
                continue
        return None

    def _check_telegram_prereqs(self, bun_finder=None, plugin_cache_dir=None):
        """Inspect the Telegram plugin setup and log WARNINGs if something
        will prevent the Telegram MCP server from launching.

        Pure detection: we never install or patch. The log file is the
        whole UX — user reads it when something's off and copy-pastes
        the printed fix command.

        Two failure modes we catch:

        1. ``--channels plugin:telegram`` is in extra_args but bun is
           not installed anywhere we can find it → the MCP server has
           no runtime at all. We print the official install URL.

        2. bun is installed at an absolute path (e.g. ``~/.bun/bin/bun``)
           but the plugin's ``.mcp.json`` still has bare ``"command":
           "bun"``. Claude Code will try to exec ``bun`` from its own
           PATH and fail. We print the exact file + absolute path the
           user needs to swap in.

        Both ``bun_finder`` and ``plugin_cache_dir`` are injectable so
        tests can exercise the logic without touching ~/.claude.
        """
        extra_args = self.config.get("claude", {}).get("extra_args") or []
        # Look for a "plugin:telegram..." token in --channels arguments
        telegram_enabled = any(
            isinstance(a, str) and "plugin:telegram" in a
            for a in extra_args
        )
        if not telegram_enabled:
            return

        finder = bun_finder if bun_finder is not None else self._find_bun
        bun = finder()

        if bun is None:
            self.logger.warning(
                "[TelegramPrereq] bun runtime not found, Telegram plugin will "
                "fail to launch. Install with:\n"
                "  curl -fsSL https://bun.sh/install | bash\n"
                "Then restart ClawX. If bun ends up at ~/.bun/bin/bun but not "
                "in PATH, ClawX will log a follow-up fix on next start."
            )
            return

        # bun exists — does the plugin .mcp.json point at it correctly?
        if plugin_cache_dir is None:
            plugin_cache_dir = Path.home() / ".claude" / "plugins" / "cache"
        plugin_cache_dir = Path(plugin_cache_dir)
        telegram_root = plugin_cache_dir / "claude-plugins-official" / "telegram"
        if not telegram_root.exists():
            # Plugin not installed yet — Claude Code will fetch it on first
            # run. No point warning about a file that doesn't exist yet.
            return

        # Find every .mcp.json under any version subdir
        mcp_files = list(telegram_root.glob("*/.mcp.json"))
        if not mcp_files:
            return

        # If bun is a bare word, any .mcp.json with "bun" works — done.
        bun_in_path = (bun == "bun")

        for mcp_path in mcp_files:
            try:
                data = json.loads(mcp_path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                self.logger.warning(
                    f"[TelegramPrereq] could not read {mcp_path}: {e}"
                )
                continue
            cmd = (
                data.get("mcpServers", {})
                    .get("telegram", {})
                    .get("command")
            )
            if not cmd:
                continue
            if cmd == "bun" and not bun_in_path:
                self.logger.warning(
                    f"[TelegramPrereq] {mcp_path} has bare \"command\": \"bun\" "
                    f"but bun is not in PATH. Claude Code will fail to launch "
                    f"the Telegram MCP. Patch it to the absolute path:\n"
                    f"  {bun}\n"
                    f"Edit: {mcp_path}\n"
                    f"Change: \"command\": \"bun\"  →  \"command\": \"{bun}\""
                )

    def _maybe_handle_compact(self, chunk):
        """Detect compact event in PTY output (runs continuously).

        When Claude auto-compacts, the terminal shows:
            ✻ Conversation compacted (ctrl+o for history)

        On detection: (1) inject AGENTS.md identity reload, (2) notify
        user via Telegram. A 60-second cooldown prevents rapid re-fires
        when the same message scrolls through the buffer multiple times.
        """
        now = time.time()
        if now < self._compact_cooldown_until:
            return
        self._compact_buffer.extend(chunk)
        if len(self._compact_buffer) > 8192:
            del self._compact_buffer[:-8192]
        if detect_compact_event(bytes(self._compact_buffer)) is None:
            return
        # Detected!
        self._compact_buffer = bytearray()
        self._compact_cooldown_until = now + 60  # 60s cooldown
        self.logger.info("[Compact] detected via PTY stream")

        # (1) Internal: restore identity after a short delay
        def _deferred_identity_reload():
            time.sleep(3)
            self.inject(
                "BLOCKING REQUIREMENT: Read AGENTS.md and follow its "
                "'Every Session' instructions completely. This is a "
                "post-compact identity reload — do it before anything else."
            )
            self.logger.info("[Compact] Injected post-compact AGENTS.md reload")

        Thread(target=_deferred_identity_reload, daemon=True).start()

        # (2) Public: notify user via Telegram
        self._notify_compact()

    def _send_telegram(self, text, tag="Notify"):
        """Send a Telegram message via Bot API. Best-effort, never crashes.

        Reads bot token from ~/.claude/channels/telegram/.env and chat_id
        from config.json -> compact_notify.telegram.chat_id.
        """
        cfg = (self.config.get("compact_notify") or {})
        tg_cfg = (cfg.get("telegram") or {})
        chat_id = tg_cfg.get("chat_id")
        if not chat_id:
            return

        token_file = Path(tg_cfg.get(
            "token_env_file",
            str(Path.home() / ".claude" / "channels" / "telegram" / ".env"),
        )).expanduser()
        token = None
        try:
            for line in token_file.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError as e:
            self.logger.warning(f"[{tag}] could not read TG token: {e}")
            return
        if not token:
            return

        try:
            import urllib.parse
            import urllib.request
            payload = urllib.parse.urlencode({
                "chat_id": str(chat_id),
                "text": text,
            }).encode("utf-8")
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            self.logger.warning(f"[{tag}] TG notify failed: {e}")

    def _notify_compact(self):
        """Send Telegram notification about auto-compact."""
        self._send_telegram(
            "🧠 Context 自動壓縮了，正在重新載入身份…",
            tag="Compact",
        )

    def _run_scheduled(self, name, prompt):
        """Execute a scheduled prompt by injecting into the PTY."""
        self.logger.info(f"[Schedule] Running '{name}'")
        if self.child_pid and self._is_alive():
            self.inject(prompt)
        else:
            self.logger.warning(f"[Schedule] Session not alive for '{name}'")

    def _is_alive(self):
        """Check if the child process is still running."""
        if self.child_pid is None:
            return False
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
            return pid == 0  # 0 means still running
        except ChildProcessError:
            return False

    # Sustained-uptime threshold for resetting restart_count. After the
    # child has been alive for this long since the most recent respawn,
    # we consider the restart "fully successful" and zero the counter so
    # a later burst of crashes can still be recovered (HIGH #2 fix —
    # previously one burst of 3 early crashes would permanently disable
    # auto-restart for the whole ClawX session lifetime).
    RESTART_COUNT_RESET_SECONDS = 30 * 60

    def _maybe_reset_restart_count(self):
        """Zero restart_count if the current child has been alive long
        enough to qualify as stable. Called from _health_loop on every
        "Health OK" tick. Extracted so it can be unit-tested without
        touching threads / signals / Event.

        No-op if no restarts have happened, or if we haven't been tracking
        a last_restart_at (e.g. first spawn), or if not enough time has
        passed since the most recent respawn.
        """
        if self.restart_count <= 0 or self.last_restart_at is None:
            return
        since_restart = (datetime.now() - self.last_restart_at).total_seconds()
        if since_restart < self.RESTART_COUNT_RESET_SECONDS:
            return
        self.logger.info(
            f"Restart counter reset: child stable for "
            f"{since_restart/60:.1f}min (was {self.restart_count})"
        )
        self.restart_count = 0

    def _health_loop(self):
        """Background health check + auto-restart."""
        session_cfg = self.config["session"]
        interval = session_cfg.get("health_check_interval", 300)
        max_restarts = session_cfg["max_restart_attempts"]

        while not self.stop_event.is_set():
            time.sleep(interval)
            if self.stop_event.is_set():
                break

            if self._is_alive():
                uptime = str(datetime.now() - self.started_at).split(".")[0] if self.started_at else "?"
                self._maybe_reset_restart_count()
                self.logger.info(f"Health OK | uptime={uptime} | restarts={self.restart_count}")
                self._scheduler_watchdog()
            elif session_cfg.get("auto_restart", True) and not self.stop_event.is_set():
                if self.restart_count < max_restarts:
                    self.logger.warning("Session died, auto-restarting...")
                    delay = session_cfg.get("restart_delay_seconds", 5)
                    time.sleep(delay)
                    self._spawn_claude()
                    self.restart_count += 1
                else:
                    self.logger.error(f"Max restarts ({max_restarts}) reached.")

    def _get_project_dir(self):
        """Resolve project_dir to absolute path."""
        raw = self.config["claude"]["project_dir"]
        # Resolve relative to config file location
        p = Path(raw)
        if not p.is_absolute():
            p = BASE_DIR / p
        return str(p.resolve())

    def _spawn_claude(self):
        """Fork + exec Claude in a PTY using pty.fork()."""
        cmd = self.build_command()
        project_dir = self._get_project_dir()
        self.logger.info(f"Starting: {' '.join(cmd)}")
        self.logger.info(f"Working dir: {project_dir}")

        # pty.fork() handles all slave PTY setup automatically
        child_pid, master_fd = pty.fork()

        if child_pid == 0:
            # === Child process ===
            os.environ["TERM"] = os.environ.get("TERM", "xterm-256color")
            os.chdir(project_dir)
            os.execvp(cmd[0], cmd)
            # If execvp fails, exit child
            os._exit(1)
        else:
            # === Parent process ===
            self.master_fd = master_fd
            self.child_pid = child_pid
            self.started_at = datetime.now()
            self.last_restart_at = self.started_at

            # Reset startup-modal detection state. We re-arm on every spawn
            # because --continue can land us in a different conversation each
            # time, with or without a compact prompt.
            self._startup_buffer = bytearray()
            self._startup_modal_active = True
            self._startup_modal_handled = False
            # Reset rate-limit detection on respawn.
            self._ratelimit_buffer = bytearray()
            self._ratelimit_cooldown_until = 0
            # Reset compact detection on respawn.
            self._compact_buffer = bytearray()
            self._compact_cooldown_until = 0

            # Set terminal size
            set_winsize(master_fd)

            # Save child PID
            CHILD_PID_FILE.write_text(str(child_pid))
            self.logger.info(f"Session started (PID: {child_pid})")

    def _maybe_handle_startup_modal(self, chunk):
        """Feed PTY output into the startup-modal detector.

        Called from the main loop on every chunk read from master_fd while
        we're still inside the spawn-time detection window. If a modal is
        detected we auto-pick the highest-numbered option (Claude's
        convention for "skip / leave alone") so the session never wedges
        waiting for a human at the keyboard.
        """
        if not self._startup_modal_active or self._startup_modal_handled:
            return
        # Window expires after STARTUP_MODAL_WINDOW_SECONDS regardless of
        # output volume — covers the "no modal at all" happy path.
        if self.started_at and (
            datetime.now() - self.started_at
        ).total_seconds() > STARTUP_MODAL_WINDOW_SECONDS:
            self._startup_modal_active = False
            self._startup_buffer = bytearray()
            return
        self._startup_buffer.extend(chunk)
        if len(self._startup_buffer) > STARTUP_MODAL_BUFFER_LIMIT:
            # Drop oldest bytes — keep the tail where the prompt would be.
            del self._startup_buffer[: -STARTUP_MODAL_BUFFER_LIMIT]
        choice = detect_startup_modal(bytes(self._startup_buffer))
        if choice is None:
            return
        # Pick the highest-numbered option (Claude's "do nothing" slot).
        try:
            with self.write_lock:
                os.write(self.master_fd, f"{choice}\r".encode())
        except OSError as e:
            self.logger.error(f"[ModalAutoSkip] write failed: {e}")
            return
        self._startup_modal_handled = True
        self._startup_modal_active = False
        self._startup_buffer = bytearray()
        self.logger.warning(
            f"[ModalAutoSkip] Detected startup modal — auto-selected option {choice}"
        )

        # After skipping the compact/resume modal, inject AGENTS.md reload
        # so Claude restores its identity in the new session.
        # Short delay lets Claude finish processing the modal selection.
        def _deferred_identity_reload():
            time.sleep(3)
            self.inject(
                "BLOCKING REQUIREMENT: Read AGENTS.md and follow its "
                "'Every Session' instructions completely. This is a "
                "post-compact identity reload — do it before anything else."
            )
            self.logger.info("[ModalAutoSkip] Injected post-compact AGENTS.md reload")

        Thread(target=_deferred_identity_reload, daemon=True).start()

    def _maybe_handle_rate_limit(self, chunk):
        """Detect rate-limit modal in PTY output (runs continuously).

        Unlike startup modal detection which only runs during the first
        60 seconds, rate limits can appear at any time. We keep a small
        rolling buffer of recent PTY output and check for the rate-limit
        prompt pattern. A 5-minute cooldown prevents rapid re-fires.
        """
        now = time.time()
        if now < self._ratelimit_cooldown_until:
            return
        self._ratelimit_buffer.extend(chunk)
        # Keep only the last 8KB — the prompt is small.
        if len(self._ratelimit_buffer) > 8192:
            del self._ratelimit_buffer[:-8192]
        choice = detect_rate_limit_modal(bytes(self._ratelimit_buffer))
        if choice is None:
            return
        # Auto-select option 1: "Stop and wait for limit to reset"
        try:
            with self.write_lock:
                os.write(self.master_fd, f"{choice}\r".encode())
        except OSError as e:
            self.logger.error(f"[RateLimit] write failed: {e}")
            return
        self._ratelimit_cooldown_until = now + 300  # 5 min cooldown
        raw_text = self._ratelimit_buffer.decode("utf-8", errors="replace").strip()
        self._ratelimit_buffer = bytearray()
        self.logger.warning(
            "[RateLimit] Detected rate-limit modal — auto-selected 'Stop and wait'"
        )
        # Notify user via Telegram — forward Claude's raw message
        self._notify_rate_limit(raw_text)

    def _notify_rate_limit(self, raw_text=""):
        """Send Telegram notification when rate limit is hit."""
        import re
        # Strip ANSI escape codes for clean TG message
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw_text).strip() if raw_text else ""
        msg = f"⚠️ Rate limit hit — auto-selected 'wait for reset'\n\n{clean}" if clean else "⚠️ Rate limit hit — auto-selected 'wait for reset'"
        self._send_telegram(msg, tag="RateLimit")

    def _maybe_handle_feedback_modal(self, chunk):
        """Detect and auto-dismiss Claude's session feedback modal."""
        now = time.time()
        if now < self._feedback_cooldown_until:
            return
        choice = detect_feedback_modal(chunk)
        if choice is None:
            return
        try:
            with self.write_lock:
                os.write(self.master_fd, f"{choice}\r".encode())
        except OSError as e:
            self.logger.error(f"[Feedback] write failed: {e}")
            return
        self._feedback_cooldown_until = now + 300  # 5 min cooldown
        self.logger.info("[Feedback] Auto-dismissed session feedback modal")

    def _maybe_handle_resume_modal(self, chunk):
        """Detect and auto-select 'Don't ask me again' on the resume-mode modal.

        The modal text arrives split across multiple PTY chunks (ANSI codes
        and line-by-line rendering), so we accumulate a rolling buffer and
        run detect_resume_modal on the full window — not on a single chunk.
        """
        now = time.time()
        if now < self._resume_cooldown_until:
            return
        self._resume_buffer.extend(chunk)
        if len(self._resume_buffer) > 8192:
            del self._resume_buffer[:-8192]
        choice = detect_resume_modal(bytes(self._resume_buffer))
        if choice is None:
            return
        try:
            with self.write_lock:
                os.write(self.master_fd, f"{choice}\r".encode())
        except OSError as e:
            self.logger.error(f"[Resume] write failed: {e}")
            return
        self._resume_buffer = bytearray()
        self._resume_cooldown_until = now + 300  # 5 min cooldown
        self.logger.info("[Resume] Auto-selected 'Don't ask me again' on resume-mode modal")

    def run(self):
        """Main loop: PTY passthrough with FIFO injection."""
        # Setup
        self._setup_fifo()
        self._setup_schedules()
        self._check_telegram_prereqs()

        # Save original terminal settings
        old_attrs = None
        if sys.stdin.isatty():
            old_attrs = termios.tcgetattr(sys.stdin.fileno())

        # Show banner
        cmd = self.build_command()
        print("\033[1;36m" + "=" * 55)
        print("  🦞 ClawX — Claude Code PTY Wrapper")
        print("=" * 55 + "\033[0m")
        print(f"\033[90m  Command:  {' '.join(cmd)}")
        print(f"  Project:  {self._get_project_dir()}")
        print(f"  FIFO:     {FIFO_PATH}")
        print(f"  Log:      {LOG_DIR}/")
        print()
        print("  \033[1mInject from another terminal:\033[0m")
        print(f"\033[90m    echo \"your message\" > {FIFO_PATH}")
        print(f"    python3 clawx.py inject \"your message\"")
        print()
        print("  \033[1mScheduled jobs:\033[0m")
        schedules = self.config.get("schedule", {})
        if schedules:
            for name, sched in schedules.items():
                if sched.get("enabled"):
                    print(f"\033[90m    ⏰ {name}: {sched['cron']} — {sched['prompt'][:50]}...")
        else:
            print("\033[90m    (none)")
        print("\033[0m" + "\033[1;36m" + "=" * 55 + "\033[0m")
        print()

        # Spawn Claude
        self._spawn_claude()

        # Handle signals
        def handle_stop(signum, frame):
            self.stop_event.set()

        def handle_winch(signum, frame):
            if self.master_fd is not None:
                set_winsize(self.master_fd)
                # Forward SIGWINCH to child
                if self.child_pid:
                    try:
                        os.kill(self.child_pid, signal.SIGWINCH)
                    except ProcessLookupError:
                        pass

        def handle_restart(signum, frame):
            self.restart_requested = True
            self.stop_event.set()

        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGWINCH, handle_winch)
        signal.signal(signal.SIGHUP, self._reload_schedules)
        signal.signal(signal.SIGUSR1, handle_restart)

        # Start background threads
        fifo_thread = Thread(target=self._fifo_reader, daemon=True)
        fifo_thread.start()

        health_thread = Thread(target=self._health_loop, daemon=True)
        health_thread.start()

        # Compact + rate-limit detection now runs inline in the main
        # PTY read loop via _maybe_handle_compact / _maybe_handle_rate_limit.
        # No background thread needed.

        # Set terminal to raw mode for passthrough
        if sys.stdin.isatty():
            import tty
            tty.setraw(sys.stdin.fileno())

        # Transcript log
        transcript = LOG_DIR / f"transcript-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        transcript_f = open(transcript, "wb")

        try:
            stdin_fd = sys.stdin.fileno()
            stdout_fd = sys.stdout.fileno()

            while not self.stop_event.is_set() and self._is_alive():
                try:
                    rlist, _, _ = select.select([stdin_fd, self.master_fd], [], [], 1.0)
                except (ValueError, OSError):
                    break

                for fd in rlist:
                    if fd == stdin_fd:
                        # User typing → forward to Claude
                        try:
                            data = os.read(stdin_fd, 4096)
                            if not data:
                                self.stop_event.set()
                                break
                            with self.write_lock:
                                os.write(self.master_fd, data)
                        except OSError:
                            self.stop_event.set()
                            break

                    elif fd == self.master_fd:
                        # Claude output → forward to terminal + transcript
                        try:
                            data = os.read(self.master_fd, 4096)
                            if not data:
                                self.stop_event.set()
                                break
                            os.write(stdout_fd, data)
                            transcript_f.write(data)
                            transcript_f.flush()
                            # PTY stream watchers — all detection runs here.
                            self._maybe_handle_startup_modal(data)
                            self._maybe_handle_compact(data)
                            self._maybe_handle_rate_limit(data)
                            self._maybe_handle_feedback_modal(data)
                            self._maybe_handle_resume_modal(data)
                        except OSError:
                            self.stop_event.set()
                            break

        finally:
            # Restore terminal
            if old_attrs and sys.stdin.isatty():
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, old_attrs)

            transcript_f.close()

            # Cleanup: give child 5s to exit gracefully, then SIGKILL.
            if self.child_pid and self._is_alive():
                os.kill(self.child_pid, signal.SIGTERM)
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        pid, _ = os.waitpid(self.child_pid, os.WNOHANG)
                        if pid != 0:
                            break
                    except ChildProcessError:
                        break
                    time.sleep(0.1)
                else:
                    # Still alive after timeout — force kill.
                    try:
                        os.kill(self.child_pid, signal.SIGKILL)
                        os.waitpid(self.child_pid, 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass

            if self.master_fd is not None:
                os.close(self.master_fd)

            if self.scheduler:
                self.scheduler.shutdown(wait=False)

            if CHILD_PID_FILE.exists():
                CHILD_PID_FILE.unlink()

            # Don't remove FIFO — other processes may still reference it
            self.logger.info("ClawX stopped.")
            print("\n[ClawX] Session ended.")

        return RESTART_EXIT_CODE if self.restart_requested else 0


def _resolve_command(raw_cmd):
    """Resolve a command name to a full path, searching PATH and common locations."""
    resolved = shutil.which(raw_cmd)
    if resolved:
        return resolved
    for candidate in [
        Path.home() / ".local" / "bin" / raw_cmd,
        Path("/usr/local/bin") / raw_cmd,
    ]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_oneshot(prompt):
    """Run a one-shot prompt via claude -p."""
    config = load_config()
    cfg = config["claude"]
    project_dir = str((BASE_DIR / cfg["project_dir"]).resolve()) if not Path(cfg["project_dir"]).is_absolute() else cfg["project_dir"]
    resolved = _resolve_command(cfg["command"])
    if resolved is None:
        raise FileNotFoundError(f"Command '{cfg['command']}' not found in PATH or common locations")
    cmd = [resolved, "-p", prompt, "--add-dir", project_dir]
    if cfg.get("model"):
        cmd.extend(["--model", cfg["model"]])

    import subprocess
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=300,
        cwd=project_dir,
    )
    return result.stdout


def self_restart():
    """Send SIGUSR1 to the running ClawX to trigger a full restart.

    The signal handler cleanly shuts down the Claude child process.
    main() sees RESTART_EXIT_CODE and re-execs clawx.py — picking up
    any code changes, same terminal, same session (via --continue).
    """
    if not PID_FILE.exists():
        print("Error: No PID file — is ClawX running?")
        return False

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGUSR1)
        print(f"[ClawX] Restart signal (SIGUSR1) sent to PID {pid}")
        print(f"[ClawX] ClawX will shut down Claude, then re-exec itself.")
    except ProcessLookupError:
        print(f"PID {pid} not found, cleaning up")
        PID_FILE.unlink()
        return False

    return True


def replay_transcript(path):
    """Parse a transcript log, strip ANSI, detect and annotate events.

    Outputs clean text with [EVENT] markers for compact, rate-limit, and
    startup modal detections. Useful for analysing past sessions and
    improving detection patterns.
    """
    p = Path(path)
    if not p.exists():
        print(f"Error: file not found: {path}")
        sys.exit(1)

    raw = p.read_bytes()
    # Strip ANSI escape sequences
    clean = _ANSI_RE.sub(b"", raw)
    text = clean.decode("utf-8", errors="replace")

    # Scan for events in sliding windows over the raw bytes
    events = []
    window = 8192
    for i in range(0, len(raw), window // 2):
        chunk = raw[i:i + window]
        if detect_compact_event(chunk):
            events.append((i, "COMPACT"))
        if detect_rate_limit_modal(chunk):
            events.append((i, "RATE_LIMIT"))
        if detect_startup_modal(chunk):
            events.append((i, "STARTUP_MODAL"))

    # Deduplicate nearby events of same type
    deduped = []
    for offset, etype in events:
        if deduped and deduped[-1][1] == etype and offset - deduped[-1][0] < window:
            continue
        deduped.append((offset, etype))

    # Summary
    print(f"=== Transcript Replay: {p.name} ===")
    print(f"Size: {len(raw):,} bytes ({len(text):,} chars clean)")
    print(f"Events detected: {len(deduped)}")
    for offset, etype in deduped:
        # Find approximate line context
        nearby = _ANSI_RE.sub(b"", raw[max(0, offset - 200):offset + 500])
        snippet = nearby.decode("utf-8", errors="replace").strip()[:200]
        print(f"\n  [{etype}] at byte ~{offset}")
        print(f"    ...{snippet}...")
    print(f"\n=== End ({p.name}) ===")


def main():
    # Resume overrides (can appear anywhere in argv).
    # -c / --continue and -nc / --no-continue are mutually exclusive.
    want_no_continue = "--no-continue" in sys.argv or "-nc" in sys.argv
    want_continue = "--continue" in sys.argv or "-c" in sys.argv
    if want_no_continue and want_continue:
        print("Error: --continue and --no-continue are mutually exclusive", file=sys.stderr)
        sys.exit(2)
    if want_no_continue:
        ClawX.force_no_continue = True
        sys.argv = [a for a in sys.argv if a not in ("--no-continue", "-nc")]
    if want_continue:
        ClawX.force_continue = True
        sys.argv = [a for a in sys.argv if a not in ("--continue", "-c")]

    if len(sys.argv) < 2:
        # Default: run PTY passthrough with restart loop
        PID_FILE.write_text(str(os.getpid()))
        try:
            clawx = ClawX()
            exit_code = clawx.run()
            if exit_code == RESTART_EXIT_CODE:
                delay = clawx.config.get("session", {}).get("restart_delay_seconds", 5)
                print(f"\n[ClawX] Restarting in {delay}s...")
                time.sleep(delay)
                # Re-exec ourselves — picks up code changes, same terminal
                os.execvp(sys.executable, [sys.executable, __file__])
        finally:
            if PID_FILE.exists():
                PID_FILE.unlink()
        return

    command = sys.argv[1]

    if command == "inject" and len(sys.argv) > 2:
        # Inject via FIFO
        msg = " ".join(sys.argv[2:])
        if not FIFO_PATH.exists():
            print(f"Error: FIFO not found at {FIFO_PATH}. Is ClawX running?")
            sys.exit(1)
        with open(str(FIFO_PATH), "w") as f:
            f.write(msg + "\n")
        print(f"Injected: {msg[:100]}")

    elif command == "prompt" and len(sys.argv) > 2:
        prompt = " ".join(sys.argv[2:])
        result = run_oneshot(prompt)
        if result:
            print(result)

    elif command == "stop":
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"Sent SIGTERM to ClawX PID {pid}")
            except ProcessLookupError:
                print(f"PID {pid} not found, cleaning up")
                PID_FILE.unlink()
        else:
            print("No PID file found")

    elif command == "replay" and len(sys.argv) > 2:
        replay_transcript(sys.argv[2])

    elif command == "restart":
        self_restart()

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
