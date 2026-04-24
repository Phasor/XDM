"""Unit tests for ApprovalFlow state transitions — create, regenerate,
expire, confirm-posted, and image-vs-text edit routing."""

from datetime import datetime, timezone

import pytest

from tweeting.approval import ApprovalFlow


# =========================
# Fakes
# =========================

class FakeSupabase:
    "In-memory drafts keyed by draft_id."

    def __init__(self, pending_expired=None, recent_posted=None):
        self.rows = {}
        self._pending_expired = pending_expired or []
        self._recent_posted = recent_posted or []
        self.inserts = []
        self.updates = []  # list of (draft_id, fields)

    def insert_draft(self, row):
        self.rows[row["draft_id"]] = dict(row)
        self.inserts.append(dict(row))
        return dict(row)

    def update_draft(self, draft_id, fields):
        self.updates.append((draft_id, dict(fields)))
        if draft_id in self.rows:
            self.rows[draft_id].update(fields)
        return self.rows.get(draft_id)

    def get_draft(self, draft_id):
        return self.rows.get(draft_id)

    def get_pending_expired(self):
        return list(self._pending_expired)

    def get_recent_posted(self, character_name, limit=20):
        return list(self._recent_posted)


class FakeComposer:
    "Returns pre-programmed compose results per call (normal / regen / prompt)."

    def __init__(self, results=None, prompt_results=None):
        # list of dict-or-None, consumed FIFO
        self.results = list(results or [])
        self.prompt_results = list(prompt_results or [])
        self.calls = []
        self.prompt_calls = []

    def compose(self, recent_posts, regen=False):
        self.calls.append({"recent": recent_posts, "regen": regen})
        if not self.results:
            return None
        return self.results.pop(0)

    def compose_for_prompt(self, image_prompt, recent_posts):
        self.prompt_calls.append({
            "image_prompt": image_prompt, "recent": recent_posts,
        })
        if not self.prompt_results:
            return None
        parsed = self.prompt_results.pop(0)
        if parsed is not None:
            parsed = dict(parsed)
            parsed["image_prompt"] = image_prompt  # mirror real composer
        return parsed


class FakeImageGen:
    def __init__(self, path=None, enabled=True):
        self._path = path
        self._enabled = enabled
        self.calls = []

    def enabled(self):
        return self._enabled

    def generate(self, prompt, draft_id):
        self.calls.append((prompt, draft_id))
        return self._path


class FakeTelegramBot:
    "Records every call. Returns a plausible Telegram response from sends."

    def __init__(self, enabled=True):
        self._enabled = enabled
        self.sent_messages = []
        self.sent_drafts = []
        self.sent_photo_drafts = []
        self.edit_body_calls = []
        self.edit_text_calls = []
        self.edit_caption_calls = []
        self.deleted = []
        self._next_message_id = 100

    def enabled(self):
        return self._enabled

    def send_message(self, text, reply_markup=None):
        self.sent_messages.append({"text": text, "reply_markup": reply_markup})
        return self._make_sent()

    def send_draft(self, draft_id, text):
        self.sent_drafts.append({"draft_id": draft_id, "text": text})
        return self._make_sent()

    def send_draft_with_image(self, draft_id, text, image_path):
        self.sent_photo_drafts.append({
            "draft_id": draft_id, "text": text, "image_path": image_path,
        })
        return self._make_sent()

    def edit_body(self, chat_id, message_id, body, has_image, reply_markup=None):
        self.edit_body_calls.append({
            "chat_id": chat_id, "message_id": message_id,
            "body": body, "has_image": has_image,
            "reply_markup": reply_markup,
        })

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edit_text_calls.append({
            "chat_id": chat_id, "message_id": message_id, "text": text,
        })

    def edit_message_caption(self, chat_id, message_id, caption, reply_markup=None):
        self.edit_caption_calls.append({
            "chat_id": chat_id, "message_id": message_id, "caption": caption,
        })

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    # Format helpers mirror real TelegramBot (used by ApprovalFlow)
    @staticmethod
    def _format_draft_body(text):
        return f"📝 Draft\n\n{text}"

    @staticmethod
    def _format_status(text, status_line):
        return f"{status_line}\n\n{text}"

    @staticmethod
    def _draft_keyboard(draft_id):
        return {"kb": draft_id}

    def _make_sent(self):
        mid = self._next_message_id
        self._next_message_id += 1
        return {"chat": {"id": 999}, "message_id": mid}


