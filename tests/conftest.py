import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from nanovoice.config import config_from_dict
from nanovoice.postprocess import Cleaner
from nanovoice.server import create_app

TOKEN = "test-token"


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def audio_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("audio")
    # ~1.5 s of "speech" (a tone, so VAD keeps it) surrounded by silence
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.5",
            "-f", "lavfi", "-i", "sine=f=440:d=1.5:r=44100",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.5",
            "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(d / "speech.wav"))
    _ffmpeg("-i", str(d / "speech.wav"), "-c:a", "aac", str(d / "speech.m4a"))
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=2", str(d / "silence.wav"))
    (d / "notes.txt").write_text("this is not audio\n")
    return d


class FakeBackend:
    name = "remote"
    model = "fake-model"

    def __init__(self, text="hello world 你好世界", delay=0.0, error: Exception | None = None):
        self.text, self.delay, self.error = text, delay, error
        self.calls = []
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, audio_path, language, prompt):
        assert audio_path.exists() and audio_path.read_bytes()[:4] == b"fLaC"
        self.calls.append({"language": language, "prompt": prompt, "path": audio_path})
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            if self.error:
                raise self.error
            return self.text
        finally:
            with self._lock:
                self.active -= 1


def llm_transport(handler):
    """A Cleaner transport: handler(request) -> httpx.Response, or raises."""
    return httpx.MockTransport(handler)


def llm_reply(content: str):
    return lambda req: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setenv("STT_TOKEN", TOKEN)

    def _make(overrides: dict | None = None, backend=None, llm=None, auth=True):
        data = {"llm": {"enabled": True, "min_chars": 5}}
        for k, v in (overrides or {}).items():
            data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
        cfg = config_from_dict(data)
        backend = backend or FakeBackend()
        cleaner = Cleaner(cfg.llm, transport=llm_transport(llm or llm_reply("Hello world. 你好世界。")))
        app = create_app(cfg, backends={"remote": backend}, cleaner=cleaner)
        client = TestClient(app, raise_server_exceptions=False)
        if auth:
            client.headers["Authorization"] = f"Bearer {TOKEN}"
        client.backend = backend
        return client

    return _make


def upload(path: Path, name: str | None = None):
    return {"file": (name or path.name, path.read_bytes())}
