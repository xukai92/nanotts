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
    server = make_client({"history": {"enabled": True}, "learn": {"auto": False}}, auth=False)
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


def test_cli_dict_commands(run_cli):
    assert run_cli("dict", "add", "Tailscale", "MagicDNS")[0] == 0
    assert run_cli("dict", "fix", "cloud code", "Claude Code")[0] == 0
    assert run_cli("dict", "rm", "MagicDNS")[0] == 0
    code, out, _, _ = run_cli("dict", "show")
    assert code == 0
    import tomllib
    assert tomllib.loads(out) == {
        "terms": ["Tailscale", "Claude Code"], "ignore": ["MagicDNS"],
        "corrections": [{"wrong": "cloud code", "right": "Claude Code"}],
    }
    assert run_cli("dict", "unfix", "cloud code", "--no-ignore")[0] == 0
    assert "corrections" not in tomllib.loads(run_cli("dict", "show")[1])


def test_cli_dict_edit(run_cli, monkeypatch, tmp_path):
    script = tmp_path / "ed.sh"
    script.write_text('#!/bin/sh\nprintf \'terms = ["Edited"]\\n\' > "$1"\n')
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    code, _, err, _ = run_cli("dict", "edit")
    assert code == 0 and "saved (1 terms" in err
    assert "Edited" in run_cli("dict", "show")[1]


def test_cli_history_and_correct(run_cli, audio_dir):
    code, out, err, _ = run_cli("transcribe", str(audio_dir / "speech.wav"), "--no-clean")
    entry_id = err.split("stt: id ")[1].strip()
    code, out, _, _ = run_cli("history", "-n", "5")
    assert code == 0 and f"[{entry_id}]" in out and "hello world 你好世界" in out
    code, out, _, _ = run_cli("correct", entry_id, "hello world 你好世界, Tailscale")
    assert code == 0
    code, out, _, _ = run_cli("correct", entry_id, "hallo world 你好世界")
    assert "+ fix  hello -> hallo" in out
    code, _, err, _ = run_cli("correct", "nope", "x")
    assert code == 1 and "404" in err
