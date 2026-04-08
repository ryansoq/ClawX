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
    python clawx.py prompt "text"    # One-shot: run prompt via -p mode, print result
"""

import json
import sys
import os
import pty
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


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"clawx-{datetime.now().strftime('%Y%m%d')}.log"
    logger = logging.getLogger("ClawX")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
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

    def build_command(self):
        """Build the claude CLI command."""
        cfg = self.config["claude"]
        # Resolve command to full path so child process can find it
        raw_cmd = cfg["command"]
        resolved = shutil.which(raw_cmd)
        if resolved is None:
            # Check common install locations
            for candidate in [
                Path.home() / ".local" / "bin" / raw_cmd,
                Path("/usr/local/bin") / raw_cmd,
            ]:
                if candidate.exists() and os.access(candidate, os.X_OK):
                    resolved = str(candidate)
                    break
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

            self.scheduler.add_job(
                self._run_scheduled,
                trigger,
                args=[name, prompt],
                id=name,
                name=name,
            )
            self.logger.info(f"Scheduled '{name}': {cron_expr}")

        self.scheduler.start()

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

            # Set terminal size
            set_winsize(master_fd)

            # Save PID
            PID_FILE.write_text(str(child_pid))
            self.logger.info(f"Session started (PID: {child_pid})")

    def run(self):
        """Main loop: PTY passthrough with FIFO injection."""
        # Setup
        self._setup_fifo()
        self._setup_schedules()

        # Save original terminal settings
        old_attrs = None
        if sys.stdin.isatty():
            old_attrs = termios.tcgetattr(sys.stdin.fileno())

        # Show banner
        cmd = self.build_command()
        print("\033[1;36m" + "=" * 55)
        print("  🦀 ClawX — Claude Code PTY Wrapper")
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

        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGWINCH, handle_winch)

        # Start background threads
        fifo_thread = Thread(target=self._fifo_reader, daemon=True)
        fifo_thread.start()

        health_thread = Thread(target=self._health_loop, daemon=True)
        health_thread.start()

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


def run_oneshot(prompt):
    """Run a one-shot prompt via claude -p."""
    config = load_config()
    cfg = config["claude"]
    project_dir = str((BASE_DIR / cfg["project_dir"]).resolve()) if not Path(cfg["project_dir"]).is_absolute() else cfg["project_dir"]
    cmd = [cfg["command"], "-p", prompt, "--add-dir", project_dir]
    if cfg.get("model"):
        cmd.extend(["--model", cfg["model"]])

    import subprocess
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=300,
        cwd=project_dir,
    )
    return result.stdout


def main():
    if len(sys.argv) < 2:
        # Default: run PTY passthrough
        clawx = ClawX()
        clawx.run()
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

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
