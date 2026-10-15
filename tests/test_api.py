import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import FakeBackend, llm_reply, upload
from nanovoice.backends import BackendError, BackendTimeout


def test_transcribe_text_is_default(make_client, audio_dir):
    c = make_client()
    r = c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "charset=utf-8" in r.headers["content-type"]
    assert r.text == "Hello world. 你好世界。"
    assert r.headers["x-request-id"]


def test_transcribe_json(make_client, audio_dir):
    c = make_client()
    r = c.post("/transcribe", data={"format": "json", "language": "zh"}, files=upload(audio_dir / "speech.m4a"))
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Hello world. 你好世界。"
    assert body["raw_text"] == "hello world 你好世界"
    assert body["cleaned"] is True
    assert body["language"] == "zh"
    assert body["backend"] == "remote" and body["model"] == "fake-model"
    assert 1900 <= body["duration_ms"] <= 2600
    assert set(body["timings"]) >= {"decode_ms", "queue_ms", "asr_ms", "clean_ms", "total_ms"}
    assert c.backend.calls[0]["language"] == "zh"


def test_format_via_query_string(make_client, audio_dir):
    r = make_client().post("/transcribe?format=json", files=upload(audio_dir / "speech.wav"))
    assert r.json()["cleaned"] is True


def test_language_defaults_to_auto(make_client, audio_dir):
    c = make_client()
    r = c.post("/transcribe", data={"format": "json"}, files=upload(audio_dir / "speech.wav"))
    assert r.json()["language"] is None
    assert c.backend.calls[0]["language"] is None


