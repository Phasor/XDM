import json
import logging
import os
import time
import math
import random
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


# Set to True to save cookies after login, which can speed up subsequent logins
SAVE_COOKIES = False


class Login:
    "Class to interactions with X.com"

    def __init__(self, driver, config):
        self.username = os.getenv("X_USERNAME") or config["x.com"]["username"]
        self.password = os.getenv("X_PASSWORD") or config["x.com"]["password"]
        self.logger = logging.getLogger("LOGIN")
        self.config = config
        self.driver = driver
        self.human_like = HumanLikeMovement()

    def login(self):
        "Login to X.com with provided credentials, using cookies if available"
        if not self.login_required():
            self.save_cookies()
            self.logger.info("Cookies saved for account: @%s", self.username)
            return

        self.logger.info("Account is not logged in, Attempting login.")

        if SAVE_COOKIES and os.path.exists(f"cookies/{self.username}.json"):
            self.logger.info(
                "Cookies file already exist for account @%s, Restoring session.",
                self.username,
            )
            self.load_cookies()

            if not self.login_required():
                self.logger.info("Session restored successfully.")
                return
            self.logger.warning(
                "Couldn't restore session from cookies file, Attempting new login."
            )

        self.enter_username()
        self.logger.info("Entered username: %s", self.username)

        self.enter_password()
        self.logger.info("Entered password: %s", "*" * len(self.password))

        time.sleep(2)
        if not self.login_required():
            self.save_cookies()
            self.logger.info("Cookies saved for account: @%s", self.username)

    def login_required(self):
        "Check if account is login or not"
        try:
            wait = WebDriverWait(self.driver, 15)
            account_locator = (
                By.CSS_SELECTOR,
                "button[data-testid='SideNav_AccountSwitcher_Button']",
            )
            login_locator = (By.CSS_SELECTOR, "a[data-testid='loginButton']")

            element = wait.until(
                EC.any_of(
                    EC.presence_of_element_located(account_locator),
                    EC.presence_of_element_located(login_locator),
                )
            )

            # Only click if it's the <a> login button
            if element.tag_name == "a":
                wait.until(EC.element_to_be_clickable(login_locator)).click()
                return True

            return False

        except TimeoutException as e:
            raise TimeoutException("Couldn't verify login due to timeout.") from e

    def enter_username(self):
        "Enter username into the input field"
        try:
            input_elem = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='text']"))
            )
            self.human_like.move_and_click(self.driver, input_elem)
            input_elem.click()

            for char in self.username:
                for key in ["keyDown", "keyUp"]:
                    self.driver.execute_cdp_cmd(
                        "Input.dispatchKeyEvent",
                        {
                            "type": key,
                            "text": char,
                        },
                    )
                time.sleep(0.01)

            self.driver.find_element(
                By.XPATH, "//button[.//span[text()='Next']]"
            ).click()

        except TimeoutException as e:
            raise TimeoutException("Couldn't verify login due to timeout.") from e

    def enter_password(self):
        "Enter password into the input field"
        wait = WebDriverWait(self.driver, 15)
        wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='password']"))
        ).click()
        # time.sleep(3)

        for char in self.password:
            for key in ["keyDown", "keyUp"]:
                self.driver.execute_cdp_cmd(
                    "Input.dispatchKeyEvent",
                    {
                        "type": key,
                        "text": char,
                    },
                )

        self.driver.find_element(
            By.CSS_SELECTOR, "button[data-testid='LoginForm_Login_Button']"
        ).click()

    def load_cookies(self):
        "Load cookies from json file"
        with open(f"cookies/{self.username}.json", "r", encoding="utf-8") as f:
            cookies = json.load(f)

        # remove auth token item from cookies
        cookies = [item for item in cookies if not "token" in item]

        for cookie in cookies:
            self.driver.add_cookie(cookie)

        current_tab = self.driver.current_window_handle

        base_url = self.config["urls"]["base"]
        self.driver.execute_script(f"window.open('{base_url}', '_blank');")

        # Switch to new tab
        new_tab = [tab for tab in self.driver.window_handles if tab != current_tab][0]
        self.driver.switch_to.window(new_tab)

        # Close old tab
        self.driver.switch_to.window(current_tab)
        self.driver.close()

        # Focus on new tab
        self.driver.switch_to.window(new_tab)

    def save_cookies(self):
        "Save cookies in a json file"
        if not SAVE_COOKIES:
            self.logger.info("SAVE_COOKIES is set to False, skipping cookie saving.")
            return

        cookies = self.driver.get_cookies()
        if not os.path.exists("cookies"):
            os.makedirs("cookies")

        with open(f"cookies/{self.username}.json", "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=4)


class HumanLikeMovement:
    "Move move like a human"

    def bezier_curve(self, p0: float, p1: float, p2: float, t: float) -> float:
        """
        Compute a point on a quadratic Bézier curve.

        :return: Interpolated point
        """
        return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t**2 * p2

    def generate_human_like_path(
        self, start_x: int, start_y: int, end_x: int, end_y: int
    ) -> list:
        """
        Generate a smooth path from start to end using Bézier curve interpolation.

        :return: List of (x, y) points
        """
        dx, dy = end_x - start_x, end_y - start_y
        distance = math.hypot(dx, dy)
        steps = min(50, max(10, int(distance / 5)))
        ctrl_offset = distance * 0.25

        ctrl_x = (start_x + end_x) / 2 + random.uniform(-ctrl_offset, ctrl_offset)
        ctrl_y = (start_y + end_y) / 2 + random.uniform(-ctrl_offset, ctrl_offset)

        path = []
        for i in range(steps + 1):
            t = i / steps
            x = self.bezier_curve(start_x, ctrl_x, end_x, t) + random.uniform(-0.2, 0.2)
            y = self.bezier_curve(start_y, ctrl_y, end_y, t) + random.uniform(-0.2, 0.2)
            path.append((int(x), int(y)))

        return path

    def move_and_click(self, driver, element):
        "Move mouse and click"
        window_width = driver.execute_script("return window.innerWidth")
        window_height = driver.execute_script("return window.innerHeight")

        start_x = random.randint(0, window_width - 1)
        start_y = random.randint(0, window_height - 1)

        rect = element.rect
        end_x = rect["x"] + rect["width"] / 2
        end_y = rect["y"] + rect["height"] / 2

        path = self.generate_human_like_path(start_x, start_y, end_x, end_y)

        for x, y in path:
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y, "buttons": 1},
            )

        # for event_type in ["mousePressed", "mouseReleased"]:
        #     driver.execute_cdp_cmd(
        #         "Input.dispatchMouseEvent",
        #         {
        #             "type": event_type,
        #             "x": end_x,
        #             "y": end_y,
        #             "button": "left",
        #             "clickCount": 1,
        #         },
        #     )
