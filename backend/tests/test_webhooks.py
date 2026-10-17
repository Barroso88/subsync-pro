import base64
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.opensubtitles import OpenSubtitles
from tests.conftest import make_srt, make_zip
from tests.test_api_e2e import provider_transport


@pytest.fixture
def client(cfg, fake_probe, media_root):
    state = {"blob": make_zip({"Novo.Filme.2025.pt.srt": make_srt(120, 30_000, lang="pt-PT").encode()})}
    OpenSubtitles._token = None
    app = create_app(cfg, transport=provider_transport(state))
    with TestClient(app) as c:
        c.core, c.root, c.state = app.state.core, media_root, state
        c.post("/api/libraries", json={"name": "Filmes", "type": "movies", "path": str(media_root / "Filmes"), "writable": True})
        c.put("/api/settings", json={"radarr": {"enabled": True, "url": "http://192.168.1.10:7878", "path_mappings": [{"remote": "/filmes", "local": str(media_root / "Filmes")}]},
                                     "languages": [{"code": "pt-PT", "priority": "primary"}]})
        yield c


def basic(tok):
    return {"Authorization": "Basic " + base64.b64encode(f"subsync:{tok}".encode()).decode()}


def wait(client, jid):
    for _ in range(200):
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("timeout")


def new_movie(client):
    d = client.root / "Filmes" / "Novo Filme (2025)"
    d.mkdir()
    (d / "Novo Filme (2025).mkv").write_bytes(b"\5" * 200_000)
    return "/filmes/Novo Filme (2025)/Novo Filme (2025).mkv"


def test_auth_required_and_test_event(client):
    tok = client.get("/api/webhooks/info").json()["token"]
    assert client.post("/api/webhooks/radarr", json={"eventType": "Test"}).status_code == 401
    assert client.post("/api/webhooks/radarr", json={"eventType": "Test"}, headers=basic("errado")).status_code == 401
    r = client.post("/api/webhooks/radarr", json={"eventType": "Test"}, headers=basic(tok))
    assert r.status_code == 202 and r.json()["test"] is True
    assert client.post("/api/webhooks/radarr", json={"eventType": "Test"}, headers={"X-Webhook-Token": tok}).status_code == 202
    assert client.post("/api/webhooks/outro", json={}, headers=basic(tok)).status_code == 404
    assert client.post("/api/webhooks/radarr", content=b"nao-json", headers=basic(tok)).status_code == 400
    assert client.post("/api/webhooks/radarr", json={"eventType": "Grab"}, headers=basic(tok)).json()["ignored"] == "Grab"


def test_import_scans_only_new_file_with_path_mapping_and_ids(client):
    tok = client.get("/api/webhooks/info").json()["token"]
    remote = new_movie(client)
    r = client.post("/api/webhooks/radarr", headers=basic(tok), json={"eventType": "Download", "isUpgrade": False,
        "movie": {"title": "Novo Filme", "tmdbId": 777, "imdbId": "tt7770000"}, "movieFile": {"path": remote}}).json()
    j = wait(client, r["job_id"])
    assert j["status"] == "done", j
    m = client.core.db.one("SELECT * FROM media_items WHERE file_name LIKE 'Novo Filme%'")
    assert m and m["tmdb_id"] == 777 and m["imdb_id"] == "tt7770000" and j["result"]["searched"] is False   # auto_download desligado


def test_import_triggers_subtitle_search_when_auto_enabled(client):
    client.put("/api/secrets", json={"values": {"opensubtitles_api_key": "K", "opensubtitles_username": "u", "opensubtitles_password": "p"}})
    client.put("/api/settings", json={"auto_download": True, "require_confirmation": True})
    tok = client.get("/api/webhooks/info").json()["token"]
    remote = new_movie(client)
    r = client.post("/api/webhooks/radarr", headers=basic(tok), json={"eventType": "Download", "movie": {"title": "Novo Filme"}, "movieFile": {"path": remote}}).json()
    j = wait(client, r["job_id"])
    assert j["status"] == "done" and j["result"]["searched"] is True, j
    # confirmação exigida: fica «por confirmar», nada instalado
    assert not list((client.root / "Filmes").rglob("Novo Filme (2025).pt-PT.srt"))


def test_unmapped_or_outside_path_is_rejected_safely(client):
    tok = client.get("/api/webhooks/info").json()["token"]
    for bad in ("/outro/sitio/x.mkv", "/filmes/../../etc/passwd"):
        r = client.post("/api/webhooks/radarr", headers=basic(tok), json={"eventType": "Download", "movieFile": {"path": bad}}).json()
        j = wait(client, r["job_id"])
        assert j["status"] == "failed" and "biblioteca" in j["error"] or "não encontrado" in j["error"]


def test_webhook_works_even_when_ui_auth_enabled(cfg, fake_probe):
    cfg.auth_password = "pw"
    with TestClient(create_app(cfg)) as c:
        assert c.post("/api/webhooks/radarr", json={"eventType": "Test"}).status_code == 401           # token em falta (não 401 da sessão)
        assert c.get("/api/webhooks/info").status_code == 401                                          # info exige sessão
        c.post("/api/auth/login", json={"password": "pw"})
        tok = c.get("/api/webhooks/info").json()["token"]
        c.cookies.clear()
        assert c.post("/api/webhooks/radarr", json={"eventType": "Test"}, headers=basic(tok)).status_code == 202
