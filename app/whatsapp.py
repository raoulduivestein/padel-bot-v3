from __future__ import annotations

import re
import shutil
import threading
import time
from typing import Any
from urllib.parse import quote

from app.config import ROOT


class WhatsAppError(RuntimeError):
    pass


class WhatsAppManager:
    def __init__(self) -> None:
        self.profile_dir = ROOT / "state" / "whatsapp-selenium-profile"
        self._lock = threading.RLock()
        self._driver: Any | None = None

    def status(self) -> dict:
        with self._lock:
            driver = self._ensure_driver()
            self._open_whatsapp(driver)
            time.sleep(1)
            return self._status(driver)

    def qr_screenshot(self) -> bytes:
        with self._lock:
            driver = self._ensure_driver()
            self._open_whatsapp(driver)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._is_logged_in(driver):
                    raise WhatsAppError("WhatsApp is already linked. Use the open Chrome window to send messages.")
                qr = self._find_qr_canvas(driver)
                if qr is not None:
                    return qr.screenshot_as_png
                time.sleep(0.3)
            raise WhatsAppError("No QR code is visible. Check the opened Chrome window.")

    def send_message(self, *, phone: str, message: str) -> dict:
        normalized_phone = self._normalize_phone(phone)
        if not normalized_phone:
            raise WhatsAppError("Phone number is required")
        if not message.strip():
            raise WhatsAppError("Message is required")

        with self._lock:
            driver = self._ensure_driver()
            url = f"https://web.whatsapp.com/send?phone={normalized_phone}&text={quote(message)}"
            self._safe_get(driver, url)
            box = self._wait_for_message_box(driver, timeout=25)
            if box is None:
                if self._find_qr_canvas(driver) is not None:
                    raise WhatsAppError("WhatsApp is not linked. Scan the QR code in the opened Chrome window.")
                raise WhatsAppError(f"WhatsApp did not open a chat for phone {normalized_phone}.")

            box.click()
            time.sleep(0.4)
            box.send_keys(self._keys().ENTER)
            time.sleep(1.5)
            if self._message_box_has_text(driver):
                self._click_send_button(driver)
                time.sleep(1.5)
            if self._message_box_has_text(driver):
                raise WhatsAppError("WhatsApp message is still a draft; send button was not activated.")
            return {"ok": True, "phone": normalized_phone, "sent": True, "url": driver.current_url}

    def debug(self) -> dict:
        with self._lock:
            driver = self._ensure_driver()
            self._open_whatsapp(driver)
            body = ""
            try:
                body = driver.find_element(self._by().TAG_NAME, "body").text
            except Exception:
                pass
            status = self._status(driver)
            status["body_excerpt"] = body[:1000]
            return status

    def reload(self) -> dict:
        with self._lock:
            driver = self._ensure_driver()
            self._open_whatsapp(driver)
            driver.refresh()
            time.sleep(2)
            return self._status(driver)

    def _ensure_driver(self):
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self._driver = None

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise WhatsAppError("Selenium is not installed. Run: pip install -r requirements.txt") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.add_argument(f"--user-data-dir={self.profile_dir}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--remote-debugging-port=0")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        chrome_binary = shutil.which("google-chrome") or shutil.which("google-chrome-stable") or shutil.which("chromium")
        if chrome_binary:
            options.binary_location = chrome_binary

        try:
            self._driver = webdriver.Chrome(options=options)
        except Exception as exc:
            raise WhatsAppError(
                f"Could not start Chrome with Selenium. Chrome binary: {chrome_binary or 'not found'}. Error: {exc}"
            ) from exc
        self._driver.set_page_load_timeout(20)
        return self._driver

    def _open_whatsapp(self, driver) -> None:
        if "web.whatsapp.com" not in (driver.current_url or ""):
            self._safe_get(driver, "https://web.whatsapp.com")

    @staticmethod
    def _safe_get(driver, url: str) -> None:
        try:
            driver.get(url)
        except Exception as exc:
            message = str(exc).lower()
            if "timeout" in message or "timed out" in message:
                return
            raise WhatsAppError(f"Chrome could not open WhatsApp Web: {exc}") from exc

    def _status(self, driver) -> dict:
        return {
            "ok": True,
            "driver": "selenium",
            "logged_in": self._is_logged_in(driver),
            "needs_qr": self._find_qr_canvas(driver) is not None and not self._is_logged_in(driver),
            "loading_chats": self._is_loading_chats(driver),
            "browser_unsupported": self._is_browser_unsupported(driver),
            "url": driver.current_url,
            "title": driver.title,
        }

    def _wait_for_message_box(self, driver, *, timeout: int):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            box = self._message_box(driver)
            if box is not None:
                return box
            if self._find_qr_canvas(driver) is not None:
                return None
            time.sleep(0.4)
        return None

    def _message_box(self, driver):
        by = self._by()
        selectors = [
            "footer div[contenteditable='true'][role='textbox']",
            "footer div[contenteditable='true'][data-tab]",
            "div[contenteditable='true'][data-tab='10']",
        ]
        for selector in selectors:
            try:
                elements = driver.find_elements(by.CSS_SELECTOR, selector)
                visible = [element for element in elements if element.is_displayed()]
                if visible:
                    return visible[-1]
            except Exception:
                continue
        return None

    def _message_box_has_text(self, driver) -> bool:
        box = self._message_box(driver)
        if box is None:
            return False
        try:
            return bool((box.text or "").strip())
        except Exception:
            return False

    def _click_send_button(self, driver) -> None:
        by = self._by()
        selectors = [
            "footer button[aria-label='Send']",
            "footer button[aria-label='Verzenden']",
            "footer [data-icon='send']",
            "footer [data-icon*='send']",
            "footer [data-testid='send']",
        ]
        for selector in selectors:
            try:
                elements = driver.find_elements(by.CSS_SELECTOR, selector)
                for element in reversed(elements):
                    if element.is_displayed():
                        driver.execute_script(
                            "const el = arguments[0]; (el.closest('button') || el.closest('[role=button]') || el).click();",
                            element,
                        )
                        return
            except Exception:
                continue
        footer = driver.find_element(by.CSS_SELECTOR, "footer")
        driver.execute_script(
            """
            const footer = arguments[0];
            const rect = footer.getBoundingClientRect();
            const target = document.elementFromPoint(rect.right - 35, rect.top + rect.height / 2);
            if (!target) return;
            (target.closest('button') || target.closest('[role=button]') || target).click();
            """,
            footer,
        )

    def _find_qr_canvas(self, driver):
        by = self._by()
        try:
            canvases = driver.find_elements(by.CSS_SELECTOR, "canvas")
            visible = [canvas for canvas in canvases if canvas.is_displayed()]
            return visible[0] if visible else None
        except Exception:
            return None

    def _is_logged_in(self, driver) -> bool:
        by = self._by()
        selectors = [
            "#side",
            "#pane-side",
            "[data-testid='chat-list']",
            "div[aria-label='Chat list']",
            "footer div[contenteditable='true']",
        ]
        for selector in selectors:
            try:
                if any(element.is_displayed() for element in driver.find_elements(by.CSS_SELECTOR, selector)):
                    return True
            except Exception:
                continue
        return False

    def _is_loading_chats(self, driver) -> bool:
        text = self._body_text(driver).lower()
        return any(
            marker in text
            for marker in [
                "je chats worden geladen",
                "your chats are loading",
                "berichten worden gedownload",
                "messages are being downloaded",
            ]
        )

    def _is_browser_unsupported(self, driver) -> bool:
        text = self._body_text(driver).lower()
        return "whatsapp works with google chrome" in text or "update google chrome" in text

    def _body_text(self, driver) -> str:
        try:
            return driver.find_element(self._by().TAG_NAME, "body").text
        except Exception:
            return ""

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("00"):
            digits = digits[2:]
        if digits.startswith("0"):
            digits = f"31{digits[1:]}"
        return digits

    @staticmethod
    def _by():
        from selenium.webdriver.common.by import By

        return By

    @staticmethod
    def _keys():
        from selenium.webdriver.common.keys import Keys

        return Keys


whatsapp_manager = WhatsAppManager()
