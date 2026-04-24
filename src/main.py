import json
import logging
import logging.handlers
import os
import queue
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from selenium.common import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from seleniumbase import Driver

from llm.llm_client import LLM
from notifications.telegram_bot import TelegramBot
from notifications.telegram_handler import Notifier, TelegramHandler
from storage.supabase_client import SupaBase
from tweeting.approval import ApprovalFlow
from tweeting.composer import TweetComposer
from tweeting.image_gen import ImageGenerator
from tweeting.poster import TweetPoster
from tweeting.scheduler import TweetScheduler
from x_automation.dm_manager import (
    DmListener,
    OpenChat,
    WINDOW,
    generate_message_id,
    normalize_text,
)
from x_automation.login import Login


CONFIG_TEMPLATE = {
    "logging_level": "INFO",
    "chrome": {
        "headless": False,
        "proxy": "",
        "undetected_chromedirver": True,
        "width_height": [780, 820],
        "window_position": [755, 0],
        "user_data_dir": "chrome_profile",
    },
    "x.com": {
        "username": "",
        "password": "",
        "passcode": "1234",
        "polling_interval": 5,
    },
    "supabase": {
        "project_url": "",
        "secret_key": "",
        "db_url": "",
        "conversations_table": "conversations",
        "messages_table": "messages",
        "drafts_table": "drafts",
        "context_message_limit": 20,
    },
    "openrouter": {
        "api_key": "",
        "model": "openai/gpt-3.5-turbo",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "timeout": 30,
        "character_file": "prompts/character.txt",
        "personality_file": "prompts/personality.txt",
    },
    "tweeting": {
        "enabled": False,
        "require_approval": True,
        "character_name": "default",
        "character_file": "prompts/character.txt",
        "character_reference_images": [],
        "context_recent_posts": 20,
        "draft_expiry_hours": 4,
        "schedule_enabled": False,
        "timezone": "UTC",
        "windows": [
            "08:00-10:30",
            "12:30-14:30",
            "18:00-20:00",
            "21:30-23:00",
        ],
        "posts_per_day_target": 4,
        "min_gap_minutes": 60,
    },
    "fal_ai": {
        "api_key": "",
        "mode": "flux_lora",
        "model": "fal-ai/flux-lora",
        "lora_url": "",
        "lora_scale": 1.0,
        "trigger_phrase": "",
        "image_size": "portrait_16_9",
        "timeout_seconds": 180,
        "image_dir": "generated_images",
    },
    "urls": {"base": "https://x.com/", "chat": "https://x.com/i/chat"},
    "telegram": {
        "bot_token": "",
        "chat_id": "",
    },
}


class Chrome:
    "A class to manage chrome driver"

    def __init__(self, logger, config):
        self.logger = logger
        self.config = config
        self.driver_instance = None

    def driver(self):
        "Initialize Chrome driver"
        chrome_args = ",".join([
            "--disable-application-cache",
            "--disk-cache-size=0",
            "--disable-cache",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
        ])
        self.driver_instance = Driver(
            uc=self.config["chrome"]["undetected_chromedirver"],
            headless2=self.config["chrome"].get("headless", False),
            user_data_dir=os.path.abspath(self.config["chrome"]["user_data_dir"]),
            proxy=self.config["chrome"]["proxy"] or None,
            chromium_arg=chrome_args,
        )

        self.driver_instance.set_window_position(
            *self.config["chrome"]["window_position"]
        )
        self.driver_instance.set_window_size(*self.config["chrome"]["width_height"])
        self.logger.info("Chrome driver initialized successfully")
        return self.driver_instance

    def is_alive(self):
        "Check if the Chrome driver is still responsive"
        try:
            _ = self.driver_instance.title
            return True
        except Exception:
            return False

    def close(self):
        "Close running chrome instance"
        if self.driver_instance:
            try:
                self.driver_instance.quit()
            except Exception:
                pass
            self.driver_instance = None
            self.logger.info("Chrome driver closed successfully")
        else:
            self.logger.warning("No running chrome driver found to close")


