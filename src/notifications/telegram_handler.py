"""
Logging handler that forwards ERROR/CRITICAL log records to a Telegram chat
via the Bot API. Designed to be hung off the root logger so any error raised
anywhere in the bot is delivered automatically.

Failure modes are intentionally silent: an alerting layer must never crash
the application that depends on it. Network errors, rate limits, and bad
config all degrade to a no-op (with at most a warning printed to stderr
once at startup if config is missing).
"""

import logging
import threading
import time
from collections import OrderedDict

import requests


class TelegramHandler(logging.Handler):
    """
    A logging.Handler that posts records to Telegram's sendMessage API.

    - Dedupes identical messages within `dedup_window` seconds so a crash
      loop doesn't flood the chat.
    - Posts in a background thread so logging never blocks on the network.
    - Caps message length to Telegram's 4096-char limit.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    MAX_LENGTH = 4000  # leave headroom under Telegram's 4096 limit
    HTTP_TIMEOUT = 10

    def __init__(self, bot_token, chat_id, level=logging.ERROR, dedup_window=300):
        super().__init__(level=level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.dedup_window = dedup_window
        self._recent = OrderedDict()  # message -> timestamp
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            text = self.format(record)
        except Exception:
            return

        if self._is_duplicate(text):
            return

        # Fire and forget so logging.error() never blocks
        threading.Thread(
            target=self._send,
            args=(text,),
            daemon=True,
        ).start()

    def _is_duplicate(self, text):
        """True if we've sent this exact text within dedup_window seconds."""
        now = time.time()
        with self._lock:
            # Drop entries older than the window
            cutoff = now - self.dedup_window
            while self._recent and next(iter(self._recent.values())) < cutoff:
                self._recent.popitem(last=False)

            if text in self._recent:
                return True

            self._recent[text] = now
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
