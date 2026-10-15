"""Hand-written OpenAPI 3.1 description of the HTTP API, served at /openapi.json and /docs."""

from __future__ import annotations

from . import __version__

SWAGGER_UI_VERSION = "5.17.14"

_ERROR = {"$ref": "#/components/responses/Error"}
_AUTH = [{"bearer": []}, {"tailscale": []}]

_TEXT_FIELDS = {
    "language": {"type": "string", "description": "Language code (`zh`, `en`, ...). Omit or `auto` to auto-detect."},
    "prompt": {"type": "string", "description": "Extra context/vocabulary for ASR, appended after dictionary terms."},
}

_TRANSCRIBE_FORM = {
    "type": "object",
    "required": ["file"],
    "properties": {
        "file": {"type": "string", "format": "binary",
                 "description": "Audio: m4a (iOS Shortcuts), wav, mp3, ogg, flac, webm, ... (anything ffmpeg decodes)."},
        **_TEXT_FIELDS,
        "clean": {"type": "boolean", "description": "Run LLM cleanup. Default: `transcribe.clean`."},
        "backend": {"type": "string", "enum": ["remote", "local"], "description": "Override the ASR backend."},
        "format": {"type": "string", "enum": ["text", "json"],
                   "description": "`text` returns only the final text as text/plain. Default: `transcribe.default_format`."},
    },
}

_RESULT = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Request id (= X-Request-ID); history entry id for /history/{id}/correct."},
        "text": {"type": "string", "description": "Final text (after corrections, replacements and cleanup)."},
        "raw_text": {"type": "string", "description": "Untouched ASR output."},
        "language": {"type": ["string", "null"], "description": "Requested language; null when auto-detected."},
        "duration_ms": {"type": ["integer", "null"], "description": "Duration of the uploaded audio."},
        "backend": {"type": "string"},
        "model": {"type": "string"},
        "cleaned": {"type": "boolean", "description": "Whether LLM cleanup was applied (false on skip or fallback)."},
        "timings": {"type": "object", "additionalProperties": {"type": "integer"},
                    "description": "decode_ms, queue_ms, asr_ms, clean_ms, total_ms"},
    },
}

_DICTIONARY = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string"},
                  "description": "Proper nouns / jargon passed to ASR as prompt; earlier entries have priority."},
        "corrections": {"type": "array", "items": {
            "type": "object", "required": ["wrong", "right"],
            "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}},
        }, "description": "Case-insensitive literal replacements applied to the ASR output."},
        "ignore": {"type": "array", "items": {"type": "string"},
                   "description": "Terms / `wrong` strings the learner must never add."},
    },
}

_HISTORY_ENTRY = {
    "type": "object",
    "properties": {
        "id": {"type": "string"}, "ts": {"type": "string"}, "endpoint": {"type": "string"},
        "raw_text": {"type": "string"}, "text": {"type": "string"},
        "language": {"type": ["string", "null"]}, "duration_ms": {"type": ["integer", "null"]},
        "backend": {"type": "string"}, "model": {"type": "string"}, "cleaned": {"type": "boolean"},
    },
}


