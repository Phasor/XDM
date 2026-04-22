"""Unit tests for TweetComposer._parse_json — tolerant extraction of
{text, image_prompt} from LLM output."""

import logging

import pytest

from tweeting.composer import TweetComposer


@pytest.fixture
def parser():
    "A bare TweetComposer for testing _parse_json without file IO or HTTP."
    c = TweetComposer.__new__(TweetComposer)
    c.logger = logging.getLogger("test-composer")
    return c


# =========================
# Happy path
# =========================

def test_parses_valid_json_with_text_and_image_prompt(parser):
    raw = '{"text": "hello world", "image_prompt": "a red bike"}'
    result = parser._parse_json(raw)
    assert result == {"text": "hello world", "image_prompt": "a red bike"}


def test_parses_json_with_null_image_prompt(parser):
    raw = '{"text": "just a thought", "image_prompt": null}'
    result = parser._parse_json(raw)
    assert result == {"text": "just a thought", "image_prompt": None}


def test_strips_surrounding_whitespace_in_text(parser):
    raw = '{"text": "   hello   ", "image_prompt": null}'
    result = parser._parse_json(raw)
    assert result["text"] == "hello"


# =========================
# Tolerant parsing: wrapping, prose, markdown
# =========================

def test_extracts_json_from_markdown_fence(parser):
    raw = '```json\n{"text": "wrapped", "image_prompt": null}\n```'
    result = parser._parse_json(raw)
    assert result == {"text": "wrapped", "image_prompt": None}


def test_extracts_json_with_leading_prose(parser):
    raw = 'Sure, here is the tweet: {"text": "prose prefix", "image_prompt": null}'
    result = parser._parse_json(raw)
    assert result == {"text": "prose prefix", "image_prompt": None}


def test_extracts_json_with_trailing_prose(parser):
    raw = '{"text": "trailing ok", "image_prompt": null} hope this helps!'
    result = parser._parse_json(raw)
    assert result == {"text": "trailing ok", "image_prompt": None}


# =========================
# image_prompt normalization
# =========================

def test_empty_string_image_prompt_becomes_none(parser):
    raw = '{"text": "hi", "image_prompt": ""}'
    assert parser._parse_json(raw)["image_prompt"] is None


def test_string_null_image_prompt_becomes_none(parser):
    "Some LLMs return the literal string 'null' instead of JSON null."
    raw = '{"text": "hi", "image_prompt": "null"}'
    assert parser._parse_json(raw)["image_prompt"] is None


def test_string_None_image_prompt_becomes_none(parser):
    raw = '{"text": "hi", "image_prompt": "None"}'
    assert parser._parse_json(raw)["image_prompt"] is None


def test_non_string_image_prompt_becomes_none(parser):
    "If the model returns e.g. a dict or number for image_prompt, drop it."
    raw = '{"text": "hi", "image_prompt": {"style": "photo"}}'
    assert parser._parse_json(raw)["image_prompt"] is None

    raw2 = '{"text": "hi", "image_prompt": 42}'
    assert parser._parse_json(raw2)["image_prompt"] is None


# =========================
# Failure modes
# =========================

def test_missing_text_returns_none(parser):
    raw = '{"image_prompt": "a photo"}'
    assert parser._parse_json(raw) is None


def test_empty_text_returns_none(parser):
    raw = '{"text": "", "image_prompt": null}'
    assert parser._parse_json(raw) is None


def test_whitespace_only_text_returns_none(parser):
    raw = '{"text": "   ", "image_prompt": null}'
    assert parser._parse_json(raw) is None


def test_non_string_text_returns_none(parser):
    raw = '{"text": 123, "image_prompt": null}'
    assert parser._parse_json(raw) is None


def test_non_json_output_returns_none(parser):
    raw = "Sorry, I cannot do that."
    assert parser._parse_json(raw) is None


def test_malformed_json_returns_none(parser):
    raw = '{"text": "hi", "image_prompt": null'  # missing closing brace
    assert parser._parse_json(raw) is None
