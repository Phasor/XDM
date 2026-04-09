import copy
import logging
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)


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

    def read_messages(self, saved_messages):
        """Read messages from page and return only new ones.

        Args:
            saved_messages: list of dicts from Supabase with 'message_text' and 'sender' keys
        """
        try:
            full_chat = self.extract_messages()
        except WebDriverException as e:
            self.logger.error("Error extracting messages: %s", str(e).splitlines()[0])
            return []

        self.logger.info("Extracted %d messages from chat.", len(full_chat))

        # Build set of saved message texts for fast lookup
        saved_texts = set()
        for msg in saved_messages:
            saved_texts.add((msg["message_text"].strip(), msg["sender"]))

        # Find messages on page that aren't in Supabase
        new_messages = []
        for msg in full_chat:
            key = (msg["text"].strip(), msg["author"])
            if key not in saved_texts:
                new_messages.append(msg)

        if not new_messages:
            self.logger.warning("No new user message found in conversation.")
            return []

        self.logger.info("Found %d new messages.", len(new_messages))

        last_msg = new_messages[-1]
        if last_msg["author"] == "assistant":
            self.logger.warning(
                "Last message is from assistant, skipping conversation: %s"
            )
            return []

        return full_chat

    def extract_messages(self):
        "Extract messages from opened chat"
        results = []

        container = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[data-testid="dm-message-list"]')
            )
        )
        time.sleep(3)  # Allow time for messages to fully load
        # input("Press enter after DM messages are fully loaded.")

        items = container.find_elements(By.CSS_SELECTOR, "ul > li")
        for li in items:
            try:
                msg_box = li.find_element(By.CSS_SELECTOR, '[data-testid^="message-"]')
            except NoSuchElementException:
                continue
            except StaleElementReferenceException:
                continue

            try:
                # --- detect author and message ID ---
                classes = msg_box.get_attribute("class")
                message_id = msg_box.get_attribute("data-testid")

                if "justify-end" in classes:
                    author = "assistant"
                elif "justify-start" in classes:
                    author = "user"
                else:
                    self.logger.warning("Unknown message format, skipping.")
                    continue

                # --- extract text ---
                try:
                    text_elem = msg_box.find_element(
                        By.CSS_SELECTOR,
                        '[data-testid^="message-text-"] span[dir="auto"]',
                    )
                except NoSuchElementException:
                    self.logger.warning("Message type is not supported, skipping.")
                    continue

                text = text_elem.get_attribute("innerText").strip()

                if text:
                    results.append(
                        {"message_id": message_id, "author": author, "text": text}
                    )
            except StaleElementReferenceException:
                self.logger.debug("Stale element during message extraction, skipping.")
                continue

        return results

    def send_message(self, text):
        "Send message to opened chat. Returns True if send was verified."
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

            # Verify the message appeared
            time.sleep(2)
            messages = self.extract_messages()
            if messages and messages[-1]["author"] == "assistant":
                self.logger.info("Message sent and verified.")
                return True

            self.logger.warning("Message send could not be verified.")
            return False

        except NoSuchElementException as e:
            self.logger.error("Failed to send message, element not found: %s", str(e))
            return False

    def get_last_message_id(self):
        "Get message ID of the last message in the opened chat"
        time.sleep(1)
        messages = self.extract_messages()
        if messages:
            return messages[-1]["message_id"]
        return None
