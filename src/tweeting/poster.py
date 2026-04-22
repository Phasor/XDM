"""
Selenium-based tweet poster. Uses the already-logged-in Chrome driver to
open the compose page, type the tweet, attach any image, and click Post.

Called from the main loop, so it's serialized with DM polling — no driver
thread-safety concerns.
"""

import logging
import os
import time

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


COMPOSE_URL = "https://x.com/compose/post"
COMPOSE_TEXTAREA = '[data-testid="tweetTextarea_0"]'
POST_BUTTON = (
    '[data-testid="tweetButtonInline"], [data-testid="tweetButton"]'
)
FILE_INPUT = '[data-testid="fileInput"]'
ATTACHMENT_PREVIEW = '[data-testid="attachments"]'


class TweetPoster:
    "Posts an approved draft to X.com via Selenium."

    def __init__(self, driver, config):
        self.driver = driver
        self.config = config
        self.logger = logging.getLogger("POSTER")

    def post(self, text, image_path=None):
        """Post `text` (with optional image) as a tweet. Returns tweet_url on
        success, raises on failure. Caller is responsible for updating draft
        status. If `image_path` is set but missing from disk, falls back to
        text-only with a warning."""
        main_handle = self.driver.current_window_handle
        self.driver.execute_script(f"window.open('{COMPOSE_URL}', '_blank');")
        self.driver.switch_to.window(self.driver.window_handles[-1])

        try:
            self._type_text(text)
            if image_path:
                self._attach_image(image_path)
            self._click_post()
            tweet_url = self._wait_for_post_success()
            self.logger.info("Tweet posted: %s", tweet_url or "(url unknown)")
            return tweet_url
        finally:
            self._close_tab_and_return(main_handle)

    # =========================
    # Internals
    # =========================

    def _type_text(self, text):
        try:
            textarea = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, COMPOSE_TEXTAREA))
            )
        except TimeoutException as e:
            raise RuntimeError("Compose textarea not found") from e

        textarea.click()
        time.sleep(0.5)
        # send_keys only supports characters in the Basic Multilingual Plane
        # (U+0000..U+FFFF); emojis like 💪 (U+1F4AA) trip it. CDP Input.insertText
        # handles arbitrary Unicode and still dispatches a synthetic input event,
        # which is what tweetTextarea_0 (Draft.js contenteditable) listens for.
        self.driver.execute_cdp_cmd("Input.insertText", {"text": text})
        time.sleep(1)

    def _attach_image(self, image_path):
        if not os.path.isfile(image_path):
            self.logger.warning(
                "Image path missing, posting text-only: %s", image_path,
            )
            return
        abs_path = os.path.abspath(image_path)
        try:
            file_input = self.driver.find_element(By.CSS_SELECTOR, FILE_INPUT)
        except NoSuchElementException as e:
            raise RuntimeError("Compose file input not found") from e
        file_input.send_keys(abs_path)

        # Wait for the attachment preview to render — confirms upload started.
        try:
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ATTACHMENT_PREVIEW)
                )
            )
        except TimeoutException as e:
            raise RuntimeError("Image upload did not complete") from e
        # Settle for server-side processing so the Post button enables.
        time.sleep(2)

    def _click_post(self):
        try:
            button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, POST_BUTTON))
            )
        except TimeoutException as e:
            raise RuntimeError("Post button not clickable") from e
        button.click()

    def _wait_for_post_success(self, timeout=20):
        """Wait for any signal that the tweet went out. X.com navigates away
        from /compose/post on success. Returns tweet URL if one can be
        recovered from the 'View' toast link, else None."""
        end = time.time() + timeout
        tweet_url = None
        while time.time() < end:
            current = self.driver.current_url or ""
            if "/compose/post" not in current:
                # Navigation away from compose = success.
                tweet_url = self._extract_tweet_url_from_toast()
                return tweet_url
            # Also check for a success toast with a View link that points at
            # the new status URL.
            tweet_url = self._extract_tweet_url_from_toast()
            if tweet_url:
                return tweet_url
            time.sleep(0.5)
        raise RuntimeError("Post did not confirm within timeout")

    def _extract_tweet_url_from_toast(self):
        "Return the newly-posted tweet URL from the success toast, or None."
        # A bare a[href*='/status/'] matches ANY tweet link on the page, and
        # X redirects the compose tab to the home feed on success — so the
        # first match is usually some random tweet from the feed, not ours.
        # Scope strictly to the success toast container.
        try:
            links = self.driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="toast"] a[href*="/status/"]'
            )
            for link in links:
                href = link.get_attribute("href") or ""
                if "/status/" in href:
                    return href
        except (WebDriverException, NoSuchElementException):
            pass
        return None

    def _close_tab_and_return(self, main_handle):
        try:
            handles = self.driver.window_handles
            if len(handles) > 1:
                self.driver.close()
                self.driver.switch_to.window(main_handle)
        except WebDriverException:
            self.logger.warning("Failed to close compose tab cleanly")
            try:
                self.driver.switch_to.window(main_handle)
            except WebDriverException:
                pass
