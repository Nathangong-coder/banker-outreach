"""One interface over Anthropic, OpenAI, Google, and OpenRouter.

Raw HTTP rather than four SDKs — fewer dependencies, and the request shapes
here are small enough that the SDKs buy nothing.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .config import settings
from .keypool import AllKeysExhausted, KeyPool

log = logging.getLogger(__name__)

DEFAULT_BASES = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "openrouter": "https://openrouter.ai/api/v1",
}


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None,
                 keys: list[str] | None = None, base_url: str | None = None):
        self.provider = (provider or settings.llm.provider).lower()
        if self.provider not in DEFAULT_BASES:
            raise ValueError(
                f"Unknown provider {self.provider!r}. "
                f"Pick one of: {', '.join(DEFAULT_BASES)}"
            )
        self.model = model or settings.llm.model
        self.base = (base_url or settings.llm.base_url
                     or DEFAULT_BASES[self.provider]).rstrip("/")
        self.pool = KeyPool(self.provider, keys if keys is not None else settings.llm.keys)
        self.client = httpx.Client(timeout=90.0)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def complete(self, system: str, user: str, max_tokens: int = 1200,
                 attempts: int = 4) -> str:
        last_err: Exception | None = None
        for attempt in range(attempts):
            state = self.pool.acquire()
            url, headers, payload = self._build(state.key, system, user, max_tokens)
            try:
                r = self.client.post(url, headers=headers, json=payload)
            except httpx.RequestError as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:
                self.pool.penalize(state, float(r.headers.get("retry-after", 30)))
                continue
            if r.status_code in (401, 403):
                self.pool.retire(state, f"HTTP {r.status_code}")
                continue
            if r.status_code >= 500:
                last_err = RuntimeError(f"{self.provider} {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(
                    f"{self.provider} {r.status_code}: {r.text[:300]}"
                )
            return self._extract(r.json())

        raise AllKeysExhausted(f"{self.provider} failed after {attempts}: {last_err}")

    def _build(self, key: str, system: str, user: str, max_tokens: int):
        if self.provider in ("anthropic",):
            return (
                f"{self.base}/messages",
                {"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
                {"model": self.model, "max_tokens": max_tokens, "system": system,
                 "messages": [{"role": "user", "content": user}]},
            )
        if self.provider in ("openai", "openrouter"):
            return (
                f"{self.base}/chat/completions",
                {"Authorization": f"Bearer {key}", "content-type": "application/json"},
                {"model": self.model, "max_tokens": max_tokens,
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user}]},
            )
        # google
        return (
            f"{self.base}/models/{self.model}:generateContent?key={key}",
            {"content-type": "application/json"},
            {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": user}]}],
             "generationConfig": {"maxOutputTokens": max_tokens}},
        )

    def _extract(self, data: dict) -> str:
        if self.provider == "anthropic":
            return "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text"
            ).strip()
        if self.provider in ("openai", "openrouter"):
            return (data["choices"][0]["message"].get("content") or "").strip()
        parts = data["candidates"][0]["content"].get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()

    def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> dict:
        """For prompts that must return an object. Tolerates fenced output."""
        raw = self.complete(system, user, max_tokens)
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise ValueError(f"Model did not return JSON. Got: {raw[:200]}")