# =========================
# Fixtures
# =========================

def _config(require_approval=True, character="alice"):
    return {"tweeting": {
        "character_name": character,
        "draft_expiry_hours": 4,
        "context_recent_posts": 20,
        "require_approval": require_approval,
    }}


def make_flow(
    compose_results=None,
    prompt_results=None,
    image_path=None,
    image_enabled=True,
    require_approval=True,
    tg_enabled=True,
    pending_expired=None,
    recent_posted=None,
):
    sb = FakeSupabase(
        pending_expired=pending_expired, recent_posted=recent_posted,
    )
    composer = FakeComposer(results=compose_results, prompt_results=prompt_results)
    image_gen = FakeImageGen(path=image_path, enabled=image_enabled)
    tg = FakeTelegramBot(enabled=tg_enabled)
    flow = ApprovalFlow(_config(require_approval), sb, composer, image_gen, tg)
    return flow, sb, composer, image_gen, tg


# =========================
# create_new_draft
# =========================

def test_create_new_draft_text_only_happy_path():
    flow, sb, composer, _img, tg = make_flow(
        compose_results=[{"text": "hello", "image_prompt": None}],
    )
    draft_id = flow.create_new_draft()

    assert draft_id is not None
    assert sb.rows[draft_id]["text"] == "hello"
    assert sb.rows[draft_id]["image_prompt"] is None
    assert sb.rows[draft_id]["image_path"] is None
    assert sb.rows[draft_id]["status"] == "pending"
    assert sb.rows[draft_id]["character_name"] == "alice"

    assert len(tg.sent_drafts) == 1
    assert tg.sent_drafts[0]["draft_id"] == draft_id
    assert tg.sent_drafts[0]["text"] == "hello"
    assert tg.sent_photo_drafts == []

    # telegram_chat_id/message_id recorded on the row after send
    assert sb.rows[draft_id]["telegram_chat_id"] == "999"
    assert sb.rows[draft_id]["telegram_message_id"] == "100"


def test_create_new_draft_with_image_sends_photo():
    flow, sb, _comp, image_gen, tg = make_flow(
        compose_results=[{"text": "look at this", "image_prompt": "red cat"}],
        image_path="/tmp/fake.png",
    )
    draft_id = flow.create_new_draft()

    assert image_gen.calls == [("red cat", draft_id)]
    assert sb.rows[draft_id]["image_path"] == "/tmp/fake.png"
    assert len(tg.sent_photo_drafts) == 1
    assert tg.sent_photo_drafts[0]["image_path"] == "/tmp/fake.png"
    assert tg.sent_drafts == []


def test_create_new_draft_image_gen_fails_falls_back_to_text():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[{"text": "oh well", "image_prompt": "something"}],
        image_path=None,  # image_gen returns None
    )
    draft_id = flow.create_new_draft()

    assert sb.rows[draft_id]["image_prompt"] == "something"
    assert sb.rows[draft_id]["image_path"] is None
    assert len(tg.sent_drafts) == 1
    assert tg.sent_photo_drafts == []


def test_create_new_draft_composer_fails_sends_warning():
    flow, sb, _comp, _img, tg = make_flow(compose_results=[None])
    result = flow.create_new_draft()

    assert result is None
    assert sb.rows == {}
    assert len(tg.sent_messages) == 1
    assert "⚠️" in tg.sent_messages[0]["text"]


def test_create_new_draft_passes_recent_posts_to_composer():
    recent = [{"text": "prior post 1"}, {"text": "prior post 2"}]
    flow, _sb, composer, _img, _tg = make_flow(
        compose_results=[{"text": "hi", "image_prompt": None}],
        recent_posted=recent,
    )
    flow.create_new_draft()

    assert composer.calls[0]["recent"] == recent
    assert composer.calls[0]["regen"] is False


# =========================
# require_approval = False
# =========================

def test_require_approval_false_auto_approves_without_tg():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[{"text": "auto-approved", "image_prompt": None}],
        require_approval=False,
    )
    draft_id = flow.create_new_draft()

    assert sb.rows[draft_id]["status"] == "approved"
    assert sb.rows[draft_id]["scheduled_for"] is not None
    assert tg.sent_drafts == []
    assert tg.sent_photo_drafts == []


# =========================
# regenerate
# =========================

