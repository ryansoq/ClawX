#!/usr/bin/env python3
"""
ClaudexClaw: Claude Code supervisor / harness
Controls, monitors, and manages Claude Code sessions.

Usage:
    python clawx.py                  # Start daemon (interactive session)
    python clawx.py send "message"   # Send a command to running session
    python clawx.py status           # Check session status
    python clawx.py stop             # Gracefully stop session
    python clawx.py prompt "text"    # One-shot: run prompt via -p mode, print result
"""

import subprocess
import json
import sys
import os
import time
import signal
import logging
from pathlib import Path
from datetime import datetime
from threading import Thread, Event

# Optional: pip install apscheduler
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
PID_FILE = BASE_DIR / "mono.pid"
SOCKET_FILE = BASE_DIR / "mono.sock"
LOG_DIR = BASE_DIR / "logs"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def setup_logging(config):
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"mono-{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("ClaudexClaw")


class ClaudeSession:
    """Manages a single Claude CLI interactive session."""

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.proc = None
        self.started_at = None
        self.restart_count = 0
        self.stop_event = Event()

    def build_command(self):
        """Build the claude CLI command."""
        cfg = self.config["claude"]
        cmd = [cfg["command"]]

        # Use print mode with verbose + streaming for programmatic control
        cmd.extend(["--print"])
        cmd.extend(["--verbose"])
        cmd.extend(["--output-format", "stream-json"])
        cmd.extend(["--input-format", "stream-json"])

        # Project directory
        cmd.extend(["--add-dir", cfg["project_dir"]])

        # Model
        if cfg.get("model"):
            cmd.extend(["--model", cfg["model"]])

        # Permission mode
        if cfg.get("dangerously_skip_permissions"):
            cmd.append("--dangerously-skip-permissions")
        elif cfg.get("permission_mode"):
            cmd.extend(["--permission-mode", cfg["permission_mode"]])

        # MCP config
        if cfg.get("mcp_config"):
            cmd.extend(["--mcp-config", cfg["mcp_config"]])

        # Extra args
        for arg in cfg.get("extra_args", []):
            cmd.append(arg)

        return cmd

    def start(self):
        """Start the Claude CLI session."""
        cmd = self.build_command()
        self.logger.info(f"Starting Claude session: {' '.join(cmd)}")

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.config["claude"]["project_dir"],
        )
        self.started_at = datetime.now()
        self.logger.info(f"Session started (PID: {self.proc.pid})")

        # Save PID
        PID_FILE.write_text(str(self.proc.pid))

        # Start output reader thread
        self._reader_thread = Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

        self._stderr_thread = Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_output(self):
        """Read and log stdout from Claude session."""
        try:
            for line in iter(self.proc.stdout.readline, b""):
                if self.stop_event.is_set():
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue
                try:
                    msg = json.loads(decoded)
                    msg_type = msg.get("type", "unknown")

                    if msg_type == "assistant" and "message" in msg:
                        content = msg["message"].get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                self.logger.info(f"[Claude] {block['text'][:200]}")
                            elif block.get("type") == "tool_use":
                                self.logger.info(f"[Tool] {block.get('name', '?')}")
                    elif msg_type == "result":
                        self.logger.info(f"[Result] cost=${msg.get('cost_usd', '?')}")
                    else:
                        self.logger.debug(f"[Stream] {msg_type}: {decoded[:100]}")
                except json.JSONDecodeError:
                    self.logger.info(f"[Raw] {decoded[:200]}")
        except Exception as e:
            if not self.stop_event.is_set():
                self.logger.error(f"Output reader error: {e}")

    def _read_stderr(self):
        """Read and log stderr."""
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if self.stop_event.is_set():
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded:
                    self.logger.warning(f"[stderr] {decoded[:200]}")
        except Exception as e:
            if not self.stop_event.is_set():
                self.logger.error(f"Stderr reader error: {e}")

    def send(self, text):
        """Send a message to the running session via stream-json input."""
        if not self.proc or self.proc.poll() is not None:
            self.logger.error("No active session to send to")
            return False

        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
        })
        try:
            self.proc.stdin.write(f"{msg}\n".encode("utf-8"))
            self.proc.stdin.flush()
            self.logger.info(f"[Sent] {text[:100]}")
            return True
        except Exception as e:
            self.logger.error(f"Send error: {e}")
            return False

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Gracefully stop the session."""
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.logger.info("Stopping Claude session...")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.logger.warning("Force killing session")
                self.proc.kill()
        if PID_FILE.exists():
            PID_FILE.unlink()
        self.logger.info("Session stopped")

    def uptime(self):
        if self.started_at:
            return str(datetime.now() - self.started_at).split(".")[0]
        return "not running"


class PiMono:
    """Main supervisor daemon."""

    def __init__(self):
        self.config = load_config()
        self.logger = setup_logging(self.config)
        self.session = ClaudeSession(self.config, self.logger)
        self.scheduler = None
        self.running = False

    def setup_schedules(self):
        """Set up cron-based schedules (persistent, not session-dependent)."""
        if not HAS_SCHEDULER:
            self.logger.warning(
                "apscheduler not installed. Run: pip install apscheduler"
            )
            return

        self.scheduler = BackgroundScheduler()
        schedules = self.config.get("schedule", {})

        for name, sched in schedules.items():
            if not sched.get("enabled", False):
                continue
            cron_expr = sched["cron"]
            prompt = sched["prompt"]

            # Parse cron: minute hour day month day_of_week
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
        """Execute a scheduled prompt."""
        self.logger.info(f"[Schedule] Running '{name}'")
        if self.session.is_alive():
            self.session.send(prompt)
        else:
            self.logger.warning(f"Session not alive, running one-shot for '{name}'")
            self.run_oneshot(prompt)

    def run_oneshot(self, prompt):
        """Run a one-shot prompt via claude -p (no session needed)."""
        cfg = self.config["claude"]
        cmd = [cfg["command"], "-p", prompt, "--add-dir", cfg["project_dir"]]
        if cfg.get("model"):
            cmd.extend(["--model", cfg["model"]])

        self.logger.info(f"[One-shot] {prompt[:80]}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=cfg["project_dir"],
            )
            self.logger.info(f"[One-shot result] {result.stdout[:500]}")
            if result.stderr:
                self.logger.warning(f"[One-shot stderr] {result.stderr[:200]}")
            return result.stdout
        except subprocess.TimeoutExpired:
            self.logger.error("[One-shot] Timed out")
            return None

    def run_interactive(self):
        """Interactive mode: start session + command line for input."""
        self.running = True
        self.logger.info("=== ClaudexClaw starting (interactive) ===")

        # Set up schedules
        self.setup_schedules()

        # Handle signals
        def handle_stop(signum, frame):
            print("\n[ClaudexClaw] Shutting down...")
            self.running = False
            self.session.stop()
            if self.scheduler:
                self.scheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)

        # Start Claude session
        self.session.start()
        print()
        print("=" * 50)
        print("  ClaudexClaw interactive console")
        print("=" * 50)
        print("Commands:")
        print("  <text>        Send prompt to Claude session")
        print("  /status       Show session status")
        print("  /restart      Restart Claude session")
        print("  /oneshot <p>  Run one-shot prompt (separate process)")
        print("  /quit         Stop and exit")
        print("=" * 50)
        print()

        # Start health check thread
        health_thread = Thread(target=self._health_loop, daemon=True)
        health_thread.start()

        # Interactive input loop
        while self.running:
            try:
                user_input = input("ClaudexClaw> ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                break

            if not user_input:
                continue

            if user_input == "/quit":
                break
            elif user_input == "/status":
                alive = self.session.is_alive()
                pid = self.session.proc.pid if self.session.proc else "N/A"
                print(f"  Session: {'alive' if alive else 'dead'}")
                print(f"  PID: {pid}")
                print(f"  Uptime: {self.session.uptime()}")
                print(f"  Restarts: {self.session.restart_count}")
            elif user_input == "/restart":
                print("  Restarting session...")
                self.session.stop()
                self.session.stop_event.clear()
                time.sleep(2)
                self.session.start()
                print("  Session restarted.")
            elif user_input.startswith("/oneshot "):
                prompt = user_input[8:].strip()
                if prompt:
                    print(f"  Running one-shot: {prompt[:60]}...")
                    result = self.run_oneshot(prompt)
                    if result:
                        print(result)
            else:
                # Send to running session
                if self.session.is_alive():
                    self.session.send(user_input)
                else:
                    print("  Session is dead. Use /restart or /oneshot")

        self.session.stop()
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        self.logger.info("=== ClaudexClaw stopped ===")

    def _health_loop(self):
        """Background health check loop."""
        session_cfg = self.config["session"]
        interval = session_cfg.get("health_check_interval", 300)
        max_restarts = session_cfg["max_restart_attempts"]

        while self.running:
            time.sleep(interval)
            if not self.running:
                break

            if self.session.is_alive():
                self.logger.info(
                    f"Health OK | uptime={self.session.uptime()} | "
                    f"restarts={self.session.restart_count}"
                )
            elif session_cfg.get("auto_restart", True):
                if self.session.restart_count < max_restarts:
                    self.logger.warning("Session died, auto-restarting...")
                    self.session.stop_event.clear()
                    delay = session_cfg["restart_delay_seconds"]
                    time.sleep(delay)
                    self.session.start()
                    self.session.restart_count += 1
                else:
                    self.logger.error(f"Max restarts ({max_restarts}) reached.")

    def status(self):
        """Print current status."""
        alive = self.session.is_alive()
        pid = PID_FILE.read_text().strip() if PID_FILE.exists() else "N/A"
        print(f"Session alive: {alive}")
        print(f"PID: {pid}")
        print(f"Uptime: {self.session.uptime()}")
        print(f"Restarts: {self.session.restart_count}")
        if HAS_SCHEDULER:
            print("Scheduler: available")
        else:
            print("Scheduler: not installed (pip install apscheduler)")


def main():
    if len(sys.argv) < 2:
        # Default: run interactive mode
        mono = PiMono()
        mono.run_interactive()
        return

    command = sys.argv[1]

    if command == "prompt" and len(sys.argv) > 2:
        # One-shot prompt
        mono = PiMono()
        prompt = " ".join(sys.argv[2:])
        result = mono.run_oneshot(prompt)
        if result:
            print(result)

    elif command == "send" and len(sys.argv) > 2:
        # Send to running session (TODO: IPC via socket)
        print("TODO: IPC not yet implemented. Use 'prompt' for one-shot.")

    elif command == "status":
        mono = PiMono()
        mono.status()

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
