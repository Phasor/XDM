"""
Draft lifecycle. Owns the transitions: generate → send to Telegram → wait →
expire | approve | regenerate. The Telegram thread writes status updates
straight to Supabase for approve/reject/edit; this class handles the steps
that need the composer (create, regenerate) and the post-lifecycle edits
to the Telegram message (expire, confirm-posted).

Image-aware: if a draft has image_path, it was sent to Telegram as a photo
message, so status edits use editMessageCaption instead of editMessageText.
Regeneration always deletes the old message and sends a fresh one, since
switching between text-only and photo in place isn't supported by Telegram.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone


class ApprovalFlow:
    "Orchestrates the generate-approve-post lifecycle."

    def __init__(self, config, supabase, composer, image_gen, telegram_bot):
        self.logger = logging.getLogger("APPROVAL")
        self.supabase = supabase
        self.composer = composer
        self.image_gen = image_gen
        self.tg = telegram_bot

        tw = config["tweeting"]
        self.character_name = tw.get("character_name", "default")
        self.expiry_hours = float(tw.get("draft_expiry_hours", 4))
        self.context_recent = int(tw.get("context_recent_posts", 20))
        self.require_approval = bool(tw.get("require_approval", True))

    # =========================
    # Create / regenerate
    # =========================

    def create_new_draft(self):
        "Compose a brand-new draft and send it for approval."
        draft_id = uuid.uuid4().hex[:12]
        composed = self._compose(regen=False)
        if not composed:
            self.tg.send_message("⚠️ Composer returned no draft (LLM failure).")
            return None

        image_path = self._maybe_render_image(composed.get("image_prompt"), draft_id)
        row = self._insert_pending(draft_id, composed, image_path)
        if not row:
            return None

        self._send_for_approval(draft_id, composed["text"], image_path)
        return draft_id

    def regenerate(self, draft_id):
        "Replace an existing draft's text+image with a freshly-generated pair."
        existing = self.supabase.get_draft(draft_id)
        if not existing:
            self.logger.warning("regen target not found: %s", draft_id)
            return None

        composed = self._compose(regen=True)
        if not composed:
            self.tg.send_message(
                f"⚠️ Regeneration failed for draft {draft_id}.",
            )
            return None

        image_path = self._maybe_render_image(
            composed.get("image_prompt"), draft_id,
        )

        # Delete the old Telegram message — we can't switch between text-only
        # and photo in place, and for cleanliness we always redo the message.
        old_chat = existing.get("telegram_chat_id")
        old_mid = existing.get("telegram_message_id")
        if old_chat and old_mid:
            self.tg.delete_message(old_chat, old_mid)

        # Reset the existing row back to pending with the new content and a
        # fresh expiry window.
        expires_at = datetime.now(timezone.utc) + timedelta(hours=self.expiry_hours)
        self.supabase.update_draft(draft_id, {
            "text": composed["text"],
            "image_prompt": composed.get("image_prompt"),
            "image_path": image_path,
            "status": "pending",
            "telegram_chat_id": None,
            "telegram_message_id": None,
            "expires_at": expires_at.isoformat(),
            "scheduled_for": None,
        })

        self._send_for_approval(draft_id, composed["text"], image_path)
        return draft_id

    # =========================
    # Lifecycle ticks
    # =========================

    def expire_pending(self):
        "Mark stale pending drafts as expired and update their TG messages."
        for draft in self.supabase.get_pending_expired():
            self.supabase.update_draft(
                draft["draft_id"], {"status": "expired"},
            )
            self._edit_status(draft, "⏰ Expired (no response)")

    def confirm_posted(self, draft_id, tweet_url):
        "Edit the draft's Telegram message to show the posted tweet URL."
        draft = self.supabase.get_draft(draft_id)
        if not draft:
            return
        status_line = f"✅ Posted: {tweet_url}" if tweet_url else "✅ Posted"
        self._edit_status(draft, status_line)

    def report_failure(self, draft_id, reason):
        "Edit the draft's Telegram message to show a posting failure."
        draft = self.supabase.get_draft(draft_id)
        if not draft:
            return
        self._edit_status(draft, f"❌ Post failed: {reason}")

    # =========================
    # Internals
    # =========================

    def _compose(self, regen):
        recent = self.supabase.get_recent_posted(
            self.character_name, limit=self.context_recent,
        )
        return self.composer.compose(recent, regen=regen)

    def _maybe_render_image(self, image_prompt, draft_id):
        "Render an image if prompt present and generator configured. Never crashes."
        if not image_prompt or not self.image_gen:
            return None
        try:
            return self.image_gen.generate(image_prompt, draft_id)
        except Exception as e:
            self.logger.error("Image generation raised: %s", e)
            return None

    def _insert_pending(self, draft_id, composed, image_path):
        expires_at = datetime.now(timezone.utc) + timedelta(hours=self.expiry_hours)
        row = {
            "draft_id": draft_id,
            "character_name": self.character_name,
            "text": composed["text"],
            "image_prompt": composed.get("image_prompt"),
            "image_path": image_path,
            "status": "pending",
            "expires_at": expires_at.isoformat(),
        }
        return self.supabase.insert_draft(row)

    def _send_for_approval(self, draft_id, text, image_path):
        if not self.require_approval:
            # Skip Telegram, mark approved immediately; main loop posts next tick.
            self.supabase.update_draft(draft_id, {
                "status": "approved",
                "scheduled_for": datetime.now(timezone.utc).isoformat(),
            })
            return

        if not self.tg.enabled():
            self.logger.error(
                "Telegram not configured — can't send draft %s for approval",
                draft_id,
            )
            return

        if image_path:
            sent = self.tg.send_draft_with_image(draft_id, text, image_path)
        else:
            sent = self.tg.send_draft(draft_id, text)

        if not sent:
            self.logger.error("Failed to send draft %s to Telegram", draft_id)
            return

        self.supabase.update_draft(draft_id, {
            "telegram_chat_id": str(sent.get("chat", {}).get("id", "")),
            "telegram_message_id": str(sent.get("message_id", "")),
        })

    def _edit_status(self, draft, status_line):
        "Edit a draft's TG message with a status line prepended to its body."
        chat_id = draft.get("telegram_chat_id")
        message_id = draft.get("telegram_message_id")
        if not (chat_id and message_id):
            return
        self.tg.edit_body(
            chat_id, message_id,
            self.tg._format_status(draft["text"], status_line),
            has_image=bool(draft.get("image_path")),
        )