def spec(server_url: str = "/") -> dict:
    multipart = lambda schema: {"required": True, "content": {"multipart/form-data": {"schema": schema}}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "nanovoice stt-server",
            "version": __version__,
            "description": (
                "Self-hosted speech-to-text. Upload audio, get text.\n\n"
                "Errors are always `{\"error\": {\"code\", \"message\"}}`; every response carries `X-Request-ID`.\n\n"
                "Auth depends on `auth.mode`: `Authorization: Bearer <STT_TOKEN>` (token), the "
                "`Tailscale-User-Login` header injected by `tailscale serve` (tailscale), or none. "
                "`/health`, `/docs` and `/openapi.json` never require auth."
            ),
        },
        "servers": [{"url": server_url}],
        "components": {
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer", "description": "The value of $STT_TOKEN."},
                "tailscale": {"type": "apiKey", "in": "header", "name": "Tailscale-User-Login",
                              "description": "Set by `tailscale serve`; only with auth.mode = \"tailscale\"."},
            },
            "schemas": {"TranscribeResult": _RESULT, "Dictionary": _DICTIONARY, "HistoryEntry": _HISTORY_ENTRY},
            "responses": {"Error": {"description": "Error", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"error": {"type": "object", "properties": {
                    "code": {"type": "string"}, "message": {"type": "string"}}}}}}}}},
        },
        "paths": {
            "/transcribe": {"post": {
                "summary": "Transcribe audio (main endpoint, e.g. for iOS Shortcuts)",
                "description": "Pipeline: ffmpeg (16 kHz mono + VAD) → ASR (dictionary terms as prompt) → "
                               "dictionary corrections → replacement rules → optional LLM cleanup. "
                               "Form fields may also be given as query parameters.",
                "security": _AUTH,
                "requestBody": multipart(_TRANSCRIBE_FORM),
                "responses": {
                    "200": {"description": "Transcript", "content": {
                        "text/plain": {"schema": {"type": "string"}},
                        "application/json": {"schema": {"$ref": "#/components/schemas/TranscribeResult"}},
                    }},
                    **{c: _ERROR for c in ("400", "401", "403", "413", "415", "502", "503", "504")},
                },
            }},
            "/v1/audio/transcriptions": {"post": {
                "summary": "OpenAI-compatible transcription",
                "description": "Drop-in for OpenAI's endpoint (OpenWhispr Custom provider etc.). `model` is "
                               "accepted but ignored; LLM cleanup follows `openai_compat.clean` (off by default).",
                "security": _AUTH,
                "requestBody": multipart({
                    "type": "object", "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "model": {"type": "string", "example": "whisper-1"},
                        **_TEXT_FIELDS,
                        "response_format": {"type": "string", "enum": ["json", "text", "verbose_json"], "default": "json"},
                    },
                }),
                "responses": {
                    "200": {"description": "Transcript", "content": {
                        "application/json": {"schema": {"type": "object", "properties": {
                            "text": {"type": "string"}, "language": {"type": ["string", "null"]},
                            "duration": {"type": "number"}, "task": {"type": "string"}}}},
                        "text/plain": {"schema": {"type": "string"}},
                    }},
                    **{c: _ERROR for c in ("400", "401", "403", "413", "415", "502", "503", "504")},
                },
            }},
            "/dictionary": {
                "get": {"summary": "Get the dictionary", "security": _AUTH, "responses": {
                    "200": {"description": "Dictionary", "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/Dictionary"}}}},
                    "401": _ERROR}},
                "put": {"summary": "Replace the dictionary",
                        "description": "Validates and rewrites the dictionary file (takes effect immediately).",
                        "security": _AUTH,
                        "requestBody": {"required": True, "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Dictionary"}}}},
                        "responses": {"200": {"description": "Saved dictionary", "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Dictionary"}}}}, "400": _ERROR, "401": _ERROR}},
                "patch": {"summary": "Edit individual dictionary entries",
                          "description": "Added terms/corrections are removed from `ignore`; a correction whose `wrong` "
                                         "already exists replaces it. Removed entries are added to `ignore` (so the "
                                         "learner won't re-add them) unless `ignore_removed` is false.",
                          "security": _AUTH,
                          "requestBody": {"required": True, "content": {"application/json": {"schema": {
                              "type": "object", "properties": {
                                  "add_terms": {"type": "array", "items": {"type": "string"}},
                                  "remove_terms": {"type": "array", "items": {"type": "string"}},
                                  "add_corrections": {"type": "array", "items": {"type": "object", "properties": {
                                      "wrong": {"type": "string"}, "right": {"type": "string"}}}},
                                  "remove_corrections": {"type": "array", "items": {"type": "string"},
                                                         "description": "`wrong` values to remove"},
                                  "ignore_removed": {"type": "boolean", "default": True}}},
                              "example": {"add_terms": ["Tailscale"],
                                          "add_corrections": [{"wrong": "tail scale", "right": "Tailscale"}]}}}},
                          "responses": {"200": {"description": "Saved dictionary", "content": {"application/json": {
                              "schema": {"$ref": "#/components/schemas/Dictionary"}}}}, "400": _ERROR, "401": _ERROR}},
            },
            "/history/{entry_id}/correct": {"post": {
                "summary": "Correct a past transcript; derive dictionary corrections from the diff",
                "description": "Diffs the entry's final text against your corrected text. Each changed span "
                               "becomes a correction `wrong -> right` (and `right` a term). Single characters, "
                               "punctuation-only changes and spans absent from the raw ASR output are skipped "
                               "and reported. Requires `history.enabled`.",
                "security": _AUTH,
                "parameters": [{"name": "entry_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
                    "example": {"text": "我们明天用 Claude Code 开会吧。"}}}},
                "responses": {
                    "200": {"description": "What was added / skipped", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "added": {"$ref": "#/components/schemas/Dictionary"},
                            "skipped": {"type": "array", "items": {"type": "object", "properties": {
                                "wrong": {"type": "string"}, "right": {"type": "string"},
                                "reason": {"type": "string"}}}}}}}}},
                    "400": _ERROR, "401": _ERROR, "404": _ERROR,
                },
            }},
            "/dictionary/learn": {"post": {
                "summary": "Learn new terms / corrections from recent history now",
                "description": "Sends the last `learn.batch` history entries to the LLM and merges filtered "
                               "proposals into the dictionary. Requires `history.enabled`.",
                "security": _AUTH,
                "responses": {
                    "200": {"description": "What was added", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "entries": {"type": "integer"}, "ms": {"type": "integer"},
                            "added": {"$ref": "#/components/schemas/Dictionary"}}}}}},
                    "400": _ERROR, "401": _ERROR, "409": _ERROR, "502": _ERROR,
                },
            }},
            "/history": {"get": {
                "summary": "Recent transcriptions (newest first)",
                "security": _AUTH,
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20, "maximum": 1000}}],
                "responses": {
                    "200": {"description": "Entries", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"entries": {"type": "array", "items": {
                            "$ref": "#/components/schemas/HistoryEntry"}}}}}}},
                    "400": _ERROR, "401": _ERROR,
                },
            }},
            "/health": {"get": {"summary": "Status, backend/model, LLM, dictionary and history info (no auth)",
                                "responses": {"200": {"description": "Status"}}}},
            "/openapi.json": {"get": {"summary": "This document (no auth)", "responses": {"200": {"description": "OpenAPI"}}}},
            "/docs": {"get": {"summary": "Interactive API docs (Swagger UI, no auth)", "responses": {"200": {"description": "HTML"}}}},
        },
    }


DOCS_HTML = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>stt-server API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css">
</head><body><div id="ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({{url: "openapi.json", dom_id: "#ui"}});</script>
</body></html>"""
