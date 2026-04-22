"""
Telegram receive-side bot: long-polls getUpdates in a background thread and
dispatches callback_query clicks, /compose commands, and reply-based edits
to the draft lifecycle.

Driver-free by design: this thread never touches the Selenium driver. It
reads/writes Supabase and makes Telegram HTTP calls. Heavy work (compose
new draft, post to X.com) is deferred to the main loop via the shared
compose_queue.

Authorization is a hard chat_id check — any update from a different chat
is logged and ignored.
"""

import logging
import os
import threading
import time

import requests


class TelegramBot:
    "Long-polling Telegram bot for draft approvals."

    API_BASE = "https://api.telegram.org/bot{token}"
    LONG_POLL_TIMEOUT = 30
    ERROR_BACKOFF = 5

    def __init__(self, config, supabase, compose_queue):
        self.logger = logging.getLogger("TG-BOT")
        self.supabase = supabase
        self.compose_queue = compose_queue

        tg_cfg = config.get("telegram", {})
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or tg_cfg.get("bot_token", "")
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID") or tg_cfg.get("chat_id", ""))
        self.http_timeout = self.LONG_POLL_TIMEOUT + 10

        self._offset = None
        self._started_at = int(time.time())
        self._shutdown = threading.Event()
        self._thread = None

    def enabled(self):
        return bool(self.bot_token and self.chat_id)

    def start(self):
        if not self.enabled():
            self.logger.warning("Telegram bot disabled: missing token or chat_id")
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="telegram-bot",
        )
        self._thread.start()
        self.logger.info("Telegram bot started")

    def stop(self):
        self._shutdown.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.LONG_POLL_TIMEOUT + 5)
        self._thread = None

    # =========================
    # Polling loop
    # =========================

    def _run(self):
        while not self._shutdown.is_set():
            try:
                updates = self._get_updates()
                for u in updates:
                    if self._shutdown.is_set():
                        break
                    self._handle_update(u)
            except requests.exceptions.Timeout:
                continue  # expected on idle long-poll
            except Exception as e:
                self.logger.error("Polling error: %s", e)
                self._shutdown.wait(self.ERROR_BACKOFF)

    def _get_updates(self):
        params = {
            "timeout": self.LONG_POLL_TIMEOUT,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset

        resp = requests.get(
            self._url("getUpdates"),
            params=params,
            timeout=self.http_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            self.logger.error("getUpdates not ok: %s", data)
            return []

        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # =========================
    # Dispatch
    # =========================

    def _handle_update(self, update):
        # Skip anything queued before the bot started (best-effort dedup
        # across restarts without persistent offset storage).
        msg = update.get("message") or update.get("callback_query", {}).get("message")
        msg_date = msg.get("date") if msg else None
        if msg_date and msg_date < self._started_at:
            return

        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        if "message" in update:
            self._handle_message(update["message"])

    def _handle_callback(self, cq):
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != self.chat_id:
            self.logger.warning("Ignoring callback from chat %s", chat_id)
            self.answer_callback_query(cq["id"])
            return

        data = cq.get("data", "")
        try:
            action, draft_id = data.split(":", 1)
        except ValueError:
            self.answer_callback_query(cq["id"], text="Unknown action")
            return

        message_id = cq.get("message", {}).get("message_id")
        if action == "approve":
            self._approve(draft_id, chat_id, message_id, cq["id"])
        elif action == "reject":
            self._reject(draft_id, chat_id, message_id, cq["id"])
        elif action == "regen":
            self._regen(draft_id, chat_id, message_id, cq["id"])
        else:
            self.answer_callback_query(cq["id"], text="Unknown action")

    def _handle_message(self, msg):
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self.chat_id:
            self.logger.warning("Ignoring message from chat %s", chat_id)
            return

        text = (msg.get("text") or "").strip()

        # /compose command — queue a fresh draft for the main loop.
        if text == "/compose":
            self.compose_queue.put({"type": "new"})
            self.send_message("📝 Queued a new draft…")
            return

        # Reply to an existing draft message → treat as edit.
        reply_to = msg.get("reply_to_message")
        if reply_to and text:
            self._apply_edit(chat_id, reply_to.get("message_id"), text)

    # =========================
    # Action handlers
    # =========================

    def _approve(self, draft_id, chat_id, message_id, cq_id):
        draft = self.supabase.get_draft(draft_id)
        if not draft:
            self.answer_callback_query(cq_id, text="Draft not found")
            return
        if draft["status"] != "pending":
            self.answer_callback_query(
                cq_id, text=f"Already {draft['status']}",
            )
            return

        from datetime import datetime, timezone
        self.supabase.update_draft(draft_id, {
            "status": "approved",
            "scheduled_for": datetime.now(timezone.utc).isoformat(),
        })
        self.answer_callback_query(cq_id, text="✅ Approved")
        self.edit_message_text(
            chat_id, message_id,
            self._format_status(draft["text"], "✅ Approved, posting soon"),
        )

    def _reject(self, draft_id, chat_id, message_id, cq_id):
        draft = self.supabase.get_draft(draft_id)
        if not draft:
            self.answer_callback_query(cq_id, text="Draft not found")
            return
        self.supabase.update_draft(draft_id, {"status": "rejected"})
        self.answer_callback_query(cq_id, text="❌ Rejected")
        self.edit_message_text(
            chat_id, message_id,
            self._format_status(draft["text"], "❌ Rejected"),
        )

    def _regen(self, draft_id, chat_id, message_id, cq_id):
        draft = self.supabase.get_draft(draft_id)
        if not draft:
            self.answer_callback_query(cq_id, text="Draft not found")
            return
        # Main loop picks this up, generates a replacement, updates the same
        # DB row, and edits this TG message in place.
        self.compose_queue.put({
            "type": "regen",
            "replace_draft_id": draft_id,
        })
        self.answer_callback_query(cq_id, text="🔄 Regenerating…")
        self.edit_message_text(
            chat_id, message_id,
            self._format_status(draft["text"], "🔄 Regenerating…"),
        )

    def _apply_edit(self, chat_id, replied_msg_id, new_text):
        draft = self.supabase.get_draft_by_telegram(chat_id, replied_msg_id)
        if not draft:
            return  # reply wasn't to a draft we know about
        if draft["status"] != "pending":
            self.send_message(
                f"⚠️ Can't edit — draft is {draft['status']}.",
            )
            return
        self.supabase.update_draft(draft["draft_id"], {"text": new_text})
        self.edit_body(
            chat_id, replied_msg_id,
            self._format_draft_body(new_text),
            has_image=bool(draft.get("image_path")),
            reply_markup=self._draft_keyboard(draft["draft_id"]),
        )

    # =========================
    # Presentation helpers
    # =========================

    @staticmethod
    def _draft_keyboard(draft_id):
        return {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{draft_id}"},
                {"text": "🔄 Regen", "callback_data": f"regen:{draft_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{draft_id}"},
            ]],
        }

    @staticmethod
    def _format_draft_body(text):
        return f"📝 Draft\n\n{text}\n\n(Reply with text to edit.)"

    @staticmethod
    def _format_status(text, status_line):
        return f"{status_line}\n\n{text}"

    # =========================
    # Telegram API
    # =========================

    def _url(self, method):
        return self.API_BASE.format(token=self.bot_token) + "/" + method

    def send_message(self, text, reply_markup=None):
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(self._url("sendMessage"), json=payload, timeout=15)
            resp.raise_for_status()
            return resp.json().get("result")
        except requests.exceptions.RequestException as e:
            self.logger.error("sendMessage failed: %s", e)
            return None

    def send_draft(self, draft_id, text):
        "Send a pending draft with the standard approval keyboard."
        return self.send_message(
            self._format_draft_body(text),
            reply_markup=self._draft_keyboard(draft_id),
        )

    def send_draft_with_image(self, draft_id, text, image_path):
        "Send a pending draft as a photo message with the approval keyboard."
        caption = self._format_draft_body(text)
        return self.send_photo(
            image_path,
            caption=caption,
            reply_markup=self._draft_keyboard(draft_id),
        )

    def send_photo(self, image_path, caption=None, reply_markup=None):
        "Upload a local image as a photo message. Returns the message dict."
        try:
            with open(image_path, "rb") as f:
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption
                if reply_markup is not None:
                    import json as _json
                    data["reply_markup"] = _json.dumps(reply_markup)
                resp = requests.post(
                    self._url("sendPhoto"),
                    data=data,
                    files={"photo": f},
                    timeout=60,
                )
                resp.raise_for_status()
                return resp.json().get("result")
        except (requests.exceptions.RequestException, OSError) as e:
            self.logger.error("sendPhoto failed: %s", e)
            return None

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(
                self._url("editMessageText"), json=payload, timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("result")
        except requests.exceptions.RequestException as e:
            # Common: "message is not modified" when edits are idempotent.
            # Silent to avoid noise; Telegram treats it as a no-op.
            self.logger.debug("editMessageText: %s", e)
            return None

    def edit_message_caption(self, chat_id, message_id, caption, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(
                self._url("editMessageCaption"), json=payload, timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("result")
        except requests.exceptions.RequestException as e:
            self.logger.debug("editMessageCaption: %s", e)
            return None

    def edit_body(self, chat_id, message_id, body, has_image, reply_markup=None):
        "Edit a draft message's body, picking caption or text API by content type."
        if has_image:
            return self.edit_message_caption(
                chat_id, message_id, body, reply_markup=reply_markup,
            )
        return self.edit_message_text(
            chat_id, message_id, body, reply_markup=reply_markup,
        )

    def delete_message(self, chat_id, message_id):
        try:
            resp = requests.post(
                self._url("deleteMessage"),
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            self.logger.debug("deleteMessage failed: %s", e)
            return False

    def answer_callback_query(self, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            resp = requests.post(
                self._url("answerCallbackQuery"), json=payload, timeout=10,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            self.logger.debug("answerCallbackQuery failed: %s", e)
