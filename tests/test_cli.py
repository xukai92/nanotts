import io
import json
import sys

import pytest
from starlette.testclient import TestClient

from conftest import TOKEN
from nanovoice import cli


@pytest.fixture
def run_cli(make_client, monkeypatch, capsys):
    """Run `stt ...` with HTTP routed into an in-process server."""
    server = make_client(auth=False)
    seen = []

    def fake_client(timeout, headers):
        c = TestClient(server.app, headers=headers, raise_server_exceptions=False)
        orig = c.request

        def request(method, url, **kw):
            seen.append(str(url))
            return orig(method, url, **kw)

        c.request = request
        return c

    monkeypatch.setattr(cli.httpx, "Client", fake_client)
    monkeypatch.setenv("STT_TOKEN", TOKEN)

    def _run(*args, stdin: bytes | None = None):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
        code = 0
        try:
            cli.main(list(args))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        out, err = capsys.readouterr()
        return code, out, err, seen

    return _run


def test_cli_transcribe_text(run_cli, audio_dir):
    code, out, err, _ = run_cli("transcribe", str(audio_dir / "speech.m4a"))
    assert code == 0
    assert out == "Hello world. 你好世界。\n"
    assert "uploading" in err


def test_cli_stdin_json_no_clean(run_cli, audio_dir):
    code, out, _, _ = run_cli("transcribe", "-", "--json", "--no-clean", stdin=(audio_dir / "speech.wav").read_bytes())
    assert code == 0
    assert json.loads(out)["cleaned"] is False


@pytest.mark.parametrize("args", [["--server", "http://x:1", "health"], ["health", "--server", "http://x:1"]])
def test_cli_server_flag_any_position(run_cli, args):
    code, out, _, seen = run_cli(*args)
    assert code == 0
    assert json.loads(out)["status"] == "ok"
    assert seen[-1] == "http://x:1/health"


def test_cli_auth_error(run_cli, audio_dir, monkeypatch):
    monkeypatch.setenv("STT_TOKEN", "wrong")
    code, out, err, _ = run_cli("transcribe", str(audio_dir / "speech.wav"))
    assert code == 1 and out == ""
    assert "401 unauthorized" in err


def test_cli_missing_file(run_cli):
    code, _, err, _ = run_cli("transcribe", "/nonexistent.m4a")
    assert code == 1 and "no such file" in err
