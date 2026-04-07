import json
import logging
import os
import time
from selenium.common import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from seleniumbase import Driver

from llm.llm_client import LLM
from storage.supabase_client import SupaBase
from x_automation.dm_manager import DmListener, OpenChat
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
        "context_message_limit": 20,
    },
    "openrouter": {
        "api_key": "",
        "model": "openai/gpt-3.5-turbo",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "timeout": 30,
        "personality_file": "prompts/personality.txt",
    },
    "urls": {"base": "https://x.com/", "chat": "https://x.com/i/chat"},
}


class Chrome:
    "A class to manage chrome driver"

    def __init__(self, logger, config):
        self.logger = logger
        self.config = config
        self.driver_instance = None

    def driver(self):
        "Initialize Chrome driver"
        self.driver_instance = Driver(
            uc=self.config["chrome"]["undetected_chromedirver"],
            user_data_dir=os.path.abspath(self.config["chrome"]["user_data_dir"]),
            proxy=self.config["chrome"]["proxy"] or None,
            chromium_arg="--disable-application-cache,--disk-cache-size=0,--disable-cache",
        )

        self.driver_instance.set_window_position(
            *self.config["chrome"]["window_position"]
        )
        self.driver_instance.set_window_size(*self.config["chrome"]["width_height"])
        self.logger.info("Chrome driver initialized successfully")
        return self.driver_instance

    def close(self):
        "Close running chrome instance"
        if self.driver_instance:
            self.driver_instance.quit()
            self.logger.info("Chrome driver closed successfully")
        else:
            self.logger.warning("No running chrome driver found to close")


class XAutomation:
    "Main class for X automation tasks"

    def __init__(self):
        self.config = self.load_config()

        logging.basicConfig(
            level=(
                logging.INFO
                if self.config["logging_level"] == "INFO"
                else logging.DEBUG
            ),
            # level=logging.INFO,
            format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )

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

    def start(self):
        "Start the automation process"
        try:
            self.driver.get(self.config["urls"]["base"])

            login_manager = Login(self.driver, self.config)
            login_manager.login()

            self.driver.get(self.config["urls"]["chat"])
            self.logger.debug("Navigated to: %s", self.config["urls"]["chat"])

            # input("Press enter to continue")
            self.enter_passcode()

            self.main_loop()

        except KeyboardInterrupt:
            self.logger.warning("Keyboard interrupt received, exiting...")

        finally:
            self.driver.close()

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
            new_message = self.listener.detect_new_message()
            if not new_message:
                time.sleep(self.config["x.com"]["polling_interval"])
                continue

            conv_id, change_type, data = new_message
            self.logger.info("[%s] %s → %s", change_type, conv_id, data["last_message"])
            self.supabase.upsert_conversation(conv_id, data["username"])

            original_tab = self.driver.window_handles[0]
            url = "https://x.com/i/chat/" + conv_id
            self.driver.execute_script(f"window.open('{url}', '_blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])

            try:
                if self.enter_passcode():
                    # website will redirect to chat page after passcode.
                    # so we need open the DM page again
                    self.driver.get(url)

                latest_msg_id = self.supabase.get_latest_message_id(conv_id)

                new_chat = self.opened_chat.read_messages(latest_msg_id)
                if not new_chat:
                    continue

                chat_history = self.supabase.get_messages(conv_id)

                llm_response = self.llm.get_response(new_chat, chat_history)
                self.opened_chat.send_message(llm_response)

                self.update_supabase(conv_id, new_chat, llm_response)
                self.listener.commit(conv_id)

            finally:
                time.sleep(2)
                self.driver.close()
                self.driver.switch_to.window(original_tab)
                time.sleep(self.config["x.com"]["polling_interval"])

    def update_supabase(self, conv_id, new_chat, llm_response):
        "update supabase with assistant response"
        for msg in new_chat:
            self.supabase.save_message(
                conv_id, msg["message_id"], msg["author"], msg["text"]
            )
            time.sleep(0.5)  # slight delay to ensure order

        last_msg_id = self.opened_chat.get_last_message_id()
        self.supabase.save_message(conv_id, last_msg_id, "assistant", llm_response)
        self.supabase.update_last_message_time(conv_id)

    def load_config(self, path="config.json"):
        "Load config file"
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(CONFIG_TEMPLATE, f, indent=4)
            return CONFIG_TEMPLATE

        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)


if __name__ == "__main__":
    automation = XAutomation()
    automation.start()
