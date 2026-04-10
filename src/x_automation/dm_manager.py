import copy
import hashlib
import logging
import re
import time
import unicodedata
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)


# Number of previous messages mixed into each message's content hash. Disambiguates
# duplicate text by surrounding context: identical text in different conversation
# positions produces different hashes. Collisions only occur if WINDOW+1 consecutive
# (sender, text) pairs are byte-identical — vanishingly rare in DM traffic.
WINDOW = 10

# ASCII Unit Separator: cannot appear in normalized text (whitespace is collapsed,
# zero-width chars are stripped, but \x1f is preserved). Used as a field delimiter
# inside the hash input so that field boundaries can't be ambiguous.
SEPARATOR = "\x1f"


def normalize_text(text):
    "Canonical normalization for message text comparison and hashing."
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    text = " ".join(text.split())
    return text.strip()


def generate_message_id(conv_id, sender, text, prev_texts):
    """Context-windowed content hash for a message.

    `prev_texts` is the list of up to WINDOW immediately preceding message texts
    (oldest first). Empty list is valid for the very first message in a conversation
    or for the offline-gap fallback path. The hash is deterministic, so re-saving
    the same logical message yields the same ID and the upsert is idempotent.
    """
    parts = [conv_id, sender, normalize_text(text)] + [
        normalize_text(t) for t in prev_texts
    ]
    raw = SEPARATOR.join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class DmListener:
    "Single-step DM detector (no internal loop)."

    def __init__(self, driver, config):
        self.driver = driver
        self.config = config
        self.logger = logging.getLogger("CHAT")

        self.chats = {}
        self.prev_chats = {}
        self.first_run = True
        self._startup_queue = []  # unread conv_ids to process on startup

    def detect_new_message(self):
        "Run one detection cycle and return first change or None."
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "dm-main-container"))
            )
            time.sleep(1)  # Allow time for the DM list to fully load

            self.extract_conversations()
        except WebDriverException as e:
            self.logger.error("Error detecting DM changes: %s", str(e).splitlines()[0])
            self.driver.get(self.config["urls"]["chat"])
            return None

        if not self.chats:
            return None

        if self.first_run:
            self.logger.info("Initial conversations loaded, marking as baseline.")
            self.prev_chats = copy.deepcopy(self.chats)
            self.first_run = False

            # Queue all unread messages that arrived while bot was offline
            for conv_id, data in self.chats.items():
                if data["unread"] and data["author"] != "assistant":
                    self._startup_queue.append(conv_id)

            if self._startup_queue:
                self.logger.info(
                    "Found %d unread conversations from while offline.",
                    len(self._startup_queue),
                )

        # Drain startup queue before normal diff detection
        while self._startup_queue:
            conv_id = self._startup_queue.pop(0)
            if conv_id in self.chats:
                self.logger.info("Processing offline unread: %s", conv_id)
                return conv_id, "new_message", self.chats[conv_id]

        for conv_id, data in self.chats.items():
            prev = self.prev_chats.get(conv_id)

            # --- New Chat ---
            if not prev:
                return conv_id, "new_chat", data

            # --- Same message → ignore
            if data["last_message"] == prev["last_message"]:
                continue

            if data["author"] == "assistant":
                self.logger.debug("Assistant message detected, skipping: %s", conv_id)
                self.commit(conv_id)
                continue

            # --- New message
            return conv_id, "new_message", data

        return None

    def extract_conversations(self):
        "Extract current conversations."
        items = self.driver.find_elements(
            By.CSS_SELECTOR, 'div[data-testid^="dm-conversation-item"]'
        )

        new_chats = {}

        for item in items:
            try:
                aria = item.get_attribute("aria-description") or ""
                testid = item.get_attribute("data-testid") or ""
                span_elem = item.find_elements(By.TAG_NAME, "span")

                if span_elem and span_elem[0].text.strip() == "You:":
                    author = "assistant"
                else:
                    author = "user"

                parts = [p.strip() for p in aria.split(",")]

                username = parts[1].lstrip("@") if len(parts) > 1 else ""
                unread = parts[-1].lower() == "unread"

                if unread:
                    message_parts = parts[2:-2]
                else:
                    message_parts = parts[2:-1]

                last_message = ", ".join(message_parts).strip()

                conv_id_raw = testid.replace("dm-conversation-item-", "")
                conv_id = conv_id_raw.replace(":", "-")

                new_chats[conv_id] = {
                    "username": username,
                    "author": author,
                    "last_message": last_message,
                    "unread": unread,
                }
            except (StaleElementReferenceException, WebDriverException):
                self.logger.debug("Stale element during conversation extraction, skipping.")
                continue

        self.chats = new_chats

    def commit(self, conv_id):
        "Mark only one conversation as processed."
        if conv_id in self.chats:
            self.prev_chats[conv_id] = copy.deepcopy(self.chats[conv_id])


