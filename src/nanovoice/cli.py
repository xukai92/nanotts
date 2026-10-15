"""stt: thin CLI client. Uploads audio to stt-server and prints the result."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from .config import ConfigError, load_config


def _fail(msg: str, code: int = 1):
    print(f"stt: {msg}", file=sys.stderr)
    sys.exit(code)


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json()["error"]
        return f"{resp.status_code} {err['code']}: {err['message']}"
    except Exception:
        return f"{resp.status_code}: {resp.text[:300]}"


def main(argv: list[str] | None = None) -> None:
    # --server/--config work both before and after the subcommand (SUPPRESS keeps one from clobbering the other).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="server URL (default: $STT_SERVER or [client].server in config)")
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="config file (default: $STT_CONFIG or ~/.config/stt/config.toml)")

    ap = argparse.ArgumentParser(prog="stt", description="Speech-to-text client for stt-server", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe", parents=[common], help="transcribe an audio file ('-' for stdin)")
    t.add_argument("file")
    t.add_argument("--clean", action=argparse.BooleanOptionalAction, default=None,
                   help="LLM cleanup on/off (default: server setting)")
    t.add_argument("--language", help="language code, e.g. zh / en (default: auto)")
    t.add_argument("--backend", help="ASR backend override, e.g. remote")
    t.add_argument("--prompt", help="extra vocabulary / context for ASR")
    t.add_argument("--json", action="store_true", help="print the full JSON response")
    sub.add_parser("health", parents=[common], help="show server status")
    args = ap.parse_args(argv)
    args.server = getattr(args, "server", None)
    args.config = getattr(args, "config", None)

    try:
        cfg, _ = load_config(args.config)
    except ConfigError as e:
        _fail(f"config error: {e}")
    server = (args.server or os.environ.get("STT_SERVER") or cfg.client.server).rstrip("/")
    token = os.environ.get(cfg.client.token_env)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        with httpx.Client(timeout=cfg.client.timeout_s, headers=headers) as client:
            if args.cmd == "health":
                resp = client.get(f"{server}/health")
                if resp.status_code != 200:
                    _fail(_error_message(resp))
                print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
                return

            if args.file == "-":
                audio, name = sys.stdin.buffer.read(), "stdin"
            else:
                p = Path(args.file)
                if not p.is_file():
                    _fail(f"no such file: {p}")
                audio, name = p.read_bytes(), p.name
            if not audio:
                _fail("empty audio input")

            data = {"format": "json" if args.json else "text"}
            if args.clean is not None:
                data["clean"] = "true" if args.clean else "false"
            for key in ("language", "backend", "prompt"):
                if getattr(args, key):
                    data[key] = getattr(args, key)
            print(f"stt: uploading {len(audio)} bytes to {server}", file=sys.stderr)
            resp = client.post(f"{server}/transcribe", data=data, files={"file": (name, audio)})
    except httpx.HTTPError as e:
        _fail(f"cannot reach {server}: {e}", 2)

    if resp.status_code != 200:
        _fail(_error_message(resp))
    if args.json:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    else:
        print(resp.text)


if __name__ == "__main__":
    main()
