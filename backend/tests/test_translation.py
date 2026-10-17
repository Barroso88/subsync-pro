import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.opensubtitles import OpenSubtitles
from app.subtitles.parser import parse_srt
from app.translation import DeepL, DeepLError, rewrap, split_segments, to_srt, translate_cues, translatable
from tests.conftest import make_srt


def fake_deepl(calls, usage=(1000, 500000)):
    def h(req: httpx.Request):
        assert req.headers["authorization"] == "DeepL-Auth-Key KEY:fx"
        if req.url.path == "/v2/usage":
            return httpx.Response(200, json={"character_count": usage[0], "character_limit": usage[1]})
        body = json.loads(req.content)
        calls.append(body)
        assert req.url.host == "api-free.deepl.com" and len(body["text"]) <= 50
        return httpx.Response(200, json={"translations": [{"text": f"PT:{t}"} for t in body["text"]]})
    return httpx.MockTransport(h)


def test_free_vs_pro_host_and_batching():
    calls = []
    d = DeepL("KEY:fx", fake_deepl(calls))
    out = d.translate([f"t{i}" for i in range(120)], "pt-PT", "en")
    assert len(out) == 120 and [len(c["text"]) for c in calls] == [50, 50, 20]
    assert calls[0]["target_lang"] == "PT-PT" and calls[0]["source_lang"] == "EN"
    assert DeepL("abc").base == "https://api.deepl.com"
    with pytest.raises(DeepLError):
        d.translate(["x"], "ja")


def test_errors_mapped():
    for code, flag in ((403, "auth"), (456, "quota")):
        d = DeepL("KEY:fx", httpx.MockTransport(lambda r, c=code: httpx.Response(c)))
        with pytest.raises(DeepLError) as e:
            d.translate(["x"], "pt-BR")
        assert getattr(e.value, flag)


def test_timings_preserved_and_dialog_handling():
    srt = ("1\n00:00:01,000 --> 00:00:03,000\n- Hello there\n- General Kenobi\n\n"
           "2\n00:00:04,000 --> 00:00:06,500\nThis is a rather long sentence that will certainly need to be wrapped in two lines\n\n"
           "3\n00:00:07,000 --> 00:00:08,000\n♪\n")
    cues = parse_srt(srt).cues
    calls = []
    out, chars = translate_cues(cues, "pt-BR", "en", DeepL("KEY:fx", fake_deepl(calls)))
    assert [(c.start_ms, c.end_ms) for c in out] == [(c.start_ms, c.end_ms) for c in cues]
    assert out[0].text == "- PT:Hello there\n- PT:General Kenobi"       # diálogo mantém um travessão por fala
    assert "\n" in out[1].text and max(len(l) for l in out[1].text.split("\n")) <= 50
    assert out[2].text == "♪" and not translatable("♪")                    # música não é enviada
    assert chars == sum(len(t) for t in calls[0]["text"])
    assert len(parse_srt(to_srt(out)).cues) == 3                          # SRT resultante é válido
    assert split_segments("a\nb") == (["a b"], False) and rewrap("short") == "short"


# ---- fluxo completo pela API --------------------------------------------------------

def transport(state):
    def h(req: httpx.Request):
        host = req.url.host
        if host == "api-free.deepl.com":
            if req.url.path == "/v2/usage":
                return httpx.Response(200, json={"character_count": state["used"], "character_limit": 500000})
            body = json.loads(req.content)
            state["sent"] += sum(len(t) for t in body["text"])
            return httpx.Response(200, json={"translations": [{"text": "Tradução: " + t} for t in body["text"]]})
        p = req.url.path
        if p.endswith("/login"):
            return httpx.Response(200, json={"token": "T"})
        if p.endswith("/subtitles"):
            return httpx.Response(200, json={"data": [{"id": "1", "attributes": {
                "language": "en", "download_count": 5000, "release": "Minha.Serie.S01E02.1080p.WEB-DL-GRP",
                "feature_details": {"movie_name": "Minha Série", "season_number": 1, "episode_number": 2},
                "files": [{"file_id": 5, "file_name": "x.srt"}]}}]})
        if p.endswith("/download"):
            return httpx.Response(200, json={"link": "https://dl.opensubtitles.com/f.srt", "file_name": "x.srt"})
        if host == "dl.opensubtitles.com":
            return httpx.Response(200, content=make_srt(120, 30_000, lang="en").encode())
        return httpx.Response(404)
    return httpx.MockTransport(h)


