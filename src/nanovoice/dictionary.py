"""User dictionary: proper nouns for the ASR prompt + simple corrections.

Lives in its own TOML file so it can be edited by hand (reloaded automatically when the
file changes), through the API / `stt dict`, or grown by the learner.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import tomllib
from pathlib import Path

log = logging.getLogger("nanovoice.dictionary")

MAX_TERM_LEN = 100

HEADER = """\
# nanovoice dictionary — edit freely; the server reloads this file when it changes.
# terms:       proper nouns / jargon, passed to ASR as a prompt (earlier = higher priority)
# corrections: fixed misrecognitions, replaced case-insensitively after ASR
# ignore:      terms/corrections the learner must never (re-)add
# Note: saving via the API or the learner rewrites this file (comments are not kept).
"""


class DictionaryError(ValueError):
    pass


def _key(s: str) -> str:
    return s.strip().casefold()


def _boundary_pattern(wrong: str) -> str:
    """Literal match; ASCII-alnum edges must not touch other ASCII alnum (so 'cloud' won't hit 'icloud')."""
    p = re.escape(wrong)
    if re.match(r"[A-Za-z0-9]", wrong):
        p = r"(?<![A-Za-z0-9])" + p
    if re.search(r"[A-Za-z0-9]$", wrong):
        p = p + r"(?![A-Za-z0-9])"
    return p


def validate(data: dict) -> dict:
    """Normalize {terms, corrections, ignore}; raise DictionaryError on bad input."""
    if not isinstance(data, dict):
        raise DictionaryError("dictionary must be an object")
    unknown = set(data) - {"terms", "corrections", "ignore"}
    if unknown:
        raise DictionaryError(f"unknown key(s): {', '.join(sorted(unknown))}")

    def str_list(name):
        items = data.get(name, [])
        if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
            raise DictionaryError(f"{name} must be a list of strings")
        out, seen = [], set()
        for t in (t.strip() for t in items):
            if not t:
                continue
            if len(t) > MAX_TERM_LEN:
                raise DictionaryError(f"{name}: entry too long: {t[:30]}...")
            if _key(t) not in seen:
                seen.add(_key(t))
                out.append(t)
        return out

    corrections, seen = [], set()
    raw = data.get("corrections", [])
    if not isinstance(raw, list):
        raise DictionaryError("corrections must be a list")
    for c in raw:
        if not isinstance(c, dict) or set(c) != {"wrong", "right"} or not all(isinstance(v, str) for v in c.values()):
            raise DictionaryError("each correction must be {wrong = \"...\", right = \"...\"}")
        wrong, right = c["wrong"].strip(), c["right"].strip()
        if not wrong or not right or wrong == right:
            raise DictionaryError(f"invalid correction: {c!r}")
        if _key(wrong) not in seen:
            seen.add(_key(wrong))
            corrections.append({"wrong": wrong, "right": right})
    return {"terms": str_list("terms"), "corrections": corrections, "ignore": str_list("ignore")}


def dumps(data: dict) -> str:
    # JSON string literals are valid TOML basic strings.
    s = lambda v: json.dumps(v, ensure_ascii=False)
    lines = [HEADER, "terms = ["]
    lines += [f"  {s(t)}," for t in data["terms"]]
    lines += ["]", "", "ignore = ["]
    lines += [f"  {s(t)}," for t in data["ignore"]]
    lines += ["]", ""]
    for c in data["corrections"]:
        lines += ["[[corrections]]", f"wrong = {s(c['wrong'])}", f"right = {s(c['right'])}", ""]
    return "\n".join(lines)


class Dictionary:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._mtime: float | None = None
        self._data = {"terms": [], "corrections": [], "ignore": []}
        self._rules: list[tuple[re.Pattern, str]] = []
        self._maybe_reload()

    # -- loading / saving

    def _maybe_reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        if mtime is None:
            self._set({"terms": [], "corrections": [], "ignore": []})
            return
        try:
            with self.path.open("rb") as f:
                self._set(validate(tomllib.load(f)))
            log.info("dictionary loaded", extra={"fields": {"path": str(self.path), **self.counts()}})
        except (tomllib.TOMLDecodeError, DictionaryError, OSError) as e:
            # Keep serving with the previous version rather than failing requests on a typo.
            log.error("dictionary not reloaded, keeping previous version", extra={"fields": {"error": str(e)}})

    def _set(self, data: dict) -> None:
        self._data = data
        self._rules = [(re.compile(_boundary_pattern(c["wrong"]), re.IGNORECASE), c["right"])
                       for c in data["corrections"]]

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".dictionary-", suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(data))
        os.replace(tmp, self.path)
        self._set(data)
        self._mtime = self.path.stat().st_mtime

    # -- public API

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_reload()
            return json.loads(json.dumps(self._data))

    def counts(self) -> dict:
        d = self._data
        return {"terms": len(d["terms"]), "corrections": len(d["corrections"]), "ignore": len(d["ignore"])}

    def replace(self, data: dict) -> dict:
        data = validate(data)
        with self._lock:
            self._save(data)
        return data

    def merge(self, terms: list[str], corrections: list[dict]) -> dict:
        """Add new entries, skipping duplicates and anything on the ignore list. Returns what was added."""
        with self._lock:
            self._maybe_reload()
            data = json.loads(json.dumps(self._data))
            ignored = {_key(t) for t in data["ignore"]}
            have_terms = {_key(t) for t in data["terms"]}
            have_wrong = {_key(c["wrong"]) for c in data["corrections"]}
            added = {"terms": [], "corrections": []}
            for t in terms:
                if _key(t) and _key(t) not in have_terms and _key(t) not in ignored:
                    have_terms.add(_key(t))
                    added["terms"].append(t.strip())
            for c in corrections:
                if _key(c["wrong"]) not in have_wrong and _key(c["wrong"]) not in ignored:
                    have_wrong.add(_key(c["wrong"]))
                    added["corrections"].append(c)
            if added["terms"] or added["corrections"]:
                data["terms"] += added["terms"]
                data["corrections"] += added["corrections"]
                self._save(validate(data))
            return added

    def patch(self, add_terms=(), remove_terms=(), add_corrections=(), remove_corrections=(),
              ignore_removed: bool = True) -> dict:
        """Explicit (user) edits. Added items leave the ignore list; removed ones join it unless told not to.
        A correction for an existing `wrong` replaces its `right`."""
        adds = validate({"terms": list(add_terms), "corrections": list(add_corrections)})
        removes = validate({"terms": list(remove_terms), "ignore": list(remove_corrections)})
        add_terms, add_corrections = adds["terms"], adds["corrections"]
        remove_terms, remove_corrections = removes["terms"], removes["ignore"]
        with self._lock:
            self._maybe_reload()
            data = json.loads(json.dumps(self._data))
            added_keys = {_key(t) for t in add_terms} | {_key(c["wrong"]) for c in add_corrections}
            rm_terms = {_key(t) for t in remove_terms}
            rm_wrong = {_key(w) for w in remove_corrections}
            upsert = {_key(c["wrong"]): c for c in add_corrections}

            data["terms"] = [t for t in data["terms"] if _key(t) not in rm_terms] + list(add_terms)
            data["corrections"] = [c for c in data["corrections"]
                                   if _key(c["wrong"]) not in rm_wrong and _key(c["wrong"]) not in upsert]
            data["corrections"] += list(add_corrections)
            data["ignore"] = [t for t in data["ignore"] if _key(t) not in added_keys]
            if ignore_removed:
                data["ignore"] += list(remove_terms) + list(remove_corrections)
            data = validate(data)
            self._save(data)
            return data

    def prompt_terms(self) -> list[str]:
        with self._lock:
            self._maybe_reload()
            return list(self._data["terms"])

    def apply(self, text: str) -> str:
        with self._lock:
            self._maybe_reload()
            rules = self._rules
        for rx, right in rules:
            text = rx.sub(lambda _m, r=right: r, text)
        return text
