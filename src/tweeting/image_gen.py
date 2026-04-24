"""
fal.ai image generator supporting two modes:

- "flux_lora"       — FLUX.1 Dev text-to-image with a character LoRA
                      (trained via fal's portrait trainer). Identity comes
                      from the LoRA, so no reference images are needed at
                      inference time. Outputs don't carry C2PA / SynthID,
                      so X doesn't attach a "Made with AI" label.

- "nano_banana_edit" — Google nano-banana-pro/edit image-to-image with
                      up to 3 local reference images, base64-encoded as
                      data URIs. Best character fidelity but outputs are
                      provenance-signed and X labels them "Made with AI".

Mode is selected via config["fal_ai"]["mode"]. Each generate() call
submits to the configured model, polls, and downloads the result. All
failures degrade to None so the draft pipeline falls back to text-only.
"""

import base64
import logging
import mimetypes
import os
import time

import requests


MODE_FLUX_LORA = "flux_lora"
MODE_NANO_BANANA_EDIT = "nano_banana_edit"


class ImageGenerator:
    "Generates one image per prompt in either flux_lora or nano_banana_edit mode."

    QUEUE_BASE = "https://queue.fal.run/{model}"
    POLL_INTERVAL = 2

    def __init__(self, config):
        self.logger = logging.getLogger("IMAGE-GEN")
        fal = config["fal_ai"]
        self.api_key = os.getenv("FAL_KEY") or fal.get("api_key", "")
        self.mode = fal.get("mode", MODE_FLUX_LORA)
        self.model = fal.get("model", "fal-ai/flux-lora")
        self.timeout = int(fal.get("timeout_seconds", 180))
        self.image_dir = fal.get("image_dir", "generated_images")
        os.makedirs(self.image_dir, exist_ok=True)

        # flux_lora specific
        self.lora_url = fal.get("lora_url", "")
        self.lora_scale = float(fal.get("lora_scale", 1.0))
        self.trigger_phrase = fal.get("trigger_phrase", "")
        self.image_size = fal.get("image_size", "portrait_16_9")

        # nano_banana_edit specific
        self.reference_uris = []
        if self.mode == MODE_NANO_BANANA_EDIT:
            reference_paths = (
                config.get("tweeting", {}).get("character_reference_images") or []
            )
            self.reference_uris = self._load_references(reference_paths)

    def enabled(self):
        if not self.api_key:
            return False
        if self.mode == MODE_FLUX_LORA:
            return bool(self.lora_url)
        if self.mode == MODE_NANO_BANANA_EDIT:
            return bool(self.reference_uris)
        return False

    def generate(self, prompt, draft_id):
        """Render `prompt` as the character in a new scene, save it,
        return the local path. None on any failure."""
        if not self.enabled():
            self.logger.info(
                "fal.ai disabled (mode=%s, missing key/lora/references); skipping image",
                self.mode,
            )
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
    # Submit (mode-specific body)
    # =========================

    def _submit(self, prompt):
        if self.mode == MODE_FLUX_LORA:
            body = self._build_flux_lora_body(prompt)
        else:
            body = self._build_nano_banana_body(prompt)

        url = self.QUEUE_BASE.format(model=self.model)
        resp = requests.post(
            url, headers=self._headers(), json=body, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        request_id = data.get("request_id")
        status_url = data.get("status_url")
        response_url = data.get("response_url")
        if not request_id or not status_url or not response_url:
            raise RuntimeError(f"Unexpected submit response: {data}")
        return request_id, status_url, response_url

    def _build_flux_lora_body(self, prompt):
        # Prepend the trigger phrase so the scene prompt becomes
        # "emvoss woman walking in a london park, ...". Match LoRA captions.
        if self.trigger_phrase and self.trigger_phrase.lower() not in prompt.lower():
            prompt = f"{self.trigger_phrase} woman {prompt}"
        return {
            "prompt": prompt,
            "loras": [{"path": self.lora_url, "scale": self.lora_scale}],
            "image_size": self.image_size,
            "num_images": 1,
        }

    def _build_nano_banana_body(self, prompt):
        return {
            "prompt": prompt,
            "image_urls": self.reference_uris,
        }

    # =========================
    # Reference loading (nano_banana_edit only)
    # =========================

    def _load_references(self, paths):
        "Read reference images from disk and encode as base64 data URIs."
        uris = []
        for path in paths:
            if not os.path.isfile(path):
                self.logger.warning("Reference image not found, skipping: %s", path)
                continue
            mime, _ = mimetypes.guess_type(path)
            if not mime or not mime.startswith("image/"):
                mime = "image/jpeg"
            try:
                with open(path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("ascii")
            except OSError as e:
                self.logger.warning(
                    "Failed to read reference %s: %s", path, e,
                )
                continue
            uris.append(f"data:{mime};base64,{encoded}")
            self.logger.info("Loaded reference image: %s (%s)", path, mime)
        return uris

    # =========================
    # Poll / fetch / download
    # =========================

    def _headers(self):
        return {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }

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