class XAutomation:
    "Main class for X automation tasks"

    def __init__(self):
        self.config = self.load_config()
        self.notifier = _setup_logging(self.config)

        self.passcode = os.getenv("X_PASSCODE") or self.config["x.com"]["passcode"]
        if not (self.passcode.isdigit() and len(self.passcode) == 4):
            raise ValueError("Passcode must be exactly 4 digits")

        self.logger = logging.getLogger("MAIN")
        self.supabase = SupaBase(self.config)
        self.llm = LLM(self.config)
        self.chrome = Chrome(self.logger, self.config)
        self.driver = self.chrome.driver()
        self.listener = DmListener(self.driver, self.config)
        self.opened_chat = OpenChat(self.driver, self.config)
        self._failure_counts = {}  # conv_id -> consecutive failure count
        self._max_failures = 3
        self._start_time = time.time()
        self._max_uptime = 6 * 3600  # restart Chrome every 6 hours

        # Heartbeat watchdog state. Updated each main_loop iteration; a
        # background thread alerts via Telegram if it goes stale.
        self._heartbeat_at = time.time()
        self._heartbeat_stale_secs = 300  # 5 min
        self._heartbeat_check_secs = 60
        self._shutdown = threading.Event()
        self._heartbeat_thread = None

        # Tweeting pipeline (disabled by default; gated on config flag).
        self.tweeting_enabled = bool(self.config.get("tweeting", {}).get("enabled"))
        self.tweet_poster = None
        self.approval = None
        self.tg_bot = None
        self.scheduler = None
        self.compose_queue = queue.Queue()
        self._draft_failures = {}  # draft_id -> consecutive post failure count
        if self.tweeting_enabled:
            self._setup_tweeting()

    def _setup_tweeting(self):
        "Instantiate the tweeting components (composer, image-gen, poster, approval, scheduler, TG bot)."
        composer = TweetComposer(self.config)
        image_gen = ImageGenerator(self.config)
        self.tweet_poster = TweetPoster(self.driver, self.config)
        self.tg_bot = TelegramBot(self.config, self.supabase, self.compose_queue)
        self.approval = ApprovalFlow(
            self.config, self.supabase, composer, image_gen, self.tg_bot,
        )
        self.scheduler = TweetScheduler(self.config, self.supabase)
        self.logger.info(
            "Tweeting pipeline initialized (schedule_enabled=%s)",
            self.scheduler.enabled,
        )

    def start(self):
        "Start the automation process"
        try:
            self._login_and_navigate()

            # Bot is logged in and ready. Notify and start the watchdog thread
            # before entering the main loop.
            self._notify_event(
                f"\u2705 XDM started: @{self.config['x.com']['username']}",
                dedup_key="bot_started",
            )
            self._start_heartbeat_thread()
            if self.tg_bot:
                self.tg_bot.start()

            self.main_loop()

        except KeyboardInterrupt:
            self.logger.warning("Keyboard interrupt received, exiting...")

        finally:
            if self.tg_bot:
                self.tg_bot.stop()
            self._stop_heartbeat_thread()
            self.chrome.close()

    def _notify_event(self, text, dedup_key=None):
        "Send a Telegram event notification (no-op if Telegram not configured)."
        if self.notifier:
            self.notifier.notify(text, dedup_key=dedup_key)

    def _start_heartbeat_thread(self):
        "Background watchdog that alerts on a stalled main loop."
        self._shutdown.clear()
        self._heartbeat_at = time.time()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_watchdog, daemon=True, name="heartbeat-watchdog",
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat_thread(self):
        self._shutdown.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        self._heartbeat_thread = None

    def _heartbeat_watchdog(self):
        """
        Runs in a background thread. Alerts via Telegram if main_loop stops
        updating self._heartbeat_at for longer than _heartbeat_stale_secs.
        Sends a "recovered" alert when the heartbeat resumes.
        """
        alerted = False
        while not self._shutdown.is_set():
            stale_for = time.time() - self._heartbeat_at
            if stale_for > self._heartbeat_stale_secs:
                if not alerted:
                    self._notify_event(
                        f"\u26a0\ufe0f XDM heartbeat lost \u2014 main loop stalled "
                        f"for {int(stale_for)}s",
                        dedup_key="heartbeat_lost",
                    )
                    alerted = True
            else:
                if alerted:
                    self._notify_event(
                        "\u2705 XDM heartbeat recovered",
                        dedup_key="heartbeat_recovered",
                    )
                    alerted = False
            self._shutdown.wait(self._heartbeat_check_secs)

    def _login_and_navigate(self):
        "Login to X.com and navigate to DM inbox"
        self.driver.get(self.config["urls"]["base"])
        self.login_manager = Login(self.driver, self.config)
        self.login_manager.login()
        self.driver.get(self.config["urls"]["chat"])
        self.logger.debug("Navigated to: %s", self.config["urls"]["chat"])
        self.enter_passcode()

    def _reinitialize_driver(self):
        "Tear down Chrome and rebuild everything for crash recovery"
        self.logger.warning("Reinitializing Chrome driver...")
        self.chrome.close()
        time.sleep(5)
        self.driver = self.chrome.driver()
        self.listener = DmListener(self.driver, self.config)
        self.opened_chat = OpenChat(self.driver, self.config)
        if self.tweeting_enabled:
            self.tweet_poster = TweetPoster(self.driver, self.config)
        self._login_and_navigate()

    def ensure_session(self):
        "Check if session is still active, re-login if expired"
        try:
            login_button = self.driver.find_elements(
                By.CSS_SELECTOR, "a[data-testid='loginButton']"
            )
            if login_button:
                self.logger.warning("Session expired, re-authenticating...")
                self.login_manager.login()
                self.driver.get(self.config["urls"]["chat"])
                self.enter_passcode()
                return True
        except WebDriverException as e:
            self.logger.error("Error checking session: %s", str(e).splitlines()[0])
        return False

    def enter_passcode(self):
        "Enter chat passcode if required to access chat"
        time.sleep(5)
        try:
            element = WebDriverWait(self.driver, 20).until(
                EC.any_of(
                    EC.presence_of_element_located((By.ID, "dm-main-container")),
                    EC.presence_of_element_located(
                        (
                            By.XPATH,
                            '//div[@data-testid="pin-title" and text()="Enter Passcode"]',
                        )
                    ),
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, '[data-testid="dm-message-list"]')
                    ),
                )
            )
            if (
                element.get_attribute("id") == "dm-main-container"
                or element.get_attribute("data-testid") == "dm-message-list"
            ):
                return False

            self.logger.info("Passcode required to access chats, entering passcode.")
            container = self.driver.find_element(
                By.XPATH, '//div[@data-testid="pin-code-input-container"]'
            )

            inputs = container.find_elements(By.XPATH, './/input[@maxlength="1"]')
            if len(inputs) != 4:
                raise WebDriverException("Could not find 4 input fields")

            for i in range(4):
                inputs[i].click()
                inputs[i].send_keys(self.passcode[i])
                time.sleep(0.5)

            element = WebDriverWait(self.driver, 20).until(
                EC.any_of(
                    EC.presence_of_element_located((By.ID, "dm-main-container")),
                    EC.presence_of_element_located(
                        (
                            By.XPATH,
                            '//div[@data-testid="pin-error" and contains(., "incorrect")]',
                        )
                    ),
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, '[data-testid="dm-message-list"]')
                    ),
                )
            )
            if (
                element.get_attribute("id") == "dm-main-container"
                or element.get_attribute("data-testid") == "dm-message-list"
            ):
                self.logger.info("Passcode accepted.")
                return True
            raise ValueError("Incorrect passcode entered.")

        except WebDriverException as e:
            raise WebDriverException(
                f"Error during passcode entry: {str(e).splitlines()[0]}"
            ) from e

    def main_loop(self):
        "Main loop to listen for DM changes and to read messages"

        while True:
            # Update heartbeat so the watchdog thread knows the loop is alive.
            self._heartbeat_at = time.time()

            # Periodic Chrome restart for memory management
            if time.time() - self._start_time > self._max_uptime:
                self.logger.info("Scheduled Chrome restart for memory management.")
                try:
                    self._reinitialize_driver()
                    self._start_time = time.time()
                except Exception as e:
                    self.logger.error("Scheduled restart failed: %s", e)
                    time.sleep(30)
                    continue

            # Check if Chrome is still alive; recover if not
            if not self.chrome.is_alive():
                self.logger.error("Chrome driver is dead, restarting...")
                try:
                    self._reinitialize_driver()
                except Exception as e:
                    self.logger.error("Failed to reinitialize driver: %s", e)
                    time.sleep(30)
                    continue

            # Tweeting pipeline ticks (no-ops if tweeting disabled).
            if self.tweeting_enabled:
                self._tick_expire_drafts()
                self._tick_generate_draft()
                self._tick_post_approved()

            new_message = self.listener.detect_new_message()
            if not new_message:
                # Check if passcode screen is blocking the DM inbox
                try:
                    passcode_prompt = self.driver.find_elements(
                        By.XPATH,
                        '//div[@data-testid="pin-title" and text()="Enter Passcode"]',
                    )
                    if passcode_prompt:
                        self.logger.info("Passcode prompt detected, re-entering.")
                        self.enter_passcode()
                except WebDriverException:
                    pass
                self.ensure_session()
                time.sleep(self.config["x.com"]["polling_interval"])
                continue

            conv_id, change_type, data = new_message
            self.logger.info("[%s] %s → %s", change_type, conv_id, data["last_message"])

            url = "https://x.com/i/chat/" + conv_id
            self.driver.execute_script(f"window.open('{url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])

            try:
                self.supabase.upsert_conversation(conv_id, data["username"])

                if self.enter_passcode():
                    self.driver.get(url)

                # 1. Fetch a bounded saved tail for alignment. 100 covers the
                # ~17-msg on-screen window plus a comfortable offline-gap margin.
                all_saved = self.supabase.get_messages(conv_id, limit=100)

                # 2. Find new user messages via context-windowed hashing.
                new_chat = self.opened_chat.read_messages(all_saved, conv_id)

                # Detect an orphaned user message: no new user messages detected,
                # but the tail of saved history is a user msg with no assistant
                # follow-up. This happens when a prior poll saved the user message
                # but the reply path failed (LLM error, network blip, crash, etc).
                # Retry the reply path without re-saving.
                orphaned_retry = (
                    not new_chat
                    and all_saved
                    and all_saved[-1]["sender"] == "user"
                )

                if not new_chat and not orphaned_retry:
                    self.listener.commit(conv_id)
                    continue

                if orphaned_retry:
                    self.logger.warning(
                        "Orphaned user message detected for %s, retrying reply.",
                        conv_id,
                    )
                    self._notify_event(
                        "\u26a0\ufe0f XDM retrying orphaned user message "
                        f"(@{data['username']})",
                        dedup_key=f"orphan_retry_{conv_id}",
                    )

                # 3. Save new user messages to Supabase immediately. No-op on the
                # orphaned-retry path (new_chat is empty there).
                for msg in new_chat:
                    self.supabase.save_message(
                        conv_id, msg["message_id"], msg["author"],
                        normalize_text(msg["text"])
                    )

                # 4. Re-fetch recent history for LLM context (normal limit).
                # This now includes the just-saved user messages, so it's also
                # the right source for the assistant reply's prev context.
                chat_history = self.supabase.get_messages(conv_id)

                # 5. Get LLM response from Supabase history only (single source)
                llm_response = self.llm.get_response(chat_history)
                if not llm_response:
                    self.logger.error("LLM returned empty response, skipping reply.")
                    self.listener.commit(conv_id)
                    continue

                # 6. Send reply
                self.opened_chat.send_message(llm_response)

                # 7. Save assistant reply with context-windowed hash. Reuse
                # chat_history's tail as prev context — no extra DB round-trip.
                reply_prev = [
                    normalize_text(m["message_text"]) for m in chat_history[-WINDOW:]
                ]
                reply_hash = generate_message_id(
                    conv_id, "assistant", llm_response, reply_prev
                )
                self.supabase.save_message(
                    conv_id, reply_hash, "assistant", normalize_text(llm_response)
                )
                self.supabase.update_last_message_time(conv_id)
                self.listener.commit(conv_id)
                self._failure_counts.pop(conv_id, None)

            except Exception as e:
                self.logger.error(
                    "Error processing conversation %s: %s", conv_id, str(e)
                )
                self._failure_counts[conv_id] = (
                    self._failure_counts.get(conv_id, 0) + 1
                )
                if self._failure_counts[conv_id] >= self._max_failures:
                    self.logger.warning(
                        "Conversation %s failed %d times, skipping.",
                        conv_id, self._failure_counts[conv_id],
                    )
                    self.listener.commit(conv_id)
                    self._failure_counts.pop(conv_id, None)

            finally:
                time.sleep(2)
                self._cleanup_tabs()

    # =========================
    # Tweeting lifecycle ticks
    # =========================

    def _tick_expire_drafts(self):
        try:
            self.approval.expire_pending()
        except Exception as e:
            self.logger.error("expire_pending tick failed: %s", e)

    def _tick_generate_draft(self):
        "Enqueue a scheduled slot if due, then drain any pending compose requests."
        try:
            if self.scheduler and self.scheduler.should_fire():
                self.compose_queue.put({"type": "new", "source": "schedule"})
        except Exception as e:
            self.logger.error("scheduler tick failed: %s", e)

        while True:
            try:
                req = self.compose_queue.get_nowait()
            except queue.Empty:
                return

            try:
                req_type = req.get("type")
                if req_type == "regen":
                    self.approval.regenerate(req["replace_draft_id"])
                elif req_type == "prompt":
                    self.approval.create_draft_from_prompt(req["image_prompt"])
                else:
                    self.approval.create_new_draft()
            except Exception as e:
                self.logger.error("compose request failed: %s", e)

    def _tick_post_approved(self):
        "Post any approved drafts whose scheduled_for has arrived."
        try:
            due = self.supabase.get_approved_due()
        except Exception as e:
            self.logger.error("get_approved_due failed: %s", e)
            return

        for draft in due:
            draft_id = draft["draft_id"]
            try:
                tweet_url = self.tweet_poster.post(
                    draft["text"], image_path=draft.get("image_path"),
                )
                self.supabase.update_draft(draft_id, {
                    "status": "posted",
                    "posted_at": _now_iso(),
                    "tweet_url": tweet_url,
                })
                self.approval.confirm_posted(draft_id, tweet_url)
                self._draft_failures.pop(draft_id, None)

            except Exception as e:
                self._draft_failures[draft_id] = (
                    self._draft_failures.get(draft_id, 0) + 1
                )
                self.logger.error(
                    "Failed to post draft %s (attempt %d): %s",
                    draft_id, self._draft_failures[draft_id], e,
                )
                if self._draft_failures[draft_id] >= self._max_failures:
                    reason = str(e).splitlines()[0][:200]
                    self.supabase.update_draft(draft_id, {
                        "status": "failed",
                        "failure_reason": reason,
                    })
                    self.approval.report_failure(draft_id, reason)
                    self._draft_failures.pop(draft_id, None)

    def _cleanup_tabs(self):
        "Close all tabs except the first and navigate back to DM inbox"
        try:
            handles = self.driver.window_handles
            if len(handles) > 1:
                for handle in handles[1:]:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                self.driver.switch_to.window(handles[0])
            if self.config["urls"]["chat"] not in self.driver.current_url:
                self.driver.get(self.config["urls"]["chat"])
        except WebDriverException:
            self.logger.error("Error cleaning up tabs, navigating to inbox.")
            try:
                self.driver.get(self.config["urls"]["chat"])
            except WebDriverException:
                pass  # driver is dead, will be caught next loop iteration
        time.sleep(self.config["x.com"]["polling_interval"])

    def load_config(self, path="config.json"):
        "Load config file"
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(CONFIG_TEMPLATE, f, indent=4)
            return CONFIG_TEMPLATE

        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)


