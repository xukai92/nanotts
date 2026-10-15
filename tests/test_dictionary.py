import json
import os
import time

import httpx
import pytest
from starlette.routing import Route

from conftest import FakeBackend, upload
from nanovoice.config import config_from_dict
from nanovoice.dictionary import Dictionary, DictionaryError, dumps, validate
from nanovoice.history import History
from nanovoice.learn import Learner, LearnError, corrections_from_edit, filter_proposal, parse_proposal


# --------------------------------------------------------------------------- Dictionary


def test_dictionary_roundtrip_and_apply(tmp_path):
    d = Dictionary(tmp_path / "d.toml")
    assert d.snapshot() == {"terms": [], "corrections": [], "ignore": []}
    d.replace({"terms": ["Claude Code", "claude code", " "], "corrections": [
        {"wrong": "cloud code", "right": "Claude Code"},
        {"wrong": "克劳德", "right": "Claude"},
        {"wrong": 'a"b\\c', "right": "x"},
    ]})
    assert d.snapshot()["terms"] == ["Claude Code"]  # deduped case-insensitively, blanks dropped
    assert Dictionary(tmp_path / "d.toml").snapshot() == d.snapshot()  # file round-trips (quotes, backslashes)
    assert d.apply("I use Cloud Code and icloud code, 克劳德很好") == "I use Claude Code and icloud code, Claude很好"
    assert d.apply('a"b\\c') == "x"


def test_dictionary_reloads_on_edit_and_survives_bad_file(tmp_path):
    p = tmp_path / "d.toml"
    d = Dictionary(p)
    p.write_text('terms = ["A"]\n')
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert d.prompt_terms() == ["A"]
    p.write_text("terms = [ broken")
    os.utime(p, (time.time() + 10, time.time() + 10))
    assert d.prompt_terms() == ["A"]  # keeps the last good version
    p.unlink()
    assert d.prompt_terms() == []


def test_dictionary_validation():
    for bad in ({"terms": "x"}, {"terms": [1]}, {"corrections": [{"wrong": "a"}]},
                {"corrections": [{"wrong": "a", "right": "a"}]}, {"extra": []}, {"terms": ["x" * 101]}):
        with pytest.raises(DictionaryError):
            validate(bad)


def test_dictionary_patch_semantics(tmp_path):
    d = Dictionary(tmp_path / "d.toml")
    d.replace({"terms": ["A", "B"], "corrections": [{"wrong": "aa", "right": "A"}], "ignore": ["C"]})
    out = d.patch(add_terms=["C"], remove_terms=["B"], add_corrections=[{"wrong": "AA", "right": "A2"}])
    assert out["terms"] == ["A", "C"]
    assert out["corrections"] == [{"wrong": "AA", "right": "A2"}]  # upserted by wrong
    assert out["ignore"] == ["B"]  # C left ignore (explicitly added), B joined it
    out = d.patch(remove_corrections=["aa"], ignore_removed=False)
    assert out["corrections"] == [] and out["ignore"] == ["B"]


def test_dictionary_merge_respects_ignore(tmp_path):
    d = Dictionary(tmp_path / "d.toml")
    d.replace({"terms": ["A"], "ignore": ["Bad", "zz"]})
    added = d.merge(["a", "New", "bad"], [{"wrong": "ZZ", "right": "A"}, {"wrong": "nw", "right": "New"}])
    assert added == {"terms": ["New"], "corrections": [{"wrong": "nw", "right": "New"}]}
    assert d.snapshot()["terms"] == ["A", "New"]


def test_dumps_is_valid_toml():
    import tomllib
    data = {"terms": ["中文", "x\ny"], "corrections": [{"wrong": "\t", "right": "'"}], "ignore": []}
    assert tomllib.loads(dumps(data)) == data


# --------------------------------------------------------------------------- learning helpers


def test_corrections_from_edit():
    terms, corr, skipped = corrections_from_edit(
        "我们用克劳德写代码，在说一遍 cloud code", "我们用Claude写代码，再说一遍。 Claude Code",
        "嗯我们用克劳德写代码在说一遍 cloud code")
    assert {"wrong": "克劳德", "right": "Claude"} in corr
    assert {"wrong": "cloud code", "right": "Claude Code"} in corr
    assert terms == ["Claude", "Claude Code"]
    reasons = {s["wrong"]: s["reason"] for s in skipped}
    assert reasons["在"] == "too short to replace globally"


