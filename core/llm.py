"""One interface to the LLM providers. Tries each configured model in order and falls through on
rate limits or outages, so a busy Gemini day quietly moves to Groq.

Adding Claude later = one more entry in config.yaml `llm.providers` plus a `_claude` method here.
"""
import json
import os
import re
import time
from typing import TypeVar

import requests
from pydantic import BaseModel, ValidationError

from core.log import get_logger

log = get_logger("llm")

T = TypeVar("T", bound=BaseModel)

KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TOKENS_PER_MINUTE = 8000  # free tier, per model
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


class Unavailable(RuntimeError):
    """This provider can't answer right now (rate limit, outage, bad key). Try the next one."""


class QuotaExhausted(Unavailable):
    """Daily free quota used up. Waiting won't help until it resets, so skip this provider for the rest of the run."""


DAILY_QUOTA_MARKERS = ("exceeded your current quota", "per day")


class LLMError(RuntimeError):
    """No provider produced a valid answer."""


def extract_json(text: str) -> str:
    """Strip markdown fences and any chatter around the outermost JSON object."""
    text = FENCE_RE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


class LLM:
    def __init__(self, cfg: dict):
        self.temperature = cfg["llm"]["temperature"]
        self.providers = []
        for p in cfg["llm"]["providers"]:
            key = os.getenv(KEY_ENV[p["name"]], "").strip()
            if key:
                self.providers.append({**p, "key": key})
            else:
                log.warning("Skipping %s: %s is empty in .env", p["name"], KEY_ENV[p["name"]])
        if not self.providers:
            raise SystemExit("No LLM keys found. Add GEMINI_API_KEY or GROQ_API_KEY to .env.")
        self.last_used = ""  # "provider/model" that answered the latest call

    def json(self, system: str, prompt: str, schema: type[T]) -> T:
        """Ask for JSON matching `schema`. A provider that returns invalid JSON gets one repair attempt."""
        instructions = (f"{system}\n\nReply with a single JSON object and nothing else. It must match this "
                        f"JSON schema:\n{json.dumps(schema.model_json_schema())}")
        errors = []
        for provider in list(self.providers):
            name = f"{provider['name']}/{provider['model']}"
            try:
                text = self._call(provider, instructions, prompt)
                try:
                    result = schema.model_validate_json(extract_json(text))
                except ValidationError as exc:
                    log.info("%s returned invalid JSON (%s), asking it to fix it",
                             name, " ".join(str(exc).split())[:200])
                    repair = (f"{prompt}\n\nYour previous answer was not valid:\n{exc}\n\n"
                              f"Previous answer:\n{text}\n\nReturn the corrected JSON object only.")
                    result = schema.model_validate_json(extract_json(self._call(provider, instructions, repair)))
            except QuotaExhausted as exc:
                log.warning("%s daily quota used up, skipping it for this run", name)
                self.providers.remove(provider)
                errors.append(f"{name}: {exc}")
                continue
            except Unavailable as exc:
                log.warning("%s unavailable: %s", name, exc)
                errors.append(f"{name}: {exc}")
                continue
            except ValidationError as exc:
                log.warning("%s still returned invalid JSON: %s", name, str(exc)[:200])
                errors.append(f"{name}: invalid JSON")
                continue
            self.last_used = name
            return result
        raise LLMError("All LLM providers failed: " + " | ".join(errors))

    def _call(self, provider: dict, system: str, prompt: str) -> str:
        call = {"gemini": self._gemini, "groq": self._groq}[provider["name"]]
        return call(provider, system, prompt)

    def _post(self, url: str, headers: dict, body: dict, max_wait: float) -> dict:
        """POST, waiting and retrying on per-minute rate limits (429) and overload (503) for up to `max_wait`
        seconds in total. Daily-quota 429s fail immediately, since waiting won't clear them."""
        waited = 0.0
        while True:
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=180)
            except requests.RequestException as exc:
                raise Unavailable(f"network error: {exc}") from exc
            if resp.status_code == 200:
                return resp.json()
            message = " ".join(resp.text.split())[:160]
            if resp.status_code == 429 and any(m in resp.text for m in DAILY_QUOTA_MARKERS):
                raise QuotaExhausted(f"HTTP 429: {message}")
            if resp.status_code in (429, 503):
                wait = max(float(resp.headers.get("retry-after", 0) or 0), 10 if resp.status_code == 503 else 5)
                if waited + wait <= max_wait:
                    log.info("HTTP %d (%s), retrying in %.0fs", resp.status_code,
                             "rate limited" if resp.status_code == 429 else "overloaded", wait)
                    time.sleep(wait)
                    waited += wait
                    continue
            if resp.status_code in (401, 403, 404, 413, 429) or resp.status_code >= 500:  # 413: over a per-minute cap
                raise Unavailable(f"HTTP {resp.status_code}: {message}")
            raise LLMError(f"HTTP {resp.status_code}: {message}")  # our request is wrong; don't hide it

    def _gemini(self, provider: dict, system: str, prompt: str) -> str:
        data = self._post(
            GEMINI_URL.format(model=provider["model"]),
            {"x-goog-api-key": provider["key"]},
            {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": prompt}]}],
             "generationConfig": {"temperature": self.temperature, "responseMimeType": "application/json"}},
            max_wait=40,  # other Gemini models and Groq are next in line
        )
        candidate = (data.get("candidates") or [{}])[0]
        if candidate.get("finishReason") not in (None, "STOP"):  # MAX_TOKENS, SAFETY, ...
            raise Unavailable(f"answer not finished ({candidate.get('finishReason')})")
        parts = candidate.get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    def _groq(self, provider: dict, system: str, prompt: str) -> str:
        # The free tier counts prompt + answer budget against 8,000 tokens a minute, so the answer gets what's left.
        prompt_tokens = (len(system) + len(prompt)) // 3  # rough; English averages ~4 characters per token
        data = self._post(
            GROQ_URL,
            {"Authorization": f"Bearer {provider['key']}"},
            # No response_format: Groq's strict JSON mode rejects long answers outright. json() validates instead.
            {"model": provider["model"], "temperature": self.temperature,
             "max_completion_tokens": max(1000, min(6000, GROQ_TOKENS_PER_MINUTE - prompt_tokens - 200)),
             "reasoning_effort": "low",
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
            max_wait=150,  # last in line; its free tier resets token limits every minute
        )
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise Unavailable(f"answer not finished ({choice.get('finish_reason')})")
        return choice["message"]["content"]