def _now_iso():
    "UTC ISO-8601 timestamp for Supabase timestamptz columns."
    return datetime.now(timezone.utc).isoformat()


def _setup_logging(config):
    """
    Configure root logging once. Idempotent across run_forever() retries.
    Returns the Notifier (or None if Telegram is not configured) so callers
    can send direct event notifications outside the logging path.
    """
    root = logging.getLogger()
    if getattr(root, "_xdm_configured", False):
        return getattr(root, "_xdm_notifier", None)

    log_level = logging.INFO if config["logging_level"] == "INFO" else logging.DEBUG
    log_format = "%(asctime)s | %(process)d | %(name)s | %(levelname)s | %(message)s"
    log_datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=log_datefmt)

    logging.basicConfig(level=log_level, format=log_format, datefmt=log_datefmt)

    os.makedirs("logs", exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/xdm.log", maxBytes=10 * 1024 * 1024, backupCount=3,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Telegram alerts on ERROR/CRITICAL plus direct event notifications.
    # Token/chat_id from config or env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
    # Missing config = no-op (notifier is None, handler not attached).
    notifier = None
    tg_cfg = config.get("telegram", {})
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN") or tg_cfg.get("bot_token", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID") or tg_cfg.get("chat_id", "")
    if tg_token and tg_chat:
        notifier = Notifier(tg_token, tg_chat)
        tg_handler = TelegramHandler(notifier, level=logging.ERROR)
        tg_handler.setFormatter(
            logging.Formatter("[XDM %(levelname)s] %(name)s\n%(message)s")
        )
        root.addHandler(tg_handler)

    root._xdm_configured = True
    root._xdm_notifier = notifier
    return notifier