def test_corrections_from_edit_requires_raw_match_and_ignores_punctuation():
    terms, corr, skipped = corrections_from_edit("hello wrld", "hello world", "hello wurld")
    assert corr == [] and terms == [] and skipped[0]["reason"] == "not found in the raw ASR output"
    terms, corr, skipped = corrections_from_edit("你好，世界 ok", "你好。世界, ok", "你好世界 ok")
    assert corr == [] and {s["reason"] for s in skipped} == {"punctuation or spacing only"}


def test_parse_and_filter_proposal():
    with pytest.raises(LearnError):
        parse_proposal("no json here")
    data = parse_proposal('```json\n{"terms": ["Tailscale", "A", 3, ""], "corrections": ['
                          '{"wrong": "tail scale", "right": "Tailscale"},'   # ok
                          '{"wrong": "made up", "right": "Tailscale"},'      # not in raw
                          '{"wrong": "foo bar", "right": "Unknown"},'        # right not a term
                          '{"wrong": "A", "right": "Tailscale"}]}\n```')     # would rewrite a known term
    entries = [{"raw_text": "deploy to tail scale with foo bar and A"}]
    terms, corr = filter_proposal(data, entries, {"terms": ["A"]}, 20, 10)
    assert terms == ["Tailscale"]
    assert corr == [{"wrong": "tail scale", "right": "Tailscale"}]


def _learner(tmp_path, handler, **learn):
    cfg = config_from_dict({"learn": learn, "llm": {"model": "m"}})
    d, h = Dictionary(tmp_path / "d.toml"), History(tmp_path / "h.jsonl")
    return Learner(cfg, d, h, httpx.MockTransport(handler)), d, h


def test_learner_run(tmp_path):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '<think>...</think>{"terms": ["Tailscale"], "corrections": [{"wrong": "tail scale", "right": "Tailscale"}]}'}}]})

    learner, d, h = _learner(tmp_path, handler, batch=2)
    for i in range(3):
        h.append({"id": str(i), "raw_text": f"entry {i} tail scale", "text": f"Entry {i}."})
    res = learner.run()
    assert res["entries"] == 2
    assert res["added"] == {"terms": ["Tailscale"], "corrections": [{"wrong": "tail scale", "right": "Tailscale"}]}
    assert seen["temperature"] == 0 and seen["model"] == "m"
    payload = json.loads(seen["messages"][1]["content"])
    assert [t["raw"] for t in payload["transcripts"]] == ["entry 1 tail scale", "entry 2 tail scale"]
    assert learner.run()["added"] == {"terms": [], "corrections": []}  # idempotent


def test_learner_auto_trigger(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"terms": ["X1"]}'}}]})

    learner, d, h = _learner(tmp_path, handler, every=3)
    h.append({"raw_text": "X1 something"})
    for _ in range(2):
        learner.note_entry()
    time.sleep(0.1)
    assert calls == []
    learner.note_entry()
    for _ in range(50):
        if learner.last_run:
            break
        time.sleep(0.02)
    assert calls == [1] and d.prompt_terms() == ["X1"]


# --------------------------------------------------------------------------- HTTP API


