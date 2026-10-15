"""stt: thin CLI client for stt-server."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import httpx

from .config import ConfigError, load_config
from .dictionary import dumps as dict_dumps


def _fail(msg: str, code: int = 1):
    print(f"stt: {msg}", file=sys.stderr)
    sys.exit(code)


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json()["error"]
        return f"{resp.status_code} {err['code']}: {err['message']}"
    except Exception:
        return f"{resp.status_code}: {resp.text[:300]}"


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
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

    h = sub.add_parser("history", parents=[common], help="show recent transcriptions (needs history.enabled)")
    h.add_argument("-n", type=int, default=10, help="number of entries (default 10)")
    h.add_argument("--json", action="store_true")

    c = sub.add_parser("correct", parents=[common],
                       help="give the corrected text of a past transcription; the server learns corrections from it")
    c.add_argument("id", help="history entry id (see `stt history`)")
    c.add_argument("text", help="the corrected text ('-' to read from stdin)")

    sub.add_parser("learn", parents=[common], help="learn dictionary entries from recent history now")

    d = sub.add_parser("dict", parents=[common], help="view / edit the server's dictionary")
    dsub = d.add_subparsers(dest="dict_cmd", required=True)
    dsub.add_parser("show", parents=[common], help="print the dictionary")
    a = dsub.add_parser("add", parents=[common], help="add terms (proper nouns, jargon)")
    a.add_argument("terms", nargs="+")
    r = dsub.add_parser("rm", parents=[common], help="remove terms (and stop the learner re-adding them)")
    r.add_argument("terms", nargs="+")
    r.add_argument("--no-ignore", action="store_true", help="don't add them to the ignore list")
    f = dsub.add_parser("fix", parents=[common], help="add/replace a correction: WRONG is replaced by RIGHT")
    f.add_argument("wrong")
    f.add_argument("right")
    f.add_argument("--no-term", action="store_true", help="don't also add RIGHT as a term")
    u = dsub.add_parser("unfix", parents=[common], help="remove corrections by their WRONG text")
    u.add_argument("wrong", nargs="+")
    u.add_argument("--no-ignore", action="store_true", help="don't add them to the ignore list")
    dsub.add_parser("edit", parents=[common], help="edit the whole dictionary in $EDITOR")
    return ap


def _edit_dictionary(client: httpx.Client, server: str) -> None:
    resp = client.get(f"{server}/dictionary")
    if resp.status_code != 200:
        _fail(_error_message(resp))
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dictionary.toml"
        path.write_text(dict_dumps(resp.json()), encoding="utf-8")
        while True:
            if subprocess.run([*editor.split(), str(path)]).returncode != 0:
                _fail("editor exited with an error; nothing saved")
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                msg = f"invalid TOML: {e}"
            else:
                resp = client.put(f"{server}/dictionary", json=data)
                if resp.status_code == 200:
                    c = resp.json()
                    print(f"stt: saved ({len(c['terms'])} terms, {len(c['corrections'])} corrections)", file=sys.stderr)
                    return
                msg = _error_message(resp)
            print(f"stt: {msg}", file=sys.stderr)
            if input("re-edit? [Y/n] ").strip().lower() in ("n", "no"):
                _fail("nothing saved")


def _run(args, client: httpx.Client, server: str) -> None:
    def call(method: str, path: str, **kw) -> httpx.Response:
        resp = client.request(method, f"{server}{path}", **kw)
        if resp.status_code != 200:
            _fail(_error_message(resp))
        return resp

    if args.cmd == "health":
        _print_json(call("GET", "/health").json())

    elif args.cmd == "transcribe":
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
        resp = call("POST", "/transcribe", data=data, files={"file": (name, audio)})
        if args.json:
            _print_json(resp.json())
        else:
            print(resp.text)
            print(f"stt: id {resp.headers.get('x-request-id', '?')}", file=sys.stderr)

    elif args.cmd == "history":
        entries = call("GET", "/history", params={"limit": args.n}).json()["entries"]
        if args.json:
            _print_json(entries)
            return
        for e in reversed(entries):
            print(f"[{e.get('id')}] {e.get('ts', '')}\n  {e.get('text', '')}")
            if e.get("raw_text") != e.get("text"):
                print(f"  (raw: {e.get('raw_text', '')})")

    elif args.cmd == "correct":
        text = sys.stdin.read() if args.text == "-" else args.text
        res = call("POST", f"/history/{args.id}/correct", json={"text": text}).json()
        for c in res["added"]["corrections"]:
            print(f"+ fix  {c['wrong']} -> {c['right']}")
        for t in res["added"]["terms"]:
            print(f"+ term {t}")
        for s in res["skipped"]:
            print(f"  skip {s['wrong'] or '∅'} -> {s['right'] or '∅'}  ({s['reason']})")
        if not res["added"]["corrections"] and not res["skipped"]:
            print("no changes found")

    elif args.cmd == "learn":
        print("stt: learning from recent history...", file=sys.stderr)
        res = call("POST", "/dictionary/learn").json()
        for t in res["added"]["terms"]:
            print(f"+ term {t}")
        for c in res["added"]["corrections"]:
            print(f"+ fix  {c['wrong']} -> {c['right']}")
        print(f"stt: looked at {res['entries']} entries, added {len(res['added']['terms'])} terms "
              f"and {len(res['added']['corrections'])} corrections", file=sys.stderr)

    elif args.cmd == "dict":
        dc = args.dict_cmd
        if dc == "show":
            print(dict_dumps(call("GET", "/dictionary").json()), end="")
        elif dc == "edit":
            _edit_dictionary(client, server)
        else:
            if dc == "add":
                body = {"add_terms": args.terms}
            elif dc == "rm":
                body = {"remove_terms": args.terms, "ignore_removed": not args.no_ignore}
            elif dc == "fix":
                body = {"add_corrections": [{"wrong": args.wrong, "right": args.right}],
                        "add_terms": [] if args.no_term else [args.right]}
            else:  # unfix
                body = {"remove_corrections": args.wrong, "ignore_removed": not args.no_ignore}
            d = call("PATCH", "/dictionary", json=body).json()
            print(f"stt: dictionary now has {len(d['terms'])} terms, {len(d['corrections'])} corrections",
                  file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        cfg, _ = load_config(getattr(args, "config", None))
    except ConfigError as e:
        _fail(f"config error: {e}")
    server = (getattr(args, "server", None) or os.environ.get("STT_SERVER") or cfg.client.server).rstrip("/")
    token = os.environ.get(cfg.client.token_env)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=cfg.client.timeout_s, headers=headers) as client:
            _run(args, client, server)
    except httpx.HTTPError as e:
        _fail(f"cannot reach {server}: {e}", 2)


if __name__ == "__main__":
    main()