class OpenChat:
    "Class to open chat and to read and write messages"

    def __init__(self, driver, config):
        self.driver = driver
        self.logger = logging.getLogger("CHAT")
        self.config = config

    def read_messages(self, saved_messages, conv_id):
        """Read messages from page and return only new user messages.

        Aligns the on-screen window against the saved tail by anchoring on the
        last saved (sender, text) tuple, then computes context-windowed hashes
        for any messages past the alignment point. See plan in
        plans/merry-riding-scone.md for the full algorithm.
        """
        try:
            screen = self.extract_messages()
        except WebDriverException as e:
            self.logger.error("Error extracting messages: %s", str(e).splitlines()[0])
            return []

        if not screen:
            return []

        if not saved_messages:
            self.logger.info(
                "No saved messages (n_screen=%d), first-run fallback.", len(screen)
            )
            return self._first_run_last_user(screen, conv_id)

        saved_keys = [
            (m["sender"], normalize_text(m["message_text"])) for m in saved_messages
        ]
        screen_keys = [
            (m["author"], normalize_text(m["text"])) for m in screen
        ]

        # Anchor-based alignment: find positions in screen matching the last saved
        # tuple, then verify backward. Picks the longest valid overlap. Handles the
        # case where the on-screen window starts mid-conversation OR begins with an
        # assistant interjection just after the last saved user message.
        last_saved = saved_keys[-1]
        overlap = 0
        for p in range(len(screen_keys)):
            if screen_keys[p] != last_saved:
                continue
            k = p + 1
            if k > len(saved_keys):
                continue
            if saved_keys[-k:] == screen_keys[:k]:
                if k > overlap:
                    overlap = k

        self.logger.info(
            "Alignment: n_saved=%d n_screen=%d overlap=%d",
            len(saved_keys), len(screen_keys), overlap,
        )

        if overlap == 0:
            self.logger.warning(
                "No alignment between saved tail and on-screen — possible "
                "message gap. Falling back to last user message only."
            )
            return self._first_run_last_user(screen, conv_id)

        # Unified conversation = saved tail + new on-screen tail (oldest first).
        # New messages live at unified[len(saved_keys):]; their prev context is
        # drawn from earlier positions in unified, which may span both saved
        # rows and just-detected on-screen messages.
        unified = saved_keys + screen_keys[overlap:]
        new_user_messages = []
        for i in range(len(saved_keys), len(unified)):
            sender, _ = unified[i]
            if sender != "user":
                continue
            prev_texts = [t for _, t in unified[max(0, i - WINDOW):i]]
            screen_idx = overlap + (i - len(saved_keys))
            original = screen[screen_idx]
            msg_id = generate_message_id(
                conv_id, "user", original["text"], prev_texts
            )
            new_user_messages.append(
                {
                    "author": "user",
                    "text": original["text"],
                    "message_id": msg_id,
                }
            )

        if not new_user_messages:
            self.logger.info("No new user messages found.")
        else:
            self.logger.info("Found %d new user messages.", len(new_user_messages))
        return new_user_messages

    def _first_run_last_user(self, screen, conv_id):
        """Return only the most recent user message from the on-screen list.

        Used both for the empty-Supabase first run AND as the offline-gap
        fallback when alignment fails. Hashes with empty prev context — that
        asymmetry is fine because subsequent polls identify this message via
        the (sender, text) tuple in alignment, not by re-computing its hash.
        """
        for msg in reversed(screen):
            if msg["author"] == "user":
                msg_id = generate_message_id(conv_id, "user", msg["text"], [])
                self.logger.info("First-run/fallback selected: %s", msg["text"][:60])
                return [
                    {
                        "author": "user",
                        "text": msg["text"],
                        "message_id": msg_id,
                    }
                ]
        return []

    def extract_messages(self):
        "Extract messages from opened chat (text and author only)."
        results = []

        container = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[data-testid="dm-message-list"]')
            )
        )
        time.sleep(3)

        # Scroll to bottom to ensure latest messages are visible
        self.driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollHeight", container
        )
        time.sleep(3)

        items = container.find_elements(By.CSS_SELECTOR, "ul > li")
        for li in items:
            try:
                msg_box = li.find_element(By.CSS_SELECTOR, '[data-testid^="message-"]')
            except NoSuchElementException:
                continue
            except StaleElementReferenceException:
                continue

            try:
                classes = msg_box.get_attribute("class")

                if "justify-end" in classes:
                    author = "assistant"
                elif "justify-start" in classes:
                    author = "user"
                else:
                    continue

                try:
                    text_elem = msg_box.find_element(
                        By.CSS_SELECTOR,
                        '[data-testid^="message-text-"] span[dir="auto"]',
                    )
                except NoSuchElementException:
                    continue

                text = text_elem.get_attribute("innerText").strip()
                if text:
                    results.append({"author": author, "text": text})
            except StaleElementReferenceException:
                continue

        return results

    def send_message(self, text):
        "Send message to opened chat."
        try:
            textarea = self.driver.find_element(
                By.CSS_SELECTOR, '[data-testid="dm-composer-textarea"]'
            )
            textarea.clear()
            textarea.send_keys(text)

            time.sleep(1)

            self.driver.find_element(
                By.CSS_SELECTOR, '[data-testid="dm-composer-send-button"]'
            ).click()

            time.sleep(2)
            self.logger.info("Message sent.")
            return True

        except NoSuchElementException as e:
            self.logger.error("Failed to send message, element not found: %s", str(e))
            return False