def test_regenerate_deletes_old_message_and_resends():
    flow, sb, composer, _img, tg = make_flow(
        compose_results=[
            {"text": "first draft", "image_prompt": None},   # initial
            {"text": "replacement", "image_prompt": None},   # regen
        ],
    )
    draft_id = flow.create_new_draft()
    original_tg_msg = sb.rows[draft_id]["telegram_message_id"]

    flow.regenerate(draft_id)

    # old TG message deleted
    assert tg.deleted == [("999", original_tg_msg)]
    # compose called with regen=True the second time
    assert composer.calls[-1]["regen"] is True
    # row reset back to pending with new text
    assert sb.rows[draft_id]["text"] == "replacement"
    assert sb.rows[draft_id]["status"] == "pending"
    # new TG message sent (two sent in total: create + regen)
    assert len(tg.sent_drafts) == 2


def test_regenerate_unknown_draft_is_noop():
    flow, sb, _comp, _img, tg = make_flow()
    result = flow.regenerate("does-not-exist")
    assert result is None
    assert sb.rows == {}
    assert tg.deleted == []


def test_regenerate_composer_fails_sends_warning():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[
            {"text": "original", "image_prompt": None},
            None,  # regen compose fails
        ],
    )
    draft_id = flow.create_new_draft()
    flow.regenerate(draft_id)

    # Warning sent; original draft untouched
    assert any("⚠️" in m["text"] for m in tg.sent_messages)
    assert sb.rows[draft_id]["text"] == "original"


# =========================
# expire_pending
# =========================

def test_expire_pending_marks_expired_and_edits_tg():
    expired_draft = {
        "draft_id": "abc",
        "text": "stale draft",
        "telegram_chat_id": "999",
        "telegram_message_id": "55",
        "image_path": None,
    }
    flow, sb, _comp, _img, tg = make_flow(pending_expired=[expired_draft])

    flow.expire_pending()

    # status update persisted
    status_updates = [u for u in sb.updates if u[0] == "abc"]
    assert any(
        u[1].get("status") == "expired" for u in status_updates
    )
    # TG edited (text variant, since no image)
    assert len(tg.edit_body_calls) == 1
    assert tg.edit_body_calls[0]["has_image"] is False
    assert "Expired" in tg.edit_body_calls[0]["body"]
    assert "stale draft" in tg.edit_body_calls[0]["body"]


def test_expire_pending_uses_caption_edit_when_image_present():
    expired_draft = {
        "draft_id": "abc",
        "text": "with pic",
        "telegram_chat_id": "999",
        "telegram_message_id": "55",
        "image_path": "/tmp/pic.png",
    }
    flow, _sb, _comp, _img, tg = make_flow(pending_expired=[expired_draft])

    flow.expire_pending()
    assert tg.edit_body_calls[0]["has_image"] is True


def test_expire_pending_skips_drafts_without_telegram_ids():
    "Drafts that were never sent (e.g. tg was unavailable) shouldn't crash."
    expired_draft = {
        "draft_id": "abc",
        "text": "never sent",
        "telegram_chat_id": None,
        "telegram_message_id": None,
        "image_path": None,
    }
    flow, sb, _comp, _img, tg = make_flow(pending_expired=[expired_draft])

    flow.expire_pending()
    # DB still updated, but no TG edit happened
    assert any(u[1].get("status") == "expired" for u in sb.updates)
    assert tg.edit_body_calls == []


# =========================
# confirm_posted / report_failure
# =========================

def test_confirm_posted_edits_tg_with_url():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[{"text": "done", "image_prompt": None}],
    )
    draft_id = flow.create_new_draft()

    flow.confirm_posted(draft_id, "https://x.com/alice/status/123")

    posted_edit = tg.edit_body_calls[-1]
    assert "Posted" in posted_edit["body"]
    assert "https://x.com/alice/status/123" in posted_edit["body"]
    assert posted_edit["has_image"] is False


def test_confirm_posted_without_url_still_marks_posted():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[{"text": "done", "image_prompt": None}],
    )
    draft_id = flow.create_new_draft()

    flow.confirm_posted(draft_id, None)
    assert "Posted" in tg.edit_body_calls[-1]["body"]


