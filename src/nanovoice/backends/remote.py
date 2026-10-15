"""Any OpenAI-compatible `/v1/audio/transcriptions` endpoint (OpenAI, Groq, self-hosted...)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from ..config import RemoteASRConfig
from . import BackendError, BackendTimeout


def error_snippet(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if err:
            return str(err)[:300]
    except Exception:
        pass
    return resp.text[:300]


class RemoteBackend:
    name = "remote"

    def __init__(self, cfg: RemoteASRConfig, transport: httpx.BaseTransport | None = None):
        self.model = cfg.model
        self.base_url = cfg.base_url.rstrip("/")
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.Client(
            base_url=self.base_url + "/", headers=headers, timeout=cfg.timeout_s, transport=transport
        )

    def transcribe(self, audio_path: Path, language: str | None, prompt: str | None) -> str:
        data = {"model": self.model, "response_format": "json"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        try:
            with audio_path.open("rb") as f:
                resp = self._client.post(
                    "audio/transcriptions", data=data, files={"file": (audio_path.name, f, "audio/flac")}
                )
        except httpx.TimeoutException as e:
            raise BackendTimeout(f"ASR request timed out ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            raise BackendError(f"ASR request failed: {e}") from e
        if resp.status_code >= 400:
            raise BackendError(f"ASR upstream returned {resp.status_code}: {error_snippet(resp)}")
        try:
            return str(resp.json()["text"]).strip()
        except Exception as e:
            raise BackendError("ASR upstream returned an unexpected response") from e
