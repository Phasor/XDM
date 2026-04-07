import logging
import os
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

    def get_response(self, new_chat, chat_history):
        "Send messages to LLM and get response"
        messages = self.get_conversation_context(chat_history, new_chat)
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

        except requests.exceptions.RequestException as e:
            self.logger.error("Error: %s", e)
            return None

    def get_conversation_context(self, chat_history, new_chat):
        """Return formatted conversation for LLM with merged user messages"""
        llm_chat_history = [
            {"role": msg["sender"], "content": msg["message_text"]}
            for msg in chat_history
        ]
        llm_messages = [
            {"role": msg["author"], "content": msg["text"]} for msg in new_chat
        ]
        messages = llm_chat_history + llm_messages

        context = [{"role": "system", "content": self.personality}]
        buffer = {}

        for m in messages:
            role = m["role"]
            text = m["content"]

            if buffer and buffer["role"] == role:
                buffer["content"] += "\n\n" + text
            else:
                if buffer:
                    context.append(buffer)
                buffer = {"role": role, "content": text}

        if buffer:
            context.append(buffer)

        self.logger.debug("Context built (merged)")
        return context
