"""
Tweet composer. Given a character bio + recent post history, returns a new
draft as JSON: {"text": "...", "image_prompt": "..." | null}.

Phase 1 treats the image_prompt field as optional output — it's parsed and
stored on the draft but no image generation happens yet.
"""

import json
import logging
import os
import re
import time

import requests


class TweetComposer:
    "Generates new tweet drafts in the character's voice."

    INSTRUCTIONS = (
        "You are the character described above. Compose ONE tweet as this "
        "character, in their voice.\n\n"
        "Review the recent posts provided to avoid repeating yourself and to "
        "keep life events coherent (if you mentioned being at the airport "
        "yesterday, you are no longer at the airport today).\n\n"
        "Respond with ONLY a JSON object, no surrounding commentary:\n"
        '{"text": "<the tweet, max 280 chars>", '
        '"image_prompt": "<describe a SCENE to render, or null>"}\n\n'
        "Include an image_prompt only when the tweet would NATURALLY include "
        "a photo (selfies, food, views, objects you're holding). Pure thoughts "
        "and reactions don't need images.\n\n"
        "IMPORTANT about image_prompt: reference photos of the character "
        "(appearance, hair, build, face) are supplied separately to the image "
        "model, so DO NOT describe the character's physical appearance. "
        "Describe the SCENE only: location, pose/action, lighting, outfit, "
        "framing (mirror selfie / street candid / close-up), mood. Treat it "
        "as directing a photographer who already knows who the subject is.\n\n"
        "Examples of good image_prompts:\n"
        "- \"gym mirror selfie, black matching gym set, ponytail, natural "
        "gym lighting, full-length phone shot\"\n"
        "- \"matcha drink on a marble cafe table, morning light, overhead "
        "phone shot, no face in frame\"\n"
        "- \"walking candid on a london street, white trainers visible, "
        "pavement from the waist down, overcast daylight\"\n\n"
        "Never mention being an AI, bot, or language model."
    )

    REGEN_SUFFIX = (
        "\n\nThe user rejected your previous draft. Produce a DIFFERENT "
        "tweet — a different angle, topic, or mood than before."
    )

    INSTRUCTIONS_FOR_PROMPT = (
        "You are the character described above. The user has already "
        "decided what photo they want to post — the scene is given to you "
        "below. Your job is to write ONE tweet (max 280 chars) in the "
        "character's voice that fits naturally as a caption for this "
        "photo.\n\n"
        "Review the recent posts provided to avoid repeating yourself and "
        "keep life events coherent.\n\n"
        "Respond with ONLY a JSON object, no surrounding commentary:\n"
        '{"text": "<the tweet>"}\n\n'
        "Do not describe the image literally — the image speaks for "
        "itself. Write a caption that complements it: a thought, an "
        "observation, a reaction. Never mention being an AI, bot, or "
        "language model."
    )

    def __init__(self, config):
        self.logger = logging.getLogger("TWEET-COMPOSER")
        self.api_key = (
            os.getenv("OPENROUTER_API_KEY") or config["openrouter"]["api_key"]
        )
        self.endpoint = config["openrouter"]["endpoint"]
        self.model = config["openrouter"]["model"]
        self.timeout = config["openrouter"]["timeout"]

        with open(config["tweeting"]["character_file"], "r", encoding="utf-8") as f:
            self.character = f.read()

        self.context_recent = config["tweeting"].get("context_recent_posts", 20)

    def compose(self, recent_posts, regen=False):
        """Generate one draft. Returns {"text": str, "image_prompt": str|None}
        or None on failure.

        `recent_posts` is a list of draft rows (status='posted'), chronological.
        `regen` flags that this is a replacement for a rejected draft.
        """
        system = self.character + "\n\n" + self.INSTRUCTIONS
        if regen:
            system += self.REGEN_SUFFIX

        user_content = self._format_recent(recent_posts)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

        raw = self._call_llm(messages)
        if raw is None:
            return None
        return self._parse_json(raw)

    def compose_for_prompt(self, image_prompt, recent_posts):
        """Generate tweet text to caption a user-supplied image scene.

        Returns {"text": str, "image_prompt": str} — image_prompt echoes
        back the user's input so downstream code treats the result the
        same as a regular compose() output. None on failure.
        """
        system = self.character + "\n\n" + self.INSTRUCTIONS_FOR_PROMPT
        user_content = self._format_recent(recent_posts)
        user_content += (
            "\n\nYou are about to post a photo described as:\n"
            f"{image_prompt}\n\n"
            "Write the tweet text to caption this photo."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

        raw = self._call_llm(messages)
        if raw is None:
            return None
        parsed = self._parse_json(raw)
        if parsed is None:
            return None
        # Override whatever image_prompt the LLM returned (if any) with the
        # user's authoritative version.
        parsed["image_prompt"] = image_prompt
        return parsed

    def _format_recent(self, recent_posts):
        if not recent_posts:
            return (
                "This is your first tweet. Compose something that establishes "
                "your voice without being on-the-nose about who you are."
            )

        lines = ["Your most recent tweets (oldest → newest):"]
        for p in recent_posts:
            lines.append(f"- {p['text']}")
        lines.append("\nCompose the next tweet now.")
        return "\n".join(lines)

    def _call_llm(self, messages, max_retries=2):
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries - 1:
                    self.logger.warning(
                        "LLM request failed (attempt %d), retrying: %s",
                        attempt + 1, e,
                    )
                    time.sleep(2)
                    continue
                self.logger.error("LLM request failed: %s", e)
                return None

            except requests.exceptions.RequestException as e:
                self.logger.error("LLM error: %s", e)
                return None

    def _parse_json(self, raw):
        "Extract {text, image_prompt} from the LLM output, tolerant of stray prose."
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # Some models wrap JSON in ```json ... ``` or add trailing text.
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                self.logger.error("LLM returned non-JSON output: %r", raw[:200])
                return None
            try:
                obj = json.loads(match.group(0))
            except json.JSONDecodeError:
                self.logger.error("Could not parse JSON from LLM output: %r", raw[:200])
                return None

        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            self.logger.error("LLM output missing 'text' field: %r", obj)
            return None

        image_prompt = obj.get("image_prompt")
        if image_prompt in ("", "null", "None"):
            image_prompt = None
        if image_prompt is not None and not isinstance(image_prompt, str):
            image_prompt = None

        return {"text": text.strip(), "image_prompt": image_prompt}
