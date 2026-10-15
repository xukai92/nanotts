"""Post-processing: vocabulary prompt, replacement rules, LLM cleanup."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

from .config import LLMConfig, Replacement

log = logging.getLogger("nanovoice.clean")


def build_prompt(vocabulary: list[str], extra: str | None) -> str | None:
    parts = []
    if vocabulary:
        parts.append(", ".join(vocabulary))
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


class Cleaner:
    """LLM cleanup via an OpenAI-compatible chat completions endpoint. Never raises."""

    def __init__(self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/") + "/", headers=headers, timeout=cfg.timeout_s, transport=transport
        )

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def clean(self, text: str) -> CleanResult:
        if not self.cfg.enabled:
            return CleanResult(text, False, "disabled")
        if _visible_len(text) < self.cfg.min_chars:
            return CleanResult(text, False, "short")
        try:
            out = self._complete(text)
        except Exception as e:  # any failure falls back to the raw transcript
            log.warning("llm cleanup failed, using raw text", extra={"fields": {"error": f"{type(e).__name__}: {e}"[:300]}})
            return CleanResult(text, False, "error")
        out = _TAG_RE.sub("", _THINK_RE.sub("", out)).strip()
        # Guard against the model answering / expanding instead of cleaning.
        if not out or len(out) > 2 * len(text) + 50:
            log.warning("llm output rejected, using raw text", extra={"fields": {"in_chars": len(text), "out_chars": len(out)}})
            return CleanResult(text, False, "rejected")
        return CleanResult(out, True)

    def _complete(self, text: str) -> str:
        resp = self._client.post(
            "chat/completions",
            json={
                "model": self.cfg.model,
                "temperature": self.cfg.temperature,
                "messages": [
                    {"role": "system", "content": self.cfg.system_prompt},
                    {"role": "user", "content": self.cfg.user_template.replace("{text}", text)},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""
