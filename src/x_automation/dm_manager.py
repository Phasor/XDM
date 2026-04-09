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


def normalize_text(text):
    "Canonical normalization for message text comparison and hashing."
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    text = " ".join(text.split())
    return text.strip()


def generate_message_id(conv_id, sender, text, occurrence=1):
    "Generate a deterministic message ID from content."
    normalized = normalize_text(text)
    raw = f"{conv_id}|{sender}|{normalized}|{occurrence}"
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

        Uses content-addressed hashing to compare on-screen messages
        against Supabase history. Returns new user messages with
        deterministic message_id hashes attached.
        """
        try:
            full_chat = self.extract_messages()
        except WebDriverException as e:
            self.logger.error("Error extracting messages: %s", str(e).splitlines()[0])
            return []

        self.logger.info("Extracted %d messages from chat.", len(full_chat))

        # First conversation with empty Supabase: only take the last user message
        if not saved_messages:
            self.logger.info("No saved messages, taking only the last user message.")
            for msg in reversed(full_chat):
                if msg["author"] == "user":
                    msg_hash = generate_message_id(conv_id, "user", msg["text"])
                    msg["message_id"] = msg_hash
                    return [msg]
            return []

        # Build set of hashes for saved messages
        saved_hashes = set()
        occurrence_saved = {}
        for msg in saved_messages:
            key = (msg["sender"], normalize_text(msg["message_text"]))
            occurrence_saved[key] = occurrence_saved.get(key, 0) + 1
            msg_hash = generate_message_id(
                conv_id, msg["sender"], msg["message_text"], occurrence_saved[key]
            )
            saved_hashes.add(msg_hash)

        # Hash on-screen messages and find new ones
        new_user_messages = []
        occurrence_screen = {}
        for msg in full_chat:
            key = (msg["author"], normalize_text(msg["text"]))
            occurrence_screen[key] = occurrence_screen.get(key, 0) + 1
            msg_hash = generate_message_id(
                conv_id, msg["author"], msg["text"], occurrence_screen[key]
            )

            if msg_hash not in saved_hashes and msg["author"] == "user":
                msg["message_id"] = msg_hash
                new_user_messages.append(msg)

        if not new_user_messages:
            self.logger.info("No new user messages found.")
            return []

        self.logger.info("Found %d new user messages.", len(new_user_messages))
        return new_user_messages

    def extract_messages(self):
        "Extract messages from opened chat (text and author only)."
        results = []

        container = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[data-testid="dm-message-list"]')
            )
        )
        time.sleep(3)  # Allow time for messages to fully load

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
