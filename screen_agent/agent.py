"""
Seny Screen Awareness Agent
Runs on Mac or Windows, watches for drift from priority commitments.
"""
import base64
import io
import logging
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import mss
import mss.tools
from dotenv import load_dotenv
from PIL import Image
from pynput import keyboard, mouse

# --- Config ---

# Load .env from the screen_agent directory
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH)

SENY_URL = os.getenv("SENY_URL", "http://localhost:8000").rstrip("/")
SCREEN_AGENT_KEY = os.getenv("SCREEN_AGENT_KEY", "")
EVAL_INTERVAL = int(os.getenv("EVAL_INTERVAL", "180"))
IDLE_THRESHOLD = int(os.getenv("IDLE_THRESHOLD", "60"))
MACHINE_ID = os.getenv("MACHINE_ID", "") or socket.gethostname()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [screen-agent] %(levelname)s %(message)s"
)
_logger = logging.getLogger("screen_agent")

if not SCREEN_AGENT_KEY:
    _logger.error("SCREEN_AGENT_KEY is not set. Edit screen_agent/.env and add your key.")

# --- Screenshot Capture ---

class ScreenshotCapture:
    """Captures ALL monitors combined and returns JPEG bytes (base64-encoded)."""

    # Max width for the combined screenshot to keep file size reasonable
    _MAX_WIDTH = 3000

    def capture(self) -> str:
        """Take a screenshot of all monitors. Returns base64-encoded JPEG string."""
        with mss.mss() as sct:
            # monitors[0] is the virtual combined rectangle spanning all monitors
            monitor = sct.monitors[0]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # Resize if very wide (e.g. 3x 1440p = 7680px) to keep upload size sane
        if img.width > self._MAX_WIDTH:
            ratio = self._MAX_WIDTH / img.width
            new_size = (self._MAX_WIDTH, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        # Compress to JPEG at 70% quality
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70, optimize=True)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")


# --- Idle Detection ---

class ActivityMonitor:
    """
    Listens for global mouse and keyboard events.
    is_idle() returns True if no input has been received for IDLE_THRESHOLD seconds.
    """

    def __init__(self, idle_threshold: int = IDLE_THRESHOLD):
        self._idle_threshold = idle_threshold
        self._last_input_time = time.time()
        self._lock = threading.Lock()
        self._running = False

    def _record_activity(self, *args, **kwargs):
        with self._lock:
            self._last_input_time = time.time()

    def start(self):
        """Start listening for input events in background threads."""
        if self._running:
            return
        self._running = True

        # Mouse listener: movement and clicks both reset timer
        self._mouse_listener = mouse.Listener(
            on_move=self._record_activity,
            on_click=self._record_activity,
            on_scroll=self._record_activity,
        )
        self._mouse_listener.daemon = True
        self._mouse_listener.start()

        # Keyboard listener: any key press resets timer
        self._keyboard_listener = keyboard.Listener(
            on_press=self._record_activity,
        )
        self._keyboard_listener.daemon = True
        self._keyboard_listener.start()

    def is_idle(self) -> bool:
        with self._lock:
            return (time.time() - self._last_input_time) > self._idle_threshold

    def seconds_since_input(self) -> float:
        with self._lock:
            return time.time() - self._last_input_time


# --- Seny Backend Client ---

class SenyClient:
    """HTTP client for calling Seny backend screen endpoints."""

    def __init__(self, base_url: str = SENY_URL, key: str = SCREEN_AGENT_KEY):
        self._base_url = base_url
        self._headers = {"X-Screen-Agent-Key": key}

    def get_priority(self) -> list:
        """Fetch active priority items. Returns list of dicts."""
        try:
            r = httpx.get(
                f"{self._base_url}/api/screen/priority",
                headers=self._headers,
                timeout=10.0
            )
            r.raise_for_status()
            return r.json().get("items", [])
        except Exception as e:
            _logger.warning("get_priority failed: %s", repr(e))
            return []

    def evaluate(self, screenshot_b64: str, machine_id: str, escalation_stage: int = 0) -> dict:
        """Submit screenshot for evaluation. Returns {status, nudge_fired}."""
        try:
            r = httpx.post(
                f"{self._base_url}/api/screen/evaluate",
                headers=self._headers,
                json={
                    "screenshot_b64": screenshot_b64,
                    "machine_id": machine_id,
                    "escalation_stage": escalation_stage,
                },
                timeout=30.0  # Vision call can take a few seconds
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _logger.warning("evaluate failed: %s", repr(e))
            return {"status": "on_track", "nudge_fired": False}


# --- Main Agent ---

class ScreenAgent:
    """
    Orchestrates screenshot capture, idle detection, and backend evaluation.
    run_loop() blocks forever and is meant to run in a background thread.
    paused flag is read by run_loop; set by tray UI.
    """

    def __init__(self):
        self.paused = False
        self._capture = ScreenshotCapture()
        self._activity = ActivityMonitor()
        self._client = SenyClient()

    def start_activity_monitor(self):
        self._activity.start()

    def run_loop(self):
        """Main loop. Runs forever until process exits."""
        _logger.info(
            "Screen agent started. machine_id=%s interval=%ds idle_threshold=%ds",
            MACHINE_ID, EVAL_INTERVAL, IDLE_THRESHOLD
        )

        if not SCREEN_AGENT_KEY:
            _logger.error("No SCREEN_AGENT_KEY set — agent will not evaluate. "
                          "Set it in screen_agent/.env")

        while True:
            time.sleep(EVAL_INTERVAL)

            # Skip if paused via tray UI
            if self.paused:
                _logger.debug("Paused — skipping evaluation")
                continue

            # Skip if this machine has been idle (user is elsewhere)
            if self._activity.is_idle():
                _logger.debug(
                    "Machine idle for %.0fs — skipping evaluation",
                    self._activity.seconds_since_input()
                )
                continue

            # Take screenshot
            try:
                screenshot_b64 = self._capture.capture()
            except Exception as e:
                _logger.warning("Screenshot failed: %s", repr(e))
                continue

            # Submit to backend
            result = self._client.evaluate(
                screenshot_b64=screenshot_b64,
                machine_id=MACHINE_ID,
            )

            status = result.get("status", "on_track")
            nudge_fired = result.get("nudge_fired", False)

            _logger.info(
                "Evaluation: status=%s nudge_fired=%s machine=%s",
                status, nudge_fired, MACHINE_ID
            )


if __name__ == "__main__":
    import sys

    agent = ScreenAgent()
    agent.start_activity_monitor()

    if sys.platform == "darwin":
        # Mac: use rumps menu bar
        try:
            from tray_mac import run_mac
            run_mac(agent)
        except ImportError:
            _logger.warning("rumps not installed — running headless (no tray icon)")
            agent.run_loop()

    elif sys.platform == "win32":
        # Windows: use pystray system tray
        try:
            from tray_windows import run_windows
            run_windows(agent)
        except ImportError:
            _logger.warning("pystray not installed — running headless (no tray icon)")
            agent.run_loop()

    else:
        # Linux or other: headless fallback
        _logger.info("Unsupported platform for tray icon — running headless")
        agent.run_loop()