def _kill_orphaned_chrome(user_data_dir):
    "Kill any leftover browser processes from a previous crash"
    # pkill (run as non-root) only kills the bot user's own processes, so a
    # broad pattern is safe on a single-purpose VPS. "chrome" as a substring
    # also catches chromedriver and chrome_crashpad_handler.
    #
    # IMPORTANT: do NOT kill Xvfb here. Under systemd we run as a child of
    # `xvfb-run`, so the Xvfb instance providing our display is a sibling
    # process owned by the bot user. Killing it strands Chrome without a
    # display and causes a crash loop. systemd's KillMode=control-group
    # handles Xvfb cleanup across restarts at the cgroup level.
    try:
        subprocess.run(
            ["pkill", "-9", "-f", r"chrome|uc_driver"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    lock_file = os.path.join(user_data_dir, "SingletonLock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except OSError:
            pass


def run_forever():
    "Run the bot with crash recovery and exponential backoff"
    logger = logging.getLogger("RUNNER")
    backoff = 30
    max_backoff = 300

    while True:
        automation = None
        try:
            _kill_orphaned_chrome("chrome_profile")
            automation = XAutomation()
            backoff = 30  # reset on successful init
            automation.start()
            break  # clean exit (KeyboardInterrupt handled inside start)

        except KeyboardInterrupt:
            logger.warning("Keyboard interrupt, shutting down.")
            break

        except Exception:
            logger.error("Fatal error:\n%s", traceback.format_exc())
            logger.info("Restarting in %ds...", backoff)

        finally:
            if automation:
                try:
                    automation.chrome.close()
                except Exception:
                    pass

        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    run_forever()