def test_confirm_posted_uses_caption_when_image_present():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[{"text": "with pic", "image_prompt": "a cat"}],
        image_path="/tmp/cat.png",
    )
    draft_id = flow.create_new_draft()

    flow.confirm_posted(draft_id, "https://x.com/alice/status/456")
    assert tg.edit_body_calls[-1]["has_image"] is True


def test_report_failure_edits_tg_with_reason():
    flow, sb, _comp, _img, tg = make_flow(
        compose_results=[{"text": "oops", "image_prompt": None}],
    )
    draft_id = flow.create_new_draft()

    flow.report_failure(draft_id, "compose button not clickable")
    last = tg.edit_body_calls[-1]
    assert "Post failed" in last["body"]
    assert "compose button not clickable" in last["body"]


# =========================
# /prompt flow + origin-aware regeneration
# =========================

def test_create_new_draft_tags_origin_compose():
    flow, sb, _comp, _img, _tg = make_flow(
        compose_results=[{"text": "from compose", "image_prompt": None}],
    )
    draft_id = flow.create_new_draft()
    assert sb.rows[draft_id]["origin"] == "compose"


def test_create_draft_from_prompt_happy_path():
    flow, sb, composer, image_gen, tg = make_flow(
        prompt_results=[{"text": "captioned"}],
        image_path="/tmp/p.png",
    )
    draft_id = flow.create_draft_from_prompt("a sunny park bench")

    # compose_for_prompt called with user's prompt
    assert composer.prompt_calls[0]["image_prompt"] == "a sunny park bench"
    # image_gen called with user's prompt (not the composer's)
    assert image_gen.calls == [("a sunny park bench", draft_id)]
    # draft row stores the user's prompt + origin='prompt'
    assert sb.rows[draft_id]["image_prompt"] == "a sunny park bench"
    assert sb.rows[draft_id]["origin"] == "prompt"
    assert sb.rows[draft_id]["image_path"] == "/tmp/p.png"
    # sent as photo since image_path is set
    assert len(tg.sent_photo_drafts) == 1


def test_create_draft_from_prompt_composer_fails():
    flow, sb, _comp, _img, tg = make_flow(prompt_results=[None])
    result = flow.create_draft_from_prompt("some scene")

    assert result is None
    assert sb.rows == {}
    assert any("⚠️" in m["text"] for m in tg.sent_messages)


def test_regenerate_prompt_origin_preserves_image_prompt():
    "Regen on a /prompt-originated draft reuses the user's image_prompt."
    flow, sb, composer, image_gen, tg = make_flow(
        prompt_results=[
            {"text": "first caption"},   # initial create_draft_from_prompt
            {"text": "second caption"},  # regen
        ],
        image_path="/tmp/p.png",
    )
    draft_id = flow.create_draft_from_prompt("matcha on a cafe table")

    # Reset tracking state we only care about from the regen onwards
    image_gen_calls_before = len(image_gen.calls)
    compose_calls_before = len(composer.calls)
    prompt_calls_before = len(composer.prompt_calls)

    flow.regenerate(draft_id)

    # Regen should use compose_for_prompt, NOT the generic compose()
    assert len(composer.calls) == compose_calls_before
    assert len(composer.prompt_calls) == prompt_calls_before + 1
    # compose_for_prompt called with the original prompt
    assert composer.prompt_calls[-1]["image_prompt"] == "matcha on a cafe table"
    # Image gen called with the original prompt (not a new one)
    new_image_calls = image_gen.calls[image_gen_calls_before:]
    assert new_image_calls == [("matcha on a cafe table", draft_id)]
    # Draft row still has the user's original prompt
    assert sb.rows[draft_id]["image_prompt"] == "matcha on a cafe table"
    assert sb.rows[draft_id]["text"] == "second caption"


def test_regenerate_compose_origin_does_full_regen():
    "Regen on a /compose-originated draft still regenerates everything."
    flow, sb, composer, image_gen, tg = make_flow(
        compose_results=[
            {"text": "old text", "image_prompt": "old scene"},
            {"text": "new text", "image_prompt": "new scene"},
        ],
    )
    draft_id = flow.create_new_draft()
    flow.regenerate(draft_id)

    # Regen used the generic compose path (regen=True), not compose_for_prompt
    assert composer.calls[-1]["regen"] is True
    assert composer.prompt_calls == []
    # image_prompt was replaced with the LLM's new one
    assert sb.rows[draft_id]["image_prompt"] == "new scene"
    assert sb.rows[draft_id]["text"] == "new text"
