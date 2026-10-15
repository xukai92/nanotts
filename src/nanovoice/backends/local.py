"""Placeholder for an in-process model (faster-whisper / mlx-whisper).

Not implemented in this build. A real implementation would load the model once in
`__init__` (it is created once and kept for the process lifetime) and run it in
`transcribe`, passing `prompt` as `initial_prompt`.
"""

from __future__ import annotations

from pathlib import Path

from ..config import LocalASRConfig
from . import BackendUnavailable


class LocalBackend:
    name = "local"

    def __init__(self, cfg: LocalASRConfig):
        self.model = cfg.model
        raise BackendUnavailable("the local backend is not implemented in this build; use backend=remote")

    def transcribe(self, audio_path: Path, language: str | None, prompt: str | None) -> str:
        raise BackendUnavailable("local backend not implemented")