@pytest.fixture
def client(cfg, fake_probe, media_root):
    state = {"used": 1000, "sent": 0}
    OpenSubtitles._token = None
    app = create_app(cfg, transport=transport(state))
    with TestClient(app) as c:
        c.state, c.core, c.root = state, app.state.core, media_root
        c.post("/api/libraries", json={"name": "Séries", "type": "series", "path": str(media_root / "Séries"), "writable": True})
        c.put("/api/secrets", json={"values": {"opensubtitles_api_key": "K", "opensubtitles_username": "u", "opensubtitles_password": "p", "deepl_api_key": "KEY:fx"}})
        c.put("/api/settings", json={"languages": [{"code": "pt-PT", "priority": "primary"}]})
        j = c.post("/api/scan", json={}).json()["job_id"]
        for _ in range(200):
            if c.get(f"/api/jobs/{j}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        yield c


def e2(client):
    return next(i for i in client.get("/api/media", params={"kind": "episode"}).json()["items"] if "S01E02" in i["file_name"])


def test_translation_disabled_by_default(client):
    r = client.post(f"/api/media/{e2(client)['id']}/translate", json={"result": {"provider": "opensubtitles", "file_id": "5", "language": "en"}, "target": "pt-PT"})
    assert r.status_code == 403


def test_translate_prepare_install_flow(client):
    client.put("/api/settings", json={"translation": {"enabled": True, "auto_fallback": False, "source_languages": ["en"]}})
    st = client.get("/api/translation").json()
    assert st["configured"] and st["plan"] == "free" and st["usage"]["remaining"] == 499000
    assert "KEY" not in json.dumps(st)
    item = e2(client)
    res = client.post(f"/api/media/{item['id']}/search", json={"languages": ["en"]}).json()["results"][0]["result"]
    d = client.post(f"/api/media/{item['id']}/translate", json={"result": res, "target": "pt-PT"}).json()
    assert d["status"] == "prepared" and d["lang"] == "pt-PT" and d["dest_path"].endswith("S01E02.pt-PT.srt")
    ev = d["evaluation"]
    assert ev["translation"]["engine"] == "deepl" and ev["translation"]["characters"] == client.state["sent"] > 0
    assert any("Tradução automática" in f["label"] for f in ev["factors"])
    assert ev["sync"] != "validated_reference" or ev["alignment"]       # nunca validada sem referência real
    inst = client.post(f"/api/downloads/{d['id']}/install", json={"policy": "fail"}).json()
    text = open(inst["dest_path"], encoding="utf-8").read()
    cues = parse_srt(text).cues
    assert len(cues) == 120 and cues[0].text.startswith("Tradução:") and cues[0].start_ms == 10_000   # tempos originais
    assert client.get("/api/media", params={"status": "missing"}).json()["total"] == 0    # E01 já tinha pt-PT; E02 ficou coberto


def test_quota_refused_before_translating(client):
    client.put("/api/settings", json={"translation": {"enabled": True, "auto_fallback": False, "source_languages": ["en"]}})
    client.state["used"] = 499_990
    item = e2(client)
    res = client.post(f"/api/media/{item['id']}/search", json={"languages": ["en"]}).json()["results"][0]["result"]
    r = client.post(f"/api/media/{item['id']}/translate", json={"result": res, "target": "pt-PT"})
    assert r.status_code == 429 and "Quota DeepL insuficiente" in r.json()["detail"]
    assert client.state["sent"] == 0                                   # nada foi enviado ao DeepL


def test_batch_auto_fallback_leaves_pending_never_installs(client):
    client.put("/api/settings", json={"translation": {"enabled": True, "auto_fallback": True, "source_languages": ["en"]}})
    ids = [e2(client)["id"]]
    j = client.post("/api/batch", json={"media_ids": ids, "install": True, "min_tier": "probable"}).json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{j}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["translated"] == 1 and job["result"]["installed"] == 0, job
    assert client.get("/api/downloads", params={"status": "prepared"}).json()["total"] == 1
    assert not list((client.root / "Séries").rglob("*S01E02.pt-PT.srt"))
