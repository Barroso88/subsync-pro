import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.opensubtitles import OpenSubtitles
from app.subtitles.parser import parse_srt
from app.translation import DeepL, DeepLError, Gemini, rewrap, split_segments, to_srt, translate_cues, translatable
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


# ---- Gemini ---------------------------------------------------------------------------

def gem_resp(texts, **extra):
    return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"translations": texts})}]}}], **extra})


seen_models: list[str] = []


def gem_transport(handler_log, mode="ok"):
    def h(req: httpx.Request):
        assert req.url.host == "generativelanguage.googleapis.com" and req.headers["x-goog-api-key"] == "GKEY"
        assert "GKEY" not in str(req.url)                                    # a chave vai em cabeçalho, nunca no URL
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in
                                                       ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.8-flash-preview", "embedding-001")]
                                             + [{"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]})
        if "gemini-2.5-flash" in req.url.path:
            return httpx.Response(404, json={"error": {"message": "not found"}})        # o modelo antigo já não existe para esta chave
        seen_models.append(req.url.path)
        body = json.loads(req.content)
        items = json.loads(body["contents"][0]["parts"][0]["text"])["items_to_translate"]
        handler_log.append(items)
        if mode == "wrong_count" and len(items) > 5:
            return gem_resp(items[:-1])                                       # o modelo «perde» uma fala
        if mode == "blocked":
            if any("BAD" in t for t in items):
                if len(items) == 1:
                    return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
                return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
        return gem_resp([f"PT:{t}" for t in items])
    return httpx.MockTransport(h)


def test_gemini_translates_validates_count_and_subdivides():
    log = []
    g = Gemini("GKEY", "gemini-test", gem_transport(log, "wrong_count"))
    texts = [f"line {i}" for i in range(100)]
    out = g.translate(texts, "pt-PT", "en")
    assert out == [f"PT:{t}" for t in texts] and g.untranslated == 0           # contagem errada → subdivide até acertar
    assert max(len(x) for x in log[:1]) == 40 and any(len(x) <= 5 for x in log)
    with pytest.raises(DeepLError):
        g.translate(["x"], "ja")


def test_gemini_blocked_items_kept_original_and_rejected_if_many():
    log = []
    g = Gemini("GKEY", transport=gem_transport(log, "blocked"))
    texts = [f"line {i}" for i in range(200)] + ["BAD word"]
    out = g.translate(texts, "pt-BR", "en")
    assert out[-1] == "BAD word" and out[0] == "PT:line 0" and g.untranslated == 1     # 1 fala recusada: fica no original e é contada
    many = [f"BAD {i}" for i in range(30)]
    with pytest.raises(DeepLError) as e:
        Gemini("GKEY", transport=gem_transport([], "blocked")).translate(many, "pt-PT", "en")
    assert "rejeitada" in str(e.value)                                                  # demasiadas recusas: não instala uma legenda incompleta


def test_gemini_errors_mapped():
    for code, flag in ((403, "auth"), (429, "quota")):
        g = Gemini("GKEY", transport=httpx.MockTransport(lambda r, c=code: httpx.Response(c, json={"error": {"message": "x"}})))
        g._sleep = lambda s: None
        with pytest.raises(DeepLError) as e:
            g.translate(["x"], "pt-PT", "en")
        assert getattr(e.value, flag)
    g = Gemini("GKEY", "modelo/estranho;rm", httpx.MockTransport(lambda r: httpx.Response(404)))
    assert g.model == "modeloestranhorm" and "/" not in g.model and ";" not in g.model                                                         # o nome do modelo é sanitizado


def test_gemini_full_flow_via_api(cfg, fake_probe, media_root):
    state, log = {"used": 0, "sent": 0}, []
    base = transport(state)
    gem = gem_transport(log)
    OpenSubtitles._token = None
    app = create_app(cfg, transport=httpx.MockTransport(lambda r: gem.handle_request(r) if r.url.host == "generativelanguage.googleapis.com" else base.handle_request(r)))
    with TestClient(app) as c:
        c.post("/api/libraries", json={"name": "Séries", "type": "series", "path": str(media_root / "Séries"), "writable": True})
        c.put("/api/secrets", json={"values": {"opensubtitles_api_key": "K", "opensubtitles_username": "u", "opensubtitles_password": "p", "gemini_api_key": "GKEY"}})
        c.put("/api/settings", json={"languages": [{"code": "pt-PT", "priority": "primary"}], "translation": {"enabled": True, "auto_fallback": False, "source_languages": ["en"], "engine": "gemini", "gemini_model": ""}})
        j = c.post("/api/scan", json={}).json()["job_id"]
        for _ in range(200):
            if c.get(f"/api/jobs/{j}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        st = c.get("/api/translation").json()
        assert st["engine"] == "gemini" and st["configured"] and st["usage"] is None and "GKEY" not in json.dumps(st) and "pt-PT" in st["targets"]
        assert c.post("/api/translation/test").json()["sample"].startswith("PT:")
        item = e2(c)
        res = c.post(f"/api/media/{item['id']}/search", json={"languages": ["en"]}).json()["results"][0]["result"]
        d = c.post(f"/api/media/{item['id']}/translate", json={"result": res, "target": "pt-PT"}).json()
        assert d["status"] == "prepared" and d["evaluation"]["translation"]["engine"] == "gemini"
        assert any("Gemini" in f["label"] for f in d["evaluation"]["factors"])
        inst = c.post(f"/api/downloads/{d['id']}/install", json={"policy": "fail"}).json()
        cues = parse_srt(open(inst["dest_path"], encoding="utf-8").read()).cues
        assert len(cues) == 120 and cues[0].text.startswith("PT:") and cues[0].start_ms == 10_000
        bad = c.put("/api/settings", json={"translation": {"enabled": True, "source_languages": ["en"], "engine": "outro"}})
        assert bad.status_code == 400


def test_gemini_auto_model_picks_latest_flash_and_404_suggests_models():
    seen_models.clear()
    Gemini._AUTO.clear()
    g = Gemini("GKEY", transport=gem_transport([]))
    assert g.translate(["hello"], "pt-PT", "en") == ["PT:hello"]
    assert g.model == "gemini-3.5-flash-lite" and seen_models[-1].endswith("/models/gemini-3.5-flash-lite:generateContent")   # prefere o lite (limites do escalão gratuito)
    assert Gemini.pick_flash(["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.8-flash-preview"]) == "gemini-3.8-flash"    # sem lite: o flash mais recente
    assert Gemini.pick_flash(["embedding-001"]) is None
    old = Gemini("GKEY", "gemini-2.5-flash", gem_transport([]))                            # modelo fixo e já inexistente
    with pytest.raises(DeepLError) as e:
        old.translate(["x"], "pt-PT", "en")
    assert "não encontrado" in str(e.value) and "gemini-3.5-flash-lite" in str(e.value) and "automático" in str(e.value)
    Gemini._AUTO.clear()


def wait_job(c, jid):
    for _ in range(300):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("tarefa não terminou")


def test_translate_runs_as_background_job_with_progress(client):
    client.put("/api/settings", json={"translation": {"enabled": True, "auto_fallback": False, "source_languages": ["en"]}})
    item = e2(client)
    res = client.post(f"/api/media/{item['id']}/search", json={"languages": ["en"]}).json()["results"][0]["result"]
    r = client.post(f"/api/media/{item['id']}/translate/start", json={"result": res, "target": "pt-PT"})
    assert r.status_code == 202
    j = wait_job(client, r.json()["job_id"])
    assert j["status"] == "done" and j["total"] == 3 and j["progress"] == 3                  # 120 falas = 3 lotes de 50 (DeepL)
    dl = j["result"]
    assert dl["status"] == "prepared" and dl["lang"] == "pt-PT" and dl["evaluation"]["translation"]["engine"] == "deepl"
    assert client.get("/api/downloads", params={"status": "prepared"}).json()["total"] == 1   # fica por confirmar, não instalada
    # recusas óbvias são imediatas (sem criar tarefa)
    assert client.post(f"/api/media/{item['id']}/translate/start", json={"result": res, "target": "ja"}).status_code == 400
    client.put("/api/settings", json={"translation": {"enabled": False, "source_languages": ["en"]}})
    assert client.post(f"/api/media/{item['id']}/translate/start", json={"result": res, "target": "pt-PT"}).status_code == 403


def test_translate_job_failure_message_is_clean(client):
    client.put("/api/settings", json={"translation": {"enabled": True, "auto_fallback": False, "source_languages": ["en"]}})
    client.state["used"] = 499_990
    item = e2(client)
    res = client.post(f"/api/media/{item['id']}/search", json={"languages": ["en"]}).json()["results"][0]["result"]
    j = wait_job(client, client.post(f"/api/media/{item['id']}/translate/start", json={"result": res, "target": "pt-PT"}).json()["job_id"])
    assert j["status"] == "failed" and j["error"].startswith("Quota DeepL insuficiente") and "Error" not in j["error"][:20]


def test_gemini_thinking_off_falls_back_when_model_rejects_it():
    seen = []

    def h(req: httpx.Request):
        body = json.loads(req.content)
        seen.append("thinkingConfig" in body["generationConfig"])
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Budget 0 is invalid. This model only works in thinking mode."}})
        items = json.loads(body["contents"][0]["parts"][0]["text"])["items_to_translate"]
        return gem_resp([f"PT:{t}" for t in items])
    g = Gemini("GKEY", "gemini-test", httpx.MockTransport(h))
    assert g.translate(["a", "b"], "pt-PT", "en") == ["PT:a", "PT:b"] and seen == [True, False]
    g.translate(["c"], "pt-PT", "en")
    assert seen[-1] is False                                                                 # lembra-se: não volta a tentar


def test_gemini_rate_limits_wait_for_minute_limit_but_stop_on_daily_limit():
    sleeps, n = [], {"c": 0}

    def minute(req):
        n["c"] += 1
        if n["c"] <= 2:
            return httpx.Response(429, json={"error": {"message": "quota", "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})
        body = json.loads(req.content)
        return gem_resp([f"PT:{t}" for t in json.loads(body["contents"][0]["parts"][0]["text"])["items_to_translate"]])
    g = Gemini("GKEY", "gemini-test", httpx.MockTransport(minute))
    g._sleep = sleeps.append
    assert g.translate(["a"], "pt-PT", "en") == ["PT:a"] and sleeps == [8.0, 8.0]            # espera o retryDelay indicado (+1 s) e repete

    day = Gemini("GKEY", "gemini-test", httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"message": "Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier"}})))
    day._sleep = sleeps.append
    sleeps.clear()
    with pytest.raises(DeepLError) as e:
        day.translate(["a"], "pt-PT", "en")
    assert e.value.quota and "diário" in str(e.value) and not sleeps                           # limite diário: falha logo, sem esperar em vão
