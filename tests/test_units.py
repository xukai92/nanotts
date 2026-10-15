import json
from pathlib import Path

import httpx
import pytest

from nanovoice.audio import AudioError, prepare
from nanovoice.backends import BackendError, BackendTimeout, BackendUnavailable, create_backend
from nanovoice.backends.remote import RemoteBackend
from nanovoice.config import AudioConfig, ConfigError, RemoteASRConfig, config_from_dict, load_config
from nanovoice.postprocess import Replacer, build_prompt
from nanovoice.config import Replacement


def _flac(tmp_path) -> Path:
    p = tmp_path / "a.flac"
    p.write_bytes(b"fLaC" + b"\0" * 100)
    return p


def test_remote_backend_request(tmp_path, monkeypatch):
    monkeypatch.setenv("STT_ASR_API_KEY", "sk-test")
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = req.content
        return httpx.Response(200, json={"text": " 你好 hello \n"})

    b = RemoteBackend(RemoteASRConfig(base_url="https://api.example.com/v1/", model="m1"), httpx.MockTransport(handler))
    assert b.transcribe(_flac(tmp_path), "zh", "Tailscale") == "你好 hello"
    assert seen["url"] == "https://api.example.com/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"].decode("latin-1")
    for part in ('name="model"', "m1", 'name="language"', "zh", 'name="prompt"', "Tailscale", 'name="file"'):
        assert part in body


def test_remote_backend_auto_language_omits_field(tmp_path):
    seen = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "x"})

    RemoteBackend(RemoteASRConfig(), httpx.MockTransport(handler)).transcribe(_flac(tmp_path), None, None)
    assert b'name="language"' not in seen["body"] and b'name="prompt"' not in seen["body"]


@pytest.mark.parametrize("handler,exc", [
    (lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}), BackendError),
    (lambda r: httpx.Response(200, text="not json"), BackendError),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t")), BackendTimeout),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), BackendError),
])
def test_remote_backend_errors(tmp_path, handler, exc):
    b = RemoteBackend(RemoteASRConfig(), httpx.MockTransport(handler))
    with pytest.raises(exc):
        b.transcribe(_flac(tmp_path), None, None)


def test_local_backend_is_a_stub():
    with pytest.raises(BackendUnavailable):
        create_backend("local", config_from_dict({}))
    with pytest.raises(ValueError):
        create_backend("nope", config_from_dict({}))


def test_prepare_converts_and_trims(tmp_path, audio_dir):
    out = prepare(audio_dir / "speech.m4a", tmp_path / "o.flac", AudioConfig())
    assert out.path.read_bytes()[:4] == b"fLaC"
    assert 1900 <= out.input_ms <= 2600
    assert 1300 <= out.speech_ms < out.input_ms  # leading/trailing silence removed
    no_vad = prepare(audio_dir / "speech.wav", tmp_path / "p.flac", AudioConfig(vad=False))
    assert abs(no_vad.speech_ms - 2500) < 50
    assert prepare(audio_dir / "silence.wav", tmp_path / "s.flac", AudioConfig()).speech_ms == 0
    with pytest.raises(AudioError):
        prepare(audio_dir / "notes.txt", tmp_path / "x.flac", AudioConfig())


def test_build_prompt():
    assert build_prompt([], None) is None
    assert build_prompt(["A", "B"], " c ") == "A, B c"


def test_replacer_literal_and_regex():
    r = Replacer([
        Replacement(pattern="a.b", replacement=r"x\1"),
        Replacement(pattern=r"(\d+)元", replacement=r"¥\1", regex=True),
        Replacement(pattern="CLAUDE", replacement="Claude", ignore_case=True),
    ])
    assert r.apply("a.b axb 30元 claude") == r"x\1 axb ¥30 Claude"
    with pytest.raises(ValueError):
        Replacer([Replacement(pattern="(", replacement="", regex=True)])


def test_config_validation(tmp_path):
    with pytest.raises(ConfigError, match="unknown key"):
        config_from_dict({"server": {"hots": "x"}})
    with pytest.raises(ConfigError, match="expected int"):
        config_from_dict({"server": {"port": "80"}})
    with pytest.raises(ConfigError):
        config_from_dict({"auth": {"mode": "magic"}})
    with pytest.raises(ConfigError):
        config_from_dict({"server": {"base_path": "stt/"}})
    assert config_from_dict({"server": {"max_upload_mb": 10}}).server.max_upload_mb == 10.0


def test_example_config_loads():
    cfg, path = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert path is not None
    assert cfg.asr.backend == "remote"
    Replacer(cfg.replacements)  # all example rules compile
