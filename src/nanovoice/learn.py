"""Grow the dictionary from transcription history using an LLM.

Proposals are filtered before they are merged so a bad LLM answer can't do much damage:
a correction's `wrong` must literally occur in a recent raw transcript and its `right`
must be a dictionary term; anything on the dictionary's ignore list is never re-added.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time

import httpx

from .config import Config
from .dictionary import MAX_TERM_LEN, Dictionary
from .history import History
from .postprocess import chat, llm_client

log = logging.getLogger("nanovoice.learn")


class LearnError(Exception):
    pass


class LearnBusy(LearnError):
    pass


def parse_proposal(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise LearnError("LLM did not return a JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LearnError(f"LLM returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LearnError("LLM did not return a JSON object")
    return data


def filter_proposal(data: dict, entries: list[dict], current: dict, max_terms: int, max_corrections: int):
    have = {t.casefold() for t in current["terms"]}
    terms = []
    for t in data.get("terms") or []:
        if isinstance(t, str) and 0 < len(t.strip()) <= MAX_TERM_LEN and t.strip().casefold() not in have:
            terms.append(t.strip())
    terms = terms[:max_terms]

    known = have | {t.casefold() for t in terms}
    raw_texts = [e.get("raw_text", "").casefold() for e in entries]
    corrections = []
    for c in data.get("corrections") or []:
        if not isinstance(c, dict):
            continue
        wrong, right = str(c.get("wrong", "")).strip(), str(c.get("right", "")).strip()
        if (len(wrong) >= 2 and right and wrong != right
                and right.casefold() in known                       # corrections only towards dictionary terms
                and wrong.casefold() not in known                   # never rewrite a known-good term
                and any(wrong.casefold() in r for r in raw_texts)):  # must be grounded in real ASR output
            corrections.append({"wrong": wrong, "right": right})
    return terms, corrections[:max_corrections]


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#'_-]*|\s+|.", re.DOTALL)
_PUNCT_RE = re.compile(r"[\s\W_]+")
_EDGE_RE = re.compile(r"^[\s\W_]+|[\s\W_]+$")  # punctuation/space at either end of a changed span
MAX_EDIT_CORRECTIONS = 10


def corrections_from_edit(original: str, corrected: str, raw: str) -> tuple[list[str], list[dict], list[dict]]:
    """Diff a transcript against the user's corrected version.

    Returns (terms, corrections, skipped). Each changed span becomes `wrong -> right`;
    `right` also becomes a term. Spans are skipped when they are a single character (too
    risky as a global replacement), punctuation-only, pure insertions/deletions, or not
    present in the raw ASR output (dictionary corrections are applied to raw ASR text).
    """
    a, b = _TOKEN_RE.findall(original), _TOKEN_RE.findall(corrected)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    # Group changed spans, letting whitespace-only equal runs join neighbours ("cloud code" -> "Claude Code").
    groups, cur = [], None
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        joinable = (tag == "equal" and cur is not None and k + 1 < len(ops)
                    and all(t.isspace() for t in a[i1:i2]))
        if tag != "equal" or joinable:
            cur = [cur[0], i2, cur[2], j2] if cur else [i1, i2, j1, j2]
        else:
            if cur:
                groups.append(cur)
            cur = None
    if cur:
        groups.append(cur)

    terms, corrections, skipped = [], [], []
    raw_cf = raw.casefold()
    for i1, i2, j1, j2 in groups:
        wrong, right = _EDGE_RE.sub("", "".join(a[i1:i2])), _EDGE_RE.sub("", "".join(b[j1:j2]))
        reason = None
        if wrong == right or _PUNCT_RE.sub("", wrong) == _PUNCT_RE.sub("", right):
            reason = "punctuation or spacing only"
        elif not wrong or not right:
            reason = "insertion or deletion"
        elif len(_PUNCT_RE.sub("", wrong)) < 2:
            reason = "too short to replace globally"
        elif wrong.casefold() not in raw_cf:
            reason = "not found in the raw ASR output"
        elif len(corrections) >= MAX_EDIT_CORRECTIONS:
            reason = "too many changes"
        if reason:
            skipped.append({"wrong": wrong, "right": right, "reason": reason})
            continue
        corrections.append({"wrong": wrong, "right": right})
        if len(_PUNCT_RE.sub("", right)) >= 2 and right not in terms:
            terms.append(right)
    return terms, corrections, skipped


class Learner:
    def __init__(self, cfg: Config, dictionary: Dictionary, history: History,
                 transport: httpx.BaseTransport | None = None):
        self.cfg = cfg.learn
        self.model = cfg.learn.model or cfg.llm.model
        self.temperature = cfg.llm.temperature
        self.dictionary, self.history = dictionary, history
        self._client = llm_client(cfg.llm, cfg.learn.timeout_s, transport)
        self._running = threading.Lock()
        self._count_lock = threading.Lock()
        self._since_last = 0
        self.last_run: dict | None = None

    def note_entry(self) -> None:
        """Called after each history append; kicks off a background run every `learn.every` entries."""
        if not self.cfg.auto:
            return
        with self._count_lock:
            self._since_last += 1
            due = self._since_last >= self.cfg.every
        if due and not self._running.locked():
            threading.Thread(target=self._background, name="learn", daemon=True).start()

    def _background(self) -> None:
        try:
            self.run()
        except LearnBusy:
            pass
        except Exception as e:
            log.warning("background learning failed", extra={"fields": {"error": f"{type(e).__name__}: {e}"[:300]}})

    def run(self) -> dict:
        if not self._running.acquire(blocking=False):
            raise LearnBusy("a learning run is already in progress")
        try:
            with self._count_lock:
                self._since_last = 0
            t = time.perf_counter()
            entries = [e for e in self.history.tail(self.cfg.batch) if (e.get("raw_text") or "").strip()]
            result = {"entries": len(entries), "added": {"terms": [], "corrections": []}}
            if entries:
                current = self.dictionary.snapshot()
                user = json.dumps({
                    "dictionary": {"terms": current["terms"], "corrections": current["corrections"],
                                   "never_add": current["ignore"]},
                    "transcripts": [{"raw": e.get("raw_text", ""), "final": e.get("text", "")} for e in entries],
                }, ensure_ascii=False, indent=1)
                try:
                    out = chat(self._client, self.model, self.temperature, self.cfg.prompt, user)
                except Exception as e:
                    raise LearnError(f"LLM request failed: {type(e).__name__}: {e}"[:300]) from e
                terms, corrections = filter_proposal(parse_proposal(out), entries, current,
                                                     self.cfg.max_new_terms, self.cfg.max_new_corrections)
                result["added"] = self.dictionary.merge(terms, corrections)
            result["ms"] = round((time.perf_counter() - t) * 1000)
            self.last_run = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **result}
            log.info("learning run finished", extra={"fields": {
                "entries": result["entries"], "added_terms": result["added"]["terms"],
                "added_corrections": result["added"]["corrections"], "ms": result["ms"],
            }})
            return result
        finally:
            self._running.release()
