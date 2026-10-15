"""stt-server: HTTP API around an ASR backend + post-processing pipeline."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__
from .audio import AudioError, ffmpeg_version, prepare
from .backends import BACKEND_NAMES, Backend, BackendError, BackendTimeout, BackendUnavailable, create_backend
from .config import Config, ConfigError, load_config
from .log import setup_logging
from .postprocess import Cleaner, Replacer, build_prompt

log = logging.getLogger("nanovoice")

MIB = 1024 * 1024
FORM_SLACK_BYTES = MIB  # room for non-file multipart fields on top of the file limit
MIN_SPEECH_MS = 200  # below this (after VAD) we treat the upload as silence and skip ASR


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class BodyTooLarge(Exception):
    pass


def error_response(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


# --------------------------------------------------------------------------- ASGI gateway


class Gateway:
    """Strips the optional base path, enforces the body size limit, assigns a request id and logs access."""

    def __init__(self, app, base_path: str, max_body: int):
        self.app, self.base_path, self.max_body = app, base_path, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        bp, path = self.base_path, scope["path"]
        if bp and (path == bp or path.startswith(bp + "/")):
            scope = {**scope, "path": path[len(bp):] or "/"}
        rid = uuid.uuid4().hex[:12]
        scope.setdefault("state", {})["request_id"] = rid
        start, status = time.perf_counter(), 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            cl = dict(scope["headers"]).get(b"content-length", b"")
            if cl.isdigit() and int(cl) > self.max_body:
                resp = error_response(413, "file_too_large", f"request body exceeds {self.max_body} bytes")
                return await resp(scope, receive, send_wrapper)

            received = 0

            async def limited_receive():
                nonlocal received
                msg = await receive()
                if msg["type"] == "http.request":
                    received += len(msg.get("body", b""))
                    if received > self.max_body:
                        raise BodyTooLarge()
                return msg

            await self.app(scope, limited_receive, send_wrapper)
        finally:
            client = scope.get("client")
            log.info("request", extra={"fields": {
                "request_id": rid, "method": scope["method"], "path": scope["path"], "status": status,
                "ms": round((time.perf_counter() - start) * 1000), "client": client[0] if client else None,
            }})


# --------------------------------------------------------------------------- state & pipeline


@dataclass
class Result:
    text: str
    raw_text: str
    language: str | None
    duration_ms: int | None
    backend: str
    model: str
    cleaned: bool
    timings: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "text": self.text, "raw_text": self.raw_text, "language": self.language,
            "duration_ms": self.duration_ms, "backend": self.backend, "model": self.model,
            "cleaned": self.cleaned, "timings": self.timings,
        }


class AppState:
    def __init__(self, cfg: Config, backends: dict[str, Backend] | None, cleaner: Cleaner | None):
        self.cfg = cfg
        self.backends: dict[str, Backend] = dict(backends or {})
        self._backend_lock = threading.Lock()
        self.cleaner = cleaner or Cleaner(cfg.llm)
        self.replacer = Replacer(cfg.replacements)
        self.slots = threading.BoundedSemaphore(cfg.server.max_concurrency)
        self.in_flight = 0
        self.token = os.environ.get(cfg.auth.token_env, "") if cfg.auth.mode == "token" else ""
        self.tailscale_users = {u.lower() for u in cfg.auth.tailscale_users}
        try:
            self.ffmpeg = ffmpeg_version()
        except RuntimeError:
            self.ffmpeg = None

    def backend(self, name: str) -> Backend:
        if name not in BACKEND_NAMES:
            raise ApiError(400, "invalid_backend", f"unknown backend {name!r}; available: {', '.join(BACKEND_NAMES)}")
        with self._backend_lock:
            if name not in self.backends:
                try:
                    self.backends[name] = create_backend(name, self.cfg)
                except BackendUnavailable as e:
                    raise ApiError(503, "backend_unavailable", str(e)) from e
            return self.backends[name]

    def run_asr(self, backend: Backend, path: Path, language: str | None, prompt: str | None, timings: dict) -> str:
        """Serialize access to the model: at most `max_concurrency` transcriptions at once."""
        t = time.perf_counter()
        if not self.slots.acquire(timeout=self.cfg.server.queue_timeout_s):
            raise ApiError(503, "busy", "server is busy, try again later")
        timings["queue_ms"] = _ms_since(t)
        self.in_flight += 1
        try:
            t = time.perf_counter()
            return backend.transcribe(path, language, prompt)
        finally:
            timings["asr_ms"] = _ms_since(t)
            self.in_flight -= 1
            self.slots.release()


def _ms_since(t: float) -> int:
    return round((time.perf_counter() - t) * 1000)


def _safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ""


def _parse_bool(value, name: str) -> bool | None:
    if value is None or value == "":
        return None
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ApiError(400, "invalid_parameter", f"{name} must be a boolean")


def _norm_language(value) -> str | None:
    v = (value or "").strip()
    return None if v.lower() in ("", "auto") else v


async def _read_form(request: Request, state: AppState):
    try:
        form = await request.form(max_files=2, max_fields=32)
    except BodyTooLarge:
        raise
    except HTTPException as e:
        raise ApiError(400, "invalid_request", f"could not parse form data: {e.detail}") from e
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise ApiError(400, "missing_file", "multipart/form-data field 'file' is required")
    limit = int(state.cfg.server.max_upload_mb * MIB)
    if upload.size is not None and upload.size > limit:
        raise ApiError(413, "file_too_large", f"file exceeds {state.cfg.server.max_upload_mb} MB")
    return form, upload


async def run_pipeline(request: Request, state: AppState, upload: UploadFile, *, language: str | None,
                       prompt: str | None, clean: bool, backend_name: str, endpoint: str) -> Result:
    cfg = state.cfg
    t_total = time.perf_counter()
    timings: dict = {"decode_ms": 0, "queue_ms": 0, "asr_ms": 0, "clean_ms": 0}
    backend = state.backend(backend_name)
    asr_prompt = build_prompt(cfg.asr.vocabulary, prompt)

    with tempfile.TemporaryDirectory(prefix="stt-") as tmp:
        src = Path(tmp) / ("input" + _safe_suffix(upload.filename))
        dst = Path(tmp) / "audio.flac"

        def save():
            upload.file.seek(0)
            with src.open("wb") as f:
                shutil.copyfileobj(upload.file, f)

        await run_in_threadpool(save)
        await upload.close()
        t = time.perf_counter()
        try:
            audio = await run_in_threadpool(prepare, src, dst, cfg.audio)
        except AudioError as e:
            raise ApiError(415, "unsupported_media", str(e)) from e
        timings["decode_ms"] = _ms_since(t)

        if audio.speech_ms < MIN_SPEECH_MS:
            raw = ""  # nothing but silence: skip ASR (and avoid hallucinated text)
        else:
            try:
                raw = await run_in_threadpool(state.run_asr, backend, audio.path, language, asr_prompt, timings)
            except BackendTimeout as e:
                raise ApiError(504, "asr_timeout", str(e)) from e
            except BackendUnavailable as e:
                raise ApiError(503, "backend_unavailable", str(e)) from e
            except BackendError as e:
                raise ApiError(502, "asr_failed", str(e)) from e

    text = state.replacer.apply(raw)
    cleaned, skipped = False, None
    if clean and text:
        t = time.perf_counter()
        res = await run_in_threadpool(state.cleaner.clean, text)
        timings["clean_ms"] = _ms_since(t)
        text, cleaned, skipped = res.text, res.cleaned, res.skipped
    timings["total_ms"] = _ms_since(t_total)

    fields = {
        "request_id": request.state.request_id, "endpoint": endpoint, "backend": backend.name,
        "model": backend.model, "language": language, "bytes": upload.size, "input_ms": audio.input_ms,
        "speech_ms": audio.speech_ms, "chars": len(text), "cleaned": cleaned, "clean_skipped": skipped,
        "timings": timings,
    }
    if cfg.server.log_transcripts:
        fields.update(raw_text=raw, text=text)
    log.info("transcribed", extra={"fields": fields})
    return Result(text=text, raw_text=raw, language=language, duration_ms=audio.input_ms,
                  backend=backend.name, model=backend.model, cleaned=cleaned, timings=timings)


# --------------------------------------------------------------------------- endpoints


def check_auth(request: Request, state: AppState) -> None:
    mode = state.cfg.auth.mode
    if mode == "none":
        return
    if mode == "token":
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "unauthorized", "missing bearer token")
        if not hmac.compare_digest(token.strip().encode(), state.token.encode()):
            raise ApiError(401, "unauthorized", "invalid bearer token")
        return
    login = request.headers.get("tailscale-user-login", "").strip().lower()
    if not login:
        raise ApiError(401, "unauthorized", "missing Tailscale-User-Login header (use tailscale serve)")
    if state.tailscale_users and login not in state.tailscale_users:
        raise ApiError(403, "forbidden", f"tailscale user {login!r} is not allowed")


async def health(request: Request) -> Response:
    state: AppState = request.app.state.stt
    cfg = state.cfg
    default = cfg.asr.backend
    b = state.backends.get(default)
    return JSONResponse({
        "status": "ok" if state.ffmpeg and b else "degraded",
        "version": __version__,
        "backend": {
            "default": default,
            "model": b.model if b else None,
            "base_url": getattr(b, "base_url", None),
            "loaded": sorted(state.backends),
            "available": list(BACKEND_NAMES),
        },
        "llm": {"enabled": cfg.llm.enabled, "model": cfg.llm.model if cfg.llm.enabled else None},
        "ffmpeg": state.ffmpeg,
        "auth": cfg.auth.mode,
        "queue": {"max_concurrency": cfg.server.max_concurrency, "in_flight": state.in_flight},
    })


async def transcribe(request: Request) -> Response:
    state: AppState = request.app.state.stt
    cfg = state.cfg
    check_auth(request, state)
    form, upload = await _read_form(request, state)
    q = request.query_params

    def param(name):  # form field wins over query string
        v = form.get(name)
        return v if isinstance(v, str) else q.get(name)

    fmt = (param("format") or cfg.transcribe.default_format).strip().lower()
    if fmt not in ("text", "json"):
        raise ApiError(400, "invalid_parameter", "format must be 'text' or 'json'")
    clean = _parse_bool(param("clean"), "clean")
    lang = param("language")
    result = await run_pipeline(
        request, state, upload,
        language=_norm_language(lang if lang is not None else cfg.asr.language),
        prompt=param("prompt"),
        clean=cfg.transcribe.clean if clean is None else clean,
        backend_name=(param("backend") or cfg.asr.backend).strip().lower(),
        endpoint="transcribe",
    )
    if fmt == "text":
        return PlainTextResponse(result.text)
    return JSONResponse(result.as_dict())


async def openai_transcriptions(request: Request) -> Response:
    state: AppState = request.app.state.stt
    cfg = state.cfg
    check_auth(request, state)
    form, upload = await _read_form(request, state)
    fmt = str(form.get("response_format") or "json").strip().lower()
    if fmt not in ("json", "text", "verbose_json"):
        raise ApiError(400, "invalid_parameter", "response_format must be json, text or verbose_json")
    lang = form.get("language")
    prompt = form.get("prompt")
    # `model` is accepted for compatibility but the configured backend/model is always used.
    result = await run_pipeline(
        request, state, upload,
        language=_norm_language(lang if isinstance(lang, str) else cfg.asr.language),
        prompt=prompt if isinstance(prompt, str) else None,
        clean=cfg.openai_compat.clean,
        backend_name=cfg.asr.backend,
        endpoint="openai",
    )
    if fmt == "text":
        return PlainTextResponse(result.text)
    if fmt == "verbose_json":
        return JSONResponse({
            "task": "transcribe", "language": result.language,
            "duration": (result.duration_ms or 0) / 1000, "text": result.text,
        })
    return JSONResponse({"text": result.text})


# --------------------------------------------------------------------------- error handlers


async def _api_error(request: Request, exc: ApiError) -> Response:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 and request.app.state.stt.cfg.auth.mode == "token" else None
    return error_response(exc.status, exc.code, exc.message, headers)


async def _too_large(request: Request, exc: BodyTooLarge) -> Response:
    return error_response(413, "file_too_large", f"file exceeds {request.app.state.stt.cfg.server.max_upload_mb} MB")


_HTTP_CODES = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed"}


async def _http_error(request: Request, exc: HTTPException) -> Response:
    return error_response(exc.status_code, _HTTP_CODES.get(exc.status_code, "http_error"), str(exc.detail))


async def _internal_error(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error", extra={"fields": {"request_id": request.state.request_id}})
    return error_response(500, "internal_error", "internal server error")


def create_app(cfg: Config, backends: dict[str, Backend] | None = None, cleaner: Cleaner | None = None):
    """Build the ASGI app. `backends`/`cleaner` let tests inject fakes."""
    state = AppState(cfg, backends, cleaner)
    state.backend(cfg.asr.backend)  # instantiate the default backend once, at startup
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/transcribe", transcribe, methods=["POST"]),
            Route("/v1/audio/transcriptions", openai_transcriptions, methods=["POST"]),
        ],
        exception_handlers={
            ApiError: _api_error, BodyTooLarge: _too_large, HTTPException: _http_error, Exception: _internal_error,
        },
    )
    app.state.stt = state
    max_body = int(cfg.server.max_upload_mb * MIB) + FORM_SLACK_BYTES
    return Gateway(app, cfg.server.base_path, max_body)


# --------------------------------------------------------------------------- startup


def tailscale_info() -> dict | None:
    exe = shutil.which("tailscale")
    mac = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not exe and os.path.exists(mac):
        exe = mac
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3)
        me = json.loads(out.stdout).get("Self") or {}
    except Exception:
        return None
    return {"dns_name": (me.get("DNSName") or "").rstrip(".") or None, "ips": me.get("TailscaleIPs") or []}


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return None


def _url_host(h: str) -> str:
    return f"[{h}]" if ":" in h else h


def access_urls(cfg: Config, ts: dict | None) -> list[str]:
    host, port, bp = cfg.server.host, cfg.server.port, cfg.server.base_path
    if host in ("0.0.0.0", "::", ""):
        hosts = ["127.0.0.1"]
        if lan := _lan_ip():
            hosts.append(lan)
        if ts:
            hosts += [ip for ip in ts["ips"] if "." in ip]
            if ts["dns_name"]:
                hosts.append(ts["dns_name"])
    else:
        hosts = [host]
        if ts and host in ts["ips"] and ts["dns_name"]:
            hosts.append(ts["dns_name"])
    seen = []
    for h in hosts:
        url = f"http://{_url_host(h)}:{port}{bp}"
        if url not in seen:
            seen.append(url)
    return seen


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="stt-server", description="Speech-to-text HTTP server")
    ap.add_argument("--config", help="config file (default: $STT_CONFIG or ~/.config/stt/config.toml)")
    ap.add_argument("--host", help="override server.host")
    ap.add_argument("--port", type=int, help="override server.port")
    args = ap.parse_args(argv)

    try:
        cfg, cfg_path = load_config(args.config)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    setup_logging(cfg.server.log_level)

    def die(msg: str):
        log.error(msg)
        sys.exit(1)

    try:
        ffmpeg = ffmpeg_version()
    except RuntimeError as e:
        die(str(e))
    if cfg.auth.mode == "token" and not os.environ.get(cfg.auth.token_env):
        die(f"auth.mode=token but ${cfg.auth.token_env} is not set (or set auth.mode='none')")
    if cfg.auth.mode == "tailscale" and not _is_loopback(cfg.server.host):
        die("auth.mode=tailscale requires server.host=127.0.0.1, otherwise the Tailscale-User-Login header can be forged")
    if cfg.llm.enabled and cfg.llm.api_key_env and not os.environ.get(cfg.llm.api_key_env):
        log.warning(f"llm.enabled but ${cfg.llm.api_key_env} is not set; requests will likely fail and fall back to raw text")
    if cfg.asr.backend == "remote" and cfg.asr.remote.api_key_env and not os.environ.get(cfg.asr.remote.api_key_env):
        log.warning(f"${cfg.asr.remote.api_key_env} is not set; the remote ASR may reject requests")

    try:
        app = create_app(cfg)
    except (ApiError, ValueError) as e:
        die(f"cannot start backend {cfg.asr.backend!r}: {getattr(e, 'message', e)}")

    ts = tailscale_info()
    urls = access_urls(cfg, ts)
    log.info("starting", extra={"fields": {
        "version": __version__, "config": str(cfg_path) if cfg_path else None, "ffmpeg": ffmpeg,
        "backend": cfg.asr.backend, "model": app.app.state.stt.backends[cfg.asr.backend].model,
        "llm": cfg.llm.model if cfg.llm.enabled else None, "auth": cfg.auth.mode,
        "tailscale": ts["dns_name"] if ts else None, "urls": urls,
    }})
    print("\nstt-server is reachable at:", file=sys.stderr)
    for u in urls:
        print(f"  {u}", file=sys.stderr)
    if ts and ts["dns_name"]:
        print(f"  (Tailscale MagicDNS: {ts['dns_name']}; with `tailscale serve`: https://{ts['dns_name']}{cfg.server.base_path})",
              file=sys.stderr)
    print(file=sys.stderr)

    import uvicorn

    uvicorn.run(
        app, host=cfg.server.host, port=cfg.server.port, log_config=None, access_log=False,
        proxy_headers=True, forwarded_allow_ips=cfg.server.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
