import logging
import os
import time
import requests


class LLM:
    "LLM client to interact with OpenRouter API"

    def __init__(self, config):
        self.logger = logging.getLogger("LLM")
        self.api_key = (
            os.getenv("OPENROUTER_API_KEY") or config["openrouter"]["api_key"]
        )
        self.endpoint = config["openrouter"]["endpoint"]
        self.model = config["openrouter"]["model"]
        self.timeout = config["openrouter"]["timeout"]

        with open(config["openrouter"]["personality_file"], "r", encoding="utf-8") as f:
            self.personality = f.read()

    def get_response(self, chat_history, max_retries=2):
        "Send messages to LLM and get response, with retry for transient errors"
        messages = self.get_conversation_context(chat_history)
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
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries - 1:
                    self.logger.warning("LLM request failed (attempt %d), retrying: %s", attempt + 1, e)
                    time.sleep(2)
                    continue
                self.logger.error("LLM request failed after %d attempts: %s", max_retries, e)
                return None

            except requests.exceptions.RequestException as e:
                self.logger.error("LLM error: %s", e)
                return None

    def get_conversation_context(self, chat_history):
        """Build LLM context from Supabase history only (single source of truth)."""
        context = [{"role": "system", "content": self.personality}]
        buffer = {}

        for msg in chat_history:
            role = msg["sender"]
            text = msg["message_text"]

            if buffer and buffer["role"] == role:
                buffer["content"] += "\n\n" + text
            else:
                if buffer:
                    context.append(buffer)
                buffer = {"role": role, "content": text}

        if buffer:
            context.append(buffer)

        self.logger.debug("Context built from %d messages", len(chat_history))
        return context
