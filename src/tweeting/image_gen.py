"""
fal.ai image generator (nano-banana / Gemini image).

Uses the fal queue API directly via `requests` — no extra SDK dep. All
failures degrade to None, so the draft pipeline falls back to a text-only
tweet rather than crashing.
"""

import logging
import os
import time

import requests


class ImageGenerator:
    "Generates one image per prompt and saves it to disk."

    QUEUE_BASE = "https://queue.fal.run/{model}"
    POLL_INTERVAL = 2

    def __init__(self, config):
        self.logger = logging.getLogger("IMAGE-GEN")
        fal = config["fal_ai"]
        self.api_key = os.getenv("FAL_KEY") or fal.get("api_key", "")
        self.model = fal.get("model", "fal-ai/nano-banana")
        self.timeout = int(fal.get("timeout_seconds", 120))
        self.image_dir = fal.get("image_dir", "generated_images")
        os.makedirs(self.image_dir, exist_ok=True)

    def enabled(self):
        return bool(self.api_key)

    def generate(self, prompt, draft_id):
        """Render `prompt` to an image, save it, return the local path.
        Returns None on any failure (missing key, timeout, HTTP error,
        content policy rejection)."""
        if not self.enabled():
            self.logger.info("fal.ai disabled (no API key); skipping image")
            return None

        try:
            request_id, status_url, response_url = self._submit(prompt)
        except Exception as e:
            self.logger.error("fal.ai submit failed: %s", e)
            return None

        try:
            image_url = self._poll_and_fetch(request_id, status_url, response_url)
        except Exception as e:
            self.logger.error("fal.ai poll/fetch failed: %s", e)
            return None

        if not image_url:
            return None

        return self._download(image_url, draft_id)

    # =========================
    # Internals
    # =========================

    def _headers(self):
        return {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }

    def _submit(self, prompt):
        url = self.QUEUE_BASE.format(model=self.model)
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"prompt": prompt},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        request_id = data.get("request_id")
        # fal returns status_url / response_url directly — prefer those over
        # reconstructing from the model name.
        status_url = data.get("status_url")
        response_url = data.get("response_url")
        if not request_id or not status_url or not response_url:
            raise RuntimeError(f"Unexpected submit response: {data}")
        return request_id, status_url, response_url

    def _poll_and_fetch(self, request_id, status_url, response_url):
        end = time.time() + self.timeout
        while time.time() < end:
            resp = requests.get(status_url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            status = resp.json().get("status")
            if status == "COMPLETED":
                break
            if status in ("FAILED", "CANCELLED", "ERROR"):
                self.logger.error("fal.ai job %s: %s", request_id, status)
                return None
            time.sleep(self.POLL_INTERVAL)
        else:
            self.logger.error("fal.ai job %s timed out", request_id)
            return None

        resp = requests.get(response_url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        images = data.get("images") or []
        if not images:
            self.logger.error("fal.ai returned no images: %s", data)
            return None
        return images[0].get("url")

    def _download(self, image_url, draft_id):
        path = os.path.join(self.image_dir, f"{draft_id}.png")
        try:
            resp = requests.get(image_url, timeout=30, stream=True)
            resp.raise_for_status()
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            self.logger.info("Image saved: %s", path)
            return path
        except requests.exceptions.RequestException as e:
            self.logger.error("Image download failed: %s", e)
            return None
