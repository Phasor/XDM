"""
Telegram alerting layer used both as a logging handler (for ERROR/CRITICAL
log records) and as a direct event notifier (bot started, heartbeat lost,
heartbeat recovered, etc).

Failure modes are intentionally silent: an alerting layer must never crash
the application that depends on it. Network errors, rate limits, and bad
config all degrade to a no-op.
"""

import logging
import threading
import time
from collections import OrderedDict

import requests


class Notifier:
    """
    Sends messages to a Telegram chat with built-in dedup.

    Used in two ways:
      1. Wrapped by TelegramHandler so logger.error() forwards to Telegram.
      2. Called directly via .notify() for one-off events from app code.

    Both paths share dedup state, so if app code already alerted "Heartbeat
    lost" and then a logger.error() with the same text fires, only one
    Telegram message is sent.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    MAX_LENGTH = 4000  # leave headroom under Telegram's 4096 limit
    HTTP_TIMEOUT = 10

    def __init__(self, bot_token, chat_id, dedup_window=300):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.dedup_window = dedup_window
        self._recent = OrderedDict()  # dedup_key -> timestamp
        self._lock = threading.Lock()

    def notify(self, text, dedup_key=None):
        """
        Send `text` to Telegram in a background thread.

        If `dedup_key` is provided, dedup is keyed off that string instead
        of the full text — useful for messages that contain a timestamp or
        other varying detail but represent the same event.
        """
        key = dedup_key if dedup_key is not None else text
        if self._is_duplicate(key):
            return
        threading.Thread(target=self._send, args=(text,), daemon=True).start()

    def _is_duplicate(self, key):
        now = time.time()
        with self._lock:
            cutoff = now - self.dedup_window
            # Drop entries older than the window
            while self._recent and next(iter(self._recent.values())) < cutoff:
                self._recent.popitem(last=False)

            if key in self._recent:
                return True

            self._recent[key] = now
            # Bound memory in case of high churn
            if len(self._recent) > 200:
                self._recent.popitem(last=False)
            return False

    def _send(self, text):
        if len(text) > self.MAX_LENGTH:
            text = text[: self.MAX_LENGTH - 20] + "\n... [truncated]"

        url = self.API_URL.format(token=self.bot_token)
        try:
            requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=self.HTTP_TIMEOUT,
            )
        except Exception:
            # Swallow everything — alerting must never crash the app.
            pass


class TelegramHandler(logging.Handler):
    """
    A logging.Handler that forwards records to a Notifier. Used to wire
    logger.error() / logger.critical() into Telegram alerts.
    """

    def __init__(self, notifier, level=logging.ERROR):
        super().__init__(level=level)
        self.notifier = notifier

    def emit(self, record):
        try:
            text = self.format(record)
        except Exception:
            return
        self.notifier.notify(text)
