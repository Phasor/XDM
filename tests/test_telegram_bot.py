"""Unit tests for TelegramBot — just the static helpers that are pure logic.
HTTP and threading aren't covered (would need heavy mocking for little gain)."""

from notifications.telegram_bot import TelegramBot


# =========================
# _parse_draft_id_from_edit_prompt
# =========================

def test_parses_draft_id_from_plain_text_prompt():
    "The ✏️ Edit prompt is sent as plain text (no markdown) — regex must match."
    text = "✏️ Edit draft abc123def456 — reply with your replacement text."
    assert TelegramBot._parse_draft_id_from_edit_prompt(text) == "abc123def456"


def test_parses_draft_id_even_if_backticks_present():
    "Legacy / defensive: also matches if the prompt ever had backticks."
    text = "✏️ Edit draft `abc123def456` — reply with your replacement text."
    assert TelegramBot._parse_draft_id_from_edit_prompt(text) == "abc123def456"


def test_ignores_non_edit_prompt_text():
    assert TelegramBot._parse_draft_id_from_edit_prompt("hello there") is None
    assert TelegramBot._parse_draft_id_from_edit_prompt("") is None
    assert TelegramBot._parse_draft_id_from_edit_prompt(None) is None


def test_ignores_text_without_hex_id():
    "Text mentioning 'Edit draft' but without a hex id shouldn't produce a false positive."
    text = "Edit draft something that isn't hex"
    assert TelegramBot._parse_draft_id_from_edit_prompt(text) is None


def test_handles_typical_draft_id_length():
    "Our draft_ids are 12 hex chars (uuid4().hex[:12])."
    draft_id = "a" * 12
    text = f"✏️ Edit draft {draft_id} — reply with your replacement text."
    assert TelegramBot._parse_draft_id_from_edit_prompt(text) == draft_id


# =========================
# _draft_keyboard shape (sanity check on keyboard layout)
# =========================

def test_draft_keyboard_has_four_buttons_in_2x2():
    kb = TelegramBot._draft_keyboard("abc")
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert len(rows[0]) == 2  # Approve, Regen
    assert len(rows[1]) == 2  # Edit, Reject

    all_actions = [btn["callback_data"] for row in rows for btn in row]
    assert "approve:abc" in all_actions
    assert "regen:abc" in all_actions
    assert "edit:abc" in all_actions
    assert "reject:abc" in all_actions