def test_dictionary_api(make_client):
    c = make_client()
    assert c.get("/dictionary").json() == {"terms": [], "corrections": [], "ignore": []}
    r = c.patch("/dictionary", json={"add_terms": ["Tailscale"],
                                     "add_corrections": [{"wrong": "tail scale", "right": "Tailscale"}]})
    assert r.status_code == 200 and r.json()["terms"] == ["Tailscale"]
    r = c.put("/dictionary", json={"terms": ["A"], "corrections": [], "ignore": []})
    assert r.json()["terms"] == ["A"]
    assert c.put("/dictionary", json={"terms": "A"}).status_code == 400
    assert c.patch("/dictionary", json={"nope": 1}).status_code == 400
    assert c.patch("/dictionary", content=b"not json").status_code == 400
    assert c.get("/dictionary", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_dictionary_applied_in_pipeline(make_client, audio_dir):
    c = make_client(backend=FakeBackend(text="我用 cloud code 部署到 tail scale"))
    c.patch("/dictionary", json={"add_terms": ["Tailscale"], "add_corrections": [
        {"wrong": "cloud code", "right": "Claude Code"}, {"wrong": "tail scale", "right": "Tailscale"}]})
    r = c.post("/transcribe", data={"format": "json", "clean": "false"}, files=upload(audio_dir / "speech.wav"))
    assert r.json()["text"] == "我用 Claude Code 部署到 Tailscale"
    assert r.json()["raw_text"] == "我用 cloud code 部署到 tail scale"
    assert c.backend.calls[0]["prompt"] == "Tailscale"


def test_history_disabled(make_client):
    c = make_client()
    for method, path in (("GET", "/history"), ("POST", "/dictionary/learn"), ("POST", "/history/x/correct")):
        r = c.request(method, path, json={"text": "x"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "history_disabled"


def test_history_and_correct_flow(make_client, audio_dir):
    c = make_client({"history": {"enabled": True}, "learn": {"auto": False}},
                    backend=FakeBackend(text="我们明天用 cloud code 开会"))
    r = c.post("/transcribe", data={"format": "json", "clean": "false"}, files=upload(audio_dir / "speech.wav"))
    entry_id = r.json()["id"]
    assert entry_id == r.headers["x-request-id"]
    c.post("/transcribe", files=upload(audio_dir / "silence.wav"))  # empty transcript: not recorded

    entries = c.get("/history", params={"limit": 5}).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["id"] == entry_id and entries[0]["raw_text"] == "我们明天用 cloud code 开会"
    assert "timings" not in entries[0]
    assert c.get("/history", params={"limit": 0}).status_code == 400

    r = c.post(f"/history/{entry_id}/correct", json={"text": "我们明天用 Claude Code 开会"})
    assert r.status_code == 200
    assert r.json()["added"] == {"terms": ["Claude Code"],
                                 "corrections": [{"wrong": "cloud code", "right": "Claude Code"}]}
    assert c.get("/dictionary").json()["terms"] == ["Claude Code"]
    assert c.post("/history/nope/correct", json={"text": "x"}).status_code == 404
    assert c.post(f"/history/{entry_id}/correct", json={}).status_code == 400

    # next transcription benefits from the learned correction
    r = c.post("/transcribe", data={"clean": "false"}, files=upload(audio_dir / "speech.wav"))
    assert r.text == "我们明天用 Claude Code 开会"


def test_history_file_contents(make_client, audio_dir, tmp_path):
    c = make_client({"history": {"enabled": True}, "learn": {"auto": False}})
    c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    path = tmp_path / "history.jsonl"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["raw_text"] == "hello world 你好世界" and entry["text"] == "Hello world. 你好世界。"
    assert entry["endpoint"] == "transcribe" and entry["ts"]


def test_learn_endpoint(make_client, audio_dir):
    def llm(req):
        system = json.loads(req.content)["messages"][0]["content"]
        content = ('{"terms": ["Tailscale"], "corrections": []}' if "词典" in system else "cleaned")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    c = make_client({"history": {"enabled": True}, "learn": {"auto": False}}, llm=llm)
    c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    r = c.post("/dictionary/learn")
    assert r.status_code == 200
    assert r.json()["added"]["terms"] == ["Tailscale"] and r.json()["entries"] == 1
    assert c.get("/health").json()["history"]["last_learn"]["added"]["terms"] == ["Tailscale"]

    c = make_client({"history": {"enabled": True}, "learn": {"auto": False}},
                    llm=lambda req: httpx.Response(500))
    c.post("/transcribe", files=upload(audio_dir / "speech.wav"))
    r = c.post("/dictionary/learn")
    assert r.status_code == 502 and r.json()["error"]["code"] == "learn_failed"


def test_openapi_and_docs(make_client):
    c = make_client({"server": {"base_path": "/stt"}}, auth=False)
    spec = c.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.") and spec["servers"] == [{"url": "/"}]
    assert c.get("/stt/openapi.json").json()["servers"] == [{"url": "/stt"}]
    # every route the app serves is documented, with the right methods
    for route in c.app.app.routes:
        if isinstance(route, Route):
            assert route.path in spec["paths"], route.path
            assert {m.lower() for m in route.methods} - {"head"} <= set(spec["paths"][route.path]), route.path
    r = c.get("/stt/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text and 'url: "openapi.json"' in r.text
