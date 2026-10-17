"""Fluxo completo via API HTTP com fornecedor simulado (sem rede)."""
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.opensubtitles import OpenSubtitles
from tests.conftest import make_srt, make_zip


def provider_transport(state):
    def h(req: httpx.Request):
        path = req.url.path
        if path.endswith("/login"):
            return httpx.Response(200, json={"token": "T"})
        if path.endswith("/subtitles"):
            state["search"] = dict(req.url.params)
            lang = req.url.params.get("languages", "pt-pt").split(",")[0]
            return httpx.Response(200, json={"total_count": 1, "data": [{"id": "1", "attributes": {
                "language": "pt-PT", "download_count": 900, "moviehash_match": False, "release": "Minha.Serie.S01E02.1080p.WEB-DL-GRP",
                "feature_details": {"movie_name": "Minha Série", "season_number": 1, "episode_number": 2},
                "files": [{"file_id": 55, "file_name": "x.srt"}]}}]})
        if path.endswith("/download"):
            return httpx.Response(200, json={"link": "https://dl.opensubtitles.com/f.zip", "file_name": "x.srt", "remaining": 10})
        if req.url.host == "dl.opensubtitles.com":
            return httpx.Response(200, content=state["blob"])
        return httpx.Response(404)
    return httpx.MockTransport(h)


