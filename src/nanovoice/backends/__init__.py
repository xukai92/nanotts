"""ASR backends. A backend is anything with `name`, `model` and `transcribe()`.

To add a backend (e.g. FunASR SenseVoice): write a class with the same shape in
its own module and add a branch to `create_backend` plus its name to `BACKEND_NAMES`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Config

BACKEND_NAMES = ("remote", "local")


class BackendError(Exception):
    """The backend failed to transcribe (upstream error, bad response...)."""


class BackendTimeout(BackendError):
    pass


class BackendUnavailable(BackendError):
    """The backend cannot be used in this installation."""


class Backend(Protocol):
    name: str
    model: str

    def transcribe(self, audio_path: Path, language: str | None, prompt: str | None) -> str:
        """Return the transcript. `language=None` means auto-detect."""
        ...


def create_backend(name: str, cfg: Config) -> Backend:
    if name == "remote":
        from .remote import RemoteBackend

        return RemoteBackend(cfg.asr.remote)
    if name == "local":
        from .local import LocalBackend

        return LocalBackend(cfg.asr.local)
    raise ValueError(f"unknown backend {name!r}; available: {', '.join(BACKEND_NAMES)}")
