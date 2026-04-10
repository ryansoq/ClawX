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
PID_FILE = BASE_DIR / "mono.pid"
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
      2. At least 2 distinct numbered options ("1." or "1)" form)

    Returns the highest option number detected (the conventional
    "skip / leave alone" slot in 3-option Claude prompts), or None
    if no modal is present.
    """
    if not buf:
        return None
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", errors="replace").lower()
    if not any(kw in text for kw in ("compact", "summarize", "auto-compact")):
        return None
    numbers = set()
    for match in re.finditer(r"(?:^|\s|\[)([1-9])[.)\]]", text, re.MULTILINE):
        numbers.add(int(match.group(1)))
    if len(numbers) < 2:
        return None
    return max(numbers)


def detect_compact_event(buf: bytes):
    """Detect a compact notification in PTY output.

    When Claude Code auto-compacts context it prints:
        ✻ Conversation compacted (ctrl+o for history)

    ANSI cursor-move sequences (e.g. \\x1b[1C) replace spaces in PTY
    output, so after stripping we match the words individually rather
    than as a contiguous phrase.

    Returns True if detected, None otherwise.
    """
    if not buf:
        return None
    # Replace ANSI sequences with a space so cursor-moves don't merge words.
    text = _ANSI_RE.sub(b" ", buf).decode("utf-8", errors="replace").lower()
    if "conversation" in text and "compacted" in text:
        return True
    return None


def detect_rate_limit_modal(buf: bytes):
    """Detect a rate-limit modal in PTY output.

    When Claude Code hits the usage cap it shows a blocking prompt:
        "You've hit your limit · resets 12am (Asia/Taipei)"
        /rate-limit-options
        1. Stop and wait for limit to reset
        2. Upgrade your plan

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
    numbers = set()
    for match in re.finditer(r"(?:^|\s|\[)([1-9])[.)\]]", text, re.MULTILINE):
        numbers.add(int(match.group(1)))
    if len(numbers) < 2:
        return None
    # Pick option 1 = "Stop and wait for limit to reset"
    return 1


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"clawx-{datetime.now().strftime('%Y%m%d')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
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

    def __init__(self):
        self.config = load_config()
        self.logger = setup_logging()
        self.master_fd = None
        self.child_pid = None
        self.stop_event = Event()
        self.write_lock = Lock()
        self.started_at = None
        self.scheduler = None
        self.restart_count = 0
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
        self._ratelimit_handled = False
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

        # Resume last session
        if cfg.get("resume_last"):
            cmd.append("--continue")

        # MCP config
        if cfg.get("mcp_config"):
            cmd.extend(["--mcp-config", cfg["mcp_config"]])

        # Extra args
        for arg in cfg.get("extra_args", []):
            cmd.append(arg)

        return cmd

    def inject(self, text):
        """Inject text into Claude's stdin via the PTY master."""
        if self.master_fd is None:
            self.logger.error("No active session")
            return False
        with self.write_lock:
            try:
                # Write text + carriage return (Enter in raw terminal mode)
                data = (text + "\r").encode("utf-8")
                os.write(self.master_fd, data)
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

    def _load_schedule_jobs(self):
        """Load (or reload) jobs from self.config into self.scheduler.

        Caller is responsible for clearing existing jobs first if reloading.
        """
        schedules = self.config.get("schedule", {})

        for name, sched in schedules.items():
            if not sched.get("enabled", False):
                continue
            cron_expr = sched["cron"]
            prompt = sched["prompt"]

            parts = cron_expr.split()
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )

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

        Triggered by SIGHUP. Re-reads config, removes all current jobs,
        and re-adds them from the new config. Safe to call repeatedly.
        """
        self.logger.info("[SIGHUP] Reloading schedules from config.json...")
        try:
            self.config = load_config()
            if self.scheduler is None:
                self.logger.warning("[SIGHUP] Scheduler not initialized yet, skipping")
                return
            self.scheduler.remove_all_jobs()
            self._load_schedule_jobs()
            n = len(self.scheduler.get_jobs())
            self.logger.info(f"[SIGHUP] Reload OK ({n} active jobs)")
        except Exception as e:
            self.logger.error(f"[SIGHUP] Reload failed: {e}")

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
        import time
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
            import time
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

    def _notify_compact(self):
        """Send a user-friendly Telegram message about the auto-compact.

        Reads bot token from ~/.claude/channels/telegram/.env and chat_id
        from config.json -> compact_notify.telegram.chat_id. Fails soft —
        notification is best-effort and must never crash.
        """
        cfg = (self.config.get("compact_notify") or {})
        tg_cfg = (cfg.get("telegram") or {})
        chat_id = tg_cfg.get("chat_id")
        if not chat_id:
            return  # notification not configured — silent no-op

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
            self.logger.warning(f"[Compact] could not read TG token: {e}")
            return
        if not token:
            return

        body = "🧠 Context 自動壓縮了，正在重新載入身份…"

        try:
            import urllib.parse
            import urllib.request
            payload = urllib.parse.urlencode({
                "chat_id": str(chat_id),
                "text": body,
            }).encode("utf-8")
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            self.logger.warning(f"[Compact] TG notify failed: {e}")

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

            # Reset startup-modal detection state. We re-arm on every spawn
            # because --continue can land us in a different conversation each
            # time, with or without a compact prompt.
            self._startup_buffer = bytearray()
            self._startup_modal_active = True
            self._startup_modal_handled = False
            # Reset rate-limit detection on respawn.
            self._ratelimit_buffer = bytearray()
            self._ratelimit_handled = False
            # Reset compact detection on respawn.
            self._compact_buffer = bytearray()
            self._compact_cooldown_until = 0

            # Set terminal size
            set_winsize(master_fd)

            # Save PID
            PID_FILE.write_text(str(child_pid))
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
            import time
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
        prompt pattern.
        """
        if self._ratelimit_handled:
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
        self._ratelimit_handled = True
        self._ratelimit_buffer = bytearray()
        self.logger.warning(
            "[RateLimit] Detected rate-limit modal — auto-selected 'Stop and wait'"
        )
        # Notify user via Telegram
        self._notify_rate_limit()

    def _notify_rate_limit(self):
        """Send Telegram notification when rate limit is hit."""
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
            self.logger.warning(f"[RateLimit] could not read TG token: {e}")
            return
        if not token:
            return

        body = (
            "⚠️ Token 用完了！Claude Code 撞到 rate limit。\n"
            "已自動選擇「等待 reset」。\n"
            f"預計 12:00 AM (Asia/Taipei) 重置。"
        )

        try:
            import urllib.parse
            import urllib.request
            payload = urllib.parse.urlencode({
                "chat_id": str(chat_id),
                "text": body,
            }).encode("utf-8")
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            self.logger.warning(f"[RateLimit] TG notify failed: {e}")

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
        # SIGUSR1 restart temporarily disabled — needs more testing.
        # signal.signal(signal.SIGUSR1, handle_restart)

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
                        except OSError:
                            self.stop_event.set()
                            break

        finally:
            # Restore terminal
            if old_attrs and sys.stdin.isatty():
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, old_attrs)

            transcript_f.close()

            # Cleanup
            if self.child_pid and self._is_alive():
                os.kill(self.child_pid, signal.SIGTERM)
                try:
                    os.waitpid(self.child_pid, 0)
                except ChildProcessError:
                    pass

            if self.master_fd is not None:
                os.close(self.master_fd)

            if self.scheduler:
                self.scheduler.shutdown(wait=False)

            if PID_FILE.exists():
                PID_FILE.unlink()

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
    """Signal the running ClawX instance to restart in-place.

    Sends SIGUSR1 to the running process (from PID file). The Runner's
    signal handler sets ``restart_requested = True`` and triggers a clean
    shutdown. The outer restart loop in ``main()`` sees the restart exit
    code and relaunches — same terminal, same session.
    """
    if not PID_FILE.exists():
        print("Error: No PID file — is ClawX running?")
        return False

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGUSR1)
        print(f"[ClawX] Restart signal sent to PID {pid}")
    except ProcessLookupError:
        print(f"PID {pid} not found, cleaning up")
        PID_FILE.unlink()
        return False

    return True


def main():
    if len(sys.argv) < 2:
        # Default: run PTY passthrough with restart loop
        while True:
            clawx = ClawX()
            exit_code = clawx.run()
            if exit_code != RESTART_EXIT_CODE:
                break
            delay = clawx.config.get("session", {}).get("restart_delay_seconds", 5)
            print(f"\n[ClawX] Restarting in {delay}s...")
            time.sleep(delay)
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
                print(f"Sent SIGTERM to PID {pid}")
            except ProcessLookupError:
                print(f"PID {pid} not found, cleaning up")
                PID_FILE.unlink()
        else:
            print("No PID file found")

    elif command == "restart":
        self_restart()

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