def test_default_format_configurable(make_client, audio_dir):
    c = make_client({"transcribe": {"default_format": "json"}})
    assert c.post("/transcribe", files=upload(audio_dir / "speech.wav")).json()["text"]


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic dGVzdA==", "Bearer "])
def test_auth_failure(make_client, audio_dir, header):
    c = make_client(auth=False)
    headers = {"Authorization": header} if header else {}
    r = c.post("/transcribe", files=upload(audio_dir / "speech.wav"), headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "Bearer"
    assert c.backend.calls == []


def test_health_needs_no_auth(make_client):
    r = make_client(auth=False).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["backend"]["default"] == "remote" and body["backend"]["model"] == "fake-model"


def test_auth_none(make_client, audio_dir):
    c = make_client({"auth": {"mode": "none"}}, auth=False)
    assert c.post("/transcribe", files=upload(audio_dir / "speech.wav")).status_code == 200


def test_tailscale_auth(make_client, audio_dir):
    c = make_client({"auth": {"mode": "tailscale", "tailscale_users": ["Me@Example.com"]}}, auth=False)
    files = upload(audio_dir / "speech.wav")
    assert c.post("/transcribe", files=files).status_code == 401
    r = c.post("/transcribe", files=files, headers={"Tailscale-User-Login": "eve@example.com"})
    assert r.status_code == 403
    r = c.post("/transcribe", files=files, headers={"Tailscale-User-Login": "me@example.com"})
    assert r.status_code == 200


def test_file_too_large(make_client, audio_dir):
    c = make_client({"server": {"max_upload_mb": 0.01}})  # ~10 KB; speech.wav is ~176 KB
    r = c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "file_too_large"
    assert c.backend.calls == []


def test_file_too_large_chunked(make_client, audio_dir):
    """No Content-Length (chunked upload) must still be cut off."""
    c = make_client({"server": {"max_upload_mb": 0.01}})
    body = b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n\r\n" + \
        (audio_dir / "speech.wav").read_bytes() + b"\r\n--b--\r\n"

    def chunks():
        for i in range(0, len(body), 8192):
            yield body[i:i + 8192]

    r = c.post("/transcribe", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413


def test_unsupported_media(make_client, audio_dir):
    r = make_client().post("/transcribe", files=upload(audio_dir / "notes.txt"))
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media"


def test_missing_file(make_client):
    r = make_client().post("/transcribe", data={"language": "en"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_file"


@pytest.mark.parametrize("data", [{"format": "xml"}, {"clean": "maybe"}, {"backend": "nope"}])
def test_bad_parameters(make_client, audio_dir, data):
    r = make_client().post("/transcribe", data=data, files=upload(audio_dir / "speech.wav"))
    assert r.status_code == 400


def test_local_backend_unavailable(make_client, audio_dir):
    r = make_client().post("/transcribe", data={"backend": "local"}, files=upload(audio_dir / "speech.wav"))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "backend_unavailable"


def _llm_raises(req):
    raise httpx.ConnectError("connection refused")


def _llm_timeout(req):
    raise httpx.ReadTimeout("timed out")


@pytest.mark.parametrize("llm", [
    _llm_raises,
    _llm_timeout,
    lambda req: httpx.Response(500, json={"error": {"message": "boom"}}),
    lambda req: httpx.Response(200, json={"unexpected": True}),
    llm_reply(""),
    llm_reply("Sure! Here is a long essay about hello world. " * 20),  # answered instead of cleaning
])
def test_llm_failure_falls_back_to_raw(make_client, audio_dir, llm):
    c = make_client(llm=llm)
    r = c.post("/transcribe", data={"format": "json"}, files=upload(audio_dir / "speech.wav"))
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == body["raw_text"] == "hello world 你好世界"
    assert body["cleaned"] is False


def test_llm_request_shape(make_client, audio_dir):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think><transcript>ok 好</transcript>"}}]})

    c = make_client(llm=handler)
    r = c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    assert r.text == "ok 好"
    assert seen["temperature"] == 0
    assert seen["messages"][0]["role"] == "system" and "不要回答" in seen["messages"][0]["content"]
    assert "hello world 你好世界" in seen["messages"][1]["content"]


def test_clean_opt_out_and_short_text(make_client, audio_dir):
    c = make_client()
    r = c.post("/transcribe", data={"clean": "false", "format": "json"}, files=upload(audio_dir / "speech.wav"))
    assert r.json()["cleaned"] is False
    c = make_client(backend=FakeBackend(text="好的"))
    r = c.post("/transcribe", data={"format": "json"}, files=upload(audio_dir / "speech.wav"))
    body = r.json()
    assert body["text"] == "好的" and body["cleaned"] is False  # below min_chars: LLM not called
    assert body["timings"]["clean_ms"] < 50


def test_vocabulary_prompt_and_replacements(make_client, audio_dir):
    c = make_client({
        "replacements": [
            {"pattern": "hello", "replacement": "Hi"},
            {"pattern": r"world\s+", "replacement": "World! ", "regex": True},
        ],
    })
    c.state.dictionary.replace({"terms": ["Tailscale", "Claude Code"]})
    r = c.post("/transcribe", data={"prompt": "nanovoice", "clean": "0", "format": "json"},
               files=upload(audio_dir / "speech.wav"))
    assert c.backend.calls[0]["prompt"] == "Tailscale, Claude Code nanovoice"
    assert r.json()["text"] == "Hi World! 你好世界"
    assert r.json()["raw_text"] == "hello world 你好世界"


def test_silence_skips_asr(make_client, audio_dir):
    c = make_client()
    r = c.post("/transcribe", data={"format": "json"}, files=upload(audio_dir / "silence.wav"))
    assert r.status_code == 200
    assert r.json()["text"] == ""
    assert c.backend.calls == []


@pytest.mark.parametrize("error,status,code", [
    (BackendError("upstream 500"), 502, "asr_failed"),
    (BackendTimeout("slow"), 504, "asr_timeout"),
])
def test_backend_errors(make_client, audio_dir, error, status, code):
    c = make_client(backend=FakeBackend(error=error))
    r = c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_openai_compat(make_client, audio_dir):
    c = make_client()
    files = upload(audio_dir / "speech.m4a")
    r = c.post("/v1/audio/transcriptions", data={"model": "whisper-1"}, files=files)
    assert r.status_code == 200
    assert r.json() == {"text": "hello world 你好世界"}  # no LLM cleanup by default
    r = c.post("/v1/audio/transcriptions", data={"model": "whisper-1", "response_format": "text", "language": "en"},
               files=files)
    assert r.text == "hello world 你好世界"
    assert c.backend.calls[-1]["language"] == "en"
    r = c.post("/v1/audio/transcriptions", data={"response_format": "verbose_json"}, files=files)
    assert r.json()["text"] and r.json()["duration"] > 1
    r = c.post("/v1/audio/transcriptions", data={"response_format": "srt"}, files=files)
    assert r.status_code == 400


def test_openai_compat_clean_configurable(make_client, audio_dir):
    c = make_client({"openai_compat": {"clean": True}})
    r = c.post("/v1/audio/transcriptions", files=upload(audio_dir / "speech.wav"))
    assert r.json() == {"text": "Hello world. 你好世界。"}


def test_base_path(make_client, audio_dir):
    c = make_client({"server": {"base_path": "/stt"}})
    assert c.get("/stt/health").status_code == 200
    assert c.post("/stt/transcribe", files=upload(audio_dir / "speech.wav")).status_code == 200
    # also reachable without the prefix (tailscale serve may strip it)
    assert c.get("/health").status_code == 200
    assert c.get("/sttx/health").status_code == 404


def test_not_found_is_json(make_client):
    r = make_client().get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_concurrency_is_serialized(make_client, audio_dir):
    backend = FakeBackend(delay=0.3)
    c = make_client(backend=backend)
    data = (audio_dir / "speech.wav").read_bytes()
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda _: c.post("/transcribe", files={"file": ("a.wav", data)}), range(4)))
    assert [r.status_code for r in results] == [200] * 4
    assert backend.max_active == 1


def test_queue_timeout_returns_503(make_client, audio_dir):
    backend = FakeBackend(delay=0.8)
    c = make_client({"server": {"queue_timeout_s": 0.1}}, backend=backend)
    data = (audio_dir / "speech.wav").read_bytes()
    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(lambda _: c.post("/transcribe", files={"file": ("a.wav", data)}), range(2)))
    assert sorted(r.status_code for r in results) == [200, 503]
