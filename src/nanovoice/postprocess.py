"""Post-processing: vocabulary prompt, replacement rules, LLM cleanup."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

from .config import LLMConfig, Replacement

log = logging.getLogger("nanovoice.clean")


def build_prompt(terms: list[str], extra: str | None, max_chars: int = 600) -> str | None:
    """Join dictionary terms (in priority order, capped at max_chars) and the per-request prompt."""
    parts = []
    joined = ""
    for t in terms:
        candidate = f"{joined}, {t}" if joined else t
        if len(candidate) > max_chars:
            break
        joined = candidate
    if joined:
        parts.append(joined)
    if extra and extra.strip():
        parts.append(extra.strip())
    return " ".join(parts) or None


class Replacer:
    def __init__(self, rules: list[Replacement]):
        self._rules = []
        for r in rules:
            flags = re.IGNORECASE if r.ignore_case else 0
            pattern = r.pattern if r.regex else re.escape(r.pattern)
            # Literal rules use a function so backslashes in the replacement stay literal.
            repl = r.replacement if r.regex else (lambda _m, s=r.replacement: s)
            try:
                self._rules.append((re.compile(pattern, flags), repl))
            except re.error as e:
                raise ValueError(f"invalid replacement pattern {r.pattern!r}: {e}") from e

    def apply(self, text: str) -> str:
        for rx, repl in self._rules:
            text = rx.sub(repl, text)
        return text


@dataclass
class CleanResult:
    text: str
    cleaned: bool
    skipped: str | None = None  # why cleanup was not applied: disabled|short|error|rejected


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_TAG_RE = re.compile(r"</?transcript>")


def _visible_len(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def llm_client(cfg: LLMConfig, timeout_s: float, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(base_url=cfg.base_url.rstrip("/") + "/", headers=headers, timeout=timeout_s, transport=transport)


# Models that rejected the `temperature` parameter (some reasoning models only accept the default).
_NO_TEMPERATURE: set[str] = set()


def chat(client: httpx.Client, model: str, temperature: float, system: str, user: str) -> str:
    """One OpenAI-compatible chat completion; returns the content with <think> blocks removed."""
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if model not in _NO_TEMPERATURE:
        body["temperature"] = temperature
    resp = client.post("chat/completions", json=body)
    if resp.status_code == 400 and "temperature" in body and "temperature" in resp.text:
        log.info("model rejected temperature, retrying without it", extra={"fields": {"model": model}})
        _NO_TEMPERATURE.add(model)
        del body["temperature"]
        resp = client.post("chat/completions", json=body)
    resp.raise_for_status()
    return _THINK_RE.sub("", resp.json()["choices"][0]["message"]["content"] or "").strip()


class Cleaner:
    """LLM cleanup via an OpenAI-compatible chat completions endpoint. Never raises."""

    def __init__(self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._client = llm_client(cfg, cfg.timeout_s, transport)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def clean(self, text: str) -> CleanResult:
        if not self.cfg.enabled:
            return CleanResult(text, False, "disabled")
        if _visible_len(text) < self.cfg.min_chars:
            return CleanResult(text, False, "short")
        try:
            out = chat(self._client, self.cfg.model, self.cfg.temperature, self.cfg.system_prompt,
                       self.cfg.user_template.replace("{text}", text))
        except Exception as e:  # any failure falls back to the raw transcript
            log.warning("llm cleanup failed, using raw text", extra={"fields": {"error": f"{type(e).__name__}: {e}"[:300]}})
            return CleanResult(text, False, "error")
        out = _TAG_RE.sub("", out).strip()
        # Guard against the model answering / expanding instead of cleaning.
        if not out or len(out) > 2 * len(text) + 50:
            log.warning("llm output rejected, using raw text", extra={"fields": {"in_chars": len(text), "out_chars": len(out)}})
            return CleanResult(text, False, "rejected")
        return CleanResult(out, True)