@pytest.fixture
def client(cfg, fake_probe, media_root):
    state = {"blob": make_zip({"Minha.Serie.S01E02.pt.srt": make_srt(120, 30_000, lang="pt-PT").encode()})}
    OpenSubtitles._token = None
    app = create_app(cfg, transport=provider_transport(state))
    with TestClient(app) as c:
        c.state = state
        c.core = app.state.core
        c.root = media_root
        c.post("/api/libraries", json={"name": "Séries", "type": "series", "path": str(media_root / "Séries"), "writable": True})
        c.post("/api/libraries", json={"name": "Filmes", "type": "movies", "path": str(media_root / "Filmes"), "writable": False})
        c.put("/api/secrets", json={"values": {"opensubtitles_api_key": "K", "opensubtitles_username": "u", "opensubtitles_password": "p"}})
        c.put("/api/settings", json={"languages": [{"code": "pt-PT", "priority": "primary"}, {"code": "en", "priority": "secondary"}]})
        j = c.post("/api/scan", json={}).json()["job_id"]
        for _ in range(200):
            if c.get(f"/api/jobs/{j}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        yield c


def ep(client, n):
    items = client.get("/api/media", params={"kind": "episode"}).json()["items"]
    return next(i for i in items if f"S01E0{n}" in i["file_name"])


def test_dashboard_is_derived_from_db(client):
    d = client.get("/api/dashboard").json()
    t = d["totals"]
    assert (t["movies"], t["series"], t["episodes"], t["files"]) == (1, 1, 2, 3)
    pt = next(x for x in d["coverage_by_language"] if x["code"] == "pt-PT")
    assert pt["have"] == 1 and pt["total"] == 3 and t["missing_preferred"] == 2
    assert client.get("/api/sidebar").json()["missing"] == 2


def test_media_filters_and_search(client):
    assert client.get("/api/media", params={"status": "missing", "lang": "pt-PT"}).json()["total"] == 2
    assert client.get("/api/media", params={"status": "has", "lang": "pt-PT"}).json()["total"] == 1
    assert client.get("/api/media", params={"q": "filme"}).json()["total"] == 1
    assert client.get("/api/media", params={"q": "S01E02"}).json()["total"] == 1
    assert client.get("/api/media", params={"q": "2024"}).json()["total"] == 1
    assert client.get("/api/media", params={"q": "nada-disto"}).json()["total"] == 0
    assert len(client.get("/api/series").json()) == 1


def test_search_prepare_install_flow_and_conflicts(client):
    e2 = ep(client, 2)
    r = client.post(f"/api/media/{e2['id']}/search").json()
    assert r["errors"] == [] and r["results"][0]["evaluation"]["compat"] == "probable"
    assert r["results"][0]["evaluation"]["sync"] == "not_confirmed"
    result = r["results"][0]["result"]
    d = client.post(f"/api/media/{e2['id']}/prepare", json={"result": result, "language": "pt-PT"}).json()
    assert d["status"] == "prepared" and d["dest_path"].endswith("Minha Série - S01E02.pt-PT.srt")
    # E01 tem pt-PT como referência, mas E02 não: sem referência local => não confirmada
    assert d["evaluation"]["sync"] == "not_confirmed"
    assert not os.path.exists(d["dest_path"])               # preparar não escreve
    inst = client.post(f"/api/downloads/{d['id']}/install", json={"policy": "fail"}).json()
    assert inst["status"] == "installed" and os.path.exists(inst["dest_path"])
    # reanálise: já cobre pt-PT
    assert ep(client, 2)["missing"] == []
    # novo download para o mesmo destino => conflito, nada sobrescrito
    d2 = client.post(f"/api/media/{e2['id']}/prepare", json={"result": result, "language": "pt-PT"}).json()
    assert d2["conflict"] == "exists"
    before = open(inst["dest_path"], "rb").read()
    resp = client.post(f"/api/downloads/{d2['id']}/install", json={"policy": "fail"})
    assert resp.status_code == 409 and open(inst["dest_path"], "rb").read() == before
    ok = client.post(f"/api/downloads/{d2['id']}/install", json={"policy": "keep_both"}).json()
    assert ok["dest_path"].endswith(".2.srt")
    hist = client.get("/api/downloads", params={"status": "installed"}).json()
    assert hist["total"] == 2


def test_validation_against_reference_when_available(client):
    # E01 tem pt-PT sincronizada; candidata para E01 em inglês alinhada => validada vs referência
    e1 = ep(client, 1)
    client.state["blob"] = make_srt(120, 30_000, lang="en").encode()
    res = {"provider": "opensubtitles", "file_id": "9", "language": "en", "release": "Minha.Serie.S01E01.1080p", "season": 1, "episode": 1}
    d = client.post(f"/api/media/{e1['id']}/prepare", json={"result": res, "language": "en"}).json()
    assert d["evaluation"]["sync"] == "validated_reference"
    assert d["evaluation"]["alignment"]["reference"].endswith("pt-PT.srt")


def test_html_error_page_download_fails_cleanly(client):
    e2 = ep(client, 2)
    client.state["blob"] = b"<html><body>Too many requests</body></html>"
    res = {"provider": "opensubtitles", "file_id": "9", "language": "pt-PT", "season": 1, "episode": 2}
    r = client.post(f"/api/media/{e2['id']}/prepare", json={"result": res})
    assert r.status_code == 422 and "HTML" in r.json()["detail"]
    failed = client.get("/api/downloads", params={"status": "failed"}).json()
    assert failed["total"] == 1


def test_install_blocked_on_readonly_library(client):
    mv = client.get("/api/media", params={"kind": "movie"}).json()["items"][0]
    client.state["blob"] = make_srt(120, 30_000, lang="pt-PT").encode()
    res = {"provider": "opensubtitles", "file_id": "9", "language": "pt-PT"}
    d = client.post(f"/api/media/{mv['id']}/prepare", json={"result": res}).json()
    assert d["evaluation"]["writable_error"]
    r = client.post(f"/api/downloads/{d['id']}/install", json={"policy": "fail"})
    assert r.status_code == 403
    assert not any(p.name.endswith("pt-PT.srt") for p in (client.root / "Filmes").rglob("*"))


def test_batch_install_best_and_summary(client):
    ids = [i["id"] for i in client.get("/api/media", params={"status": "missing", "kind": "episode"}).json()["items"]]
    pv = client.post("/api/batch/preview", json={"media_ids": ids}).json()
    assert pv["needing_subtitles"] == 1 and pv["language_pairs"] >= 1
    j = client.post("/api/batch", json={"media_ids": ids, "install": True, "min_tier": "probable"}).json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{j}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["installed"] == 1, job
    assert ep(client, 2)["missing"] == []


def test_path_api_security(client):
    r = client.post("/api/libraries", json={"name": "x", "type": "movies", "path": "/etc", "writable": False})
    assert r.status_code == 400
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 403
    assert client.get("/api/browse", params={"path": str(client.root / "Filmes" / ".." / ".." / "..")}).status_code == 403
    r = client.post("/api/scan", json={"library_id": 1, "subpath": "/etc"})
    j = r.json()["job_id"]
    for _ in range(100):
        st = client.get(f"/api/jobs/{j}").json()
        if st["status"] != "queued" and st["status"] != "running":
            break
        time.sleep(0.05)
    assert st["status"] == "failed"


def test_secrets_never_exposed(client):
    for url in ("/api/providers", "/api/settings", "/api/integrations", "/api/meta"):
        assert "K" != client.get(url).json() and '"p"' not in client.get(url).text
    prov = client.get("/api/providers").json()[0]
    assert prov["secrets"]["opensubtitles_api_key"]["set"] is True
    assert "value" not in prov["secrets"]["opensubtitles_api_key"]
    assert client.core.secrets.path.stat().st_mode & 0o077 == 0 if client.core.secrets.path.exists() else True
    assert client.put("/api/secrets", json={"values": {"evil": "x"}}).status_code == 400


def test_ssrf_blocked_in_settings(client):
    r = client.put("/api/settings", json={"plex": {"enabled": True, "url": "http://169.254.169.254", "path_mappings": []}})
    assert r.status_code == 400


def test_auth_required_when_password_set(cfg, fake_probe):
    cfg.auth_password = "s3cret"
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/media").status_code == 401
        assert c.post("/api/auth/login", json={"password": "bad"}).status_code == 401
        assert c.post("/api/auth/login", json={"password": "s3cret"}).status_code == 200
        assert c.get("/api/media").status_code == 200


def test_cancel_job(client):
    core = client.core
    import threading
    started = threading.Event()
    def slow(ctx, p):
        ctx.set_total(100)
        started.set()
        for _ in range(100):
            ctx.step("x")
            time.sleep(0.05)
    core.jobs.register("slow", slow)
    j = core.jobs.submit("slow", "lenta", {})
    started.wait(3)
    assert client.post(f"/api/jobs/{j}/cancel").json()["cancelled"]
    for _ in range(100):
        if client.get(f"/api/jobs/{j}").json()["status"] == "cancelled":
            break
        time.sleep(0.05)
    assert client.get(f"/api/jobs/{j}").json()["status"] == "cancelled"
