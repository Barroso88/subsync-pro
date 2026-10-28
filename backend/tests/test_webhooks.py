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


def test_import_syncs_poster_and_metadata_of_the_new_item_without_full_scan(cfg, fake_probe, media_root):
    import httpx
    seen = []
    state = {"blob": make_zip({"x.srt": make_srt(120, 30_000, lang="pt-PT").encode()})}
    base = provider_transport(state)

    def h(req: httpx.Request):
        if req.url.host == "192.168.1.10":
            seen.append((req.url.path, dict(req.url.params)))
            assert req.headers["x-api-key"] == "RK"
            if req.url.path == "/api/v3/movie":
                return httpx.Response(200, json=[{"id": 6970, "title": "Filme Novo (PT)", "year": 2025, "imdbId": "tt7770000", "tmdbId": 777,
                                                  "movieFile": {"path": "/filmes/Novo Filme (2025)/Novo Filme (2025).mkv", "dateAdded": "2026-10-28T10:00:00Z"},
                                                  "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/abc.jpg", "url": "/MediaCover/1/poster.jpg"}]}])
            return httpx.Response(404)
        return base.handle_request(req)
    app = create_app(cfg, transport=httpx.MockTransport(h))
    with TestClient(app) as c:
        c.core, c.root = app.state.core, media_root
        c.post("/api/libraries", json={"name": "Filmes", "type": "movies", "path": str(media_root / "Filmes"), "writable": True})
        c.put("/api/settings", json={"radarr": {"enabled": True, "url": "http://192.168.1.10:7878", "path_mappings": [{"remote": "/filmes", "local": str(media_root / "Filmes")}]},
                                     "languages": [{"code": "pt-PT", "priority": "primary"}]})
        c.put("/api/secrets", json={"values": {"radarr_api_key": "RK"}})
        tok = c.get("/api/webhooks/info").json()["token"]
        remote = new_movie(c)
        r = c.post("/api/webhooks/radarr", headers=basic(tok), json={"eventType": "Download", "movie": {"title": "Novo Filme", "tmdbId": 777, "imdbId": "tt7770000"},
                                                                     "movieFile": {"path": remote}}).json()
        j = wait(c, r["job_id"])
        assert j["status"] == "done" and j["result"]["synced"]["radarr"] is True, j
        m = c.core.db.one("SELECT * FROM media_items WHERE file_name LIKE 'Novo Filme%'")
        assert m["radarr_id"] == 6970 and m["radarr_title"] == "Filme Novo (PT)" and "image.tmdb.org/t/p/w342/abc.jpg" in m["poster"] and m["added_at"] == "2026-10-28T10:00:00Z"
        assert seen == [("/api/v3/movie", {"tmdbId": "777"})]                       # só um pedido dirigido, sem descarregar a lista inteira
        item = next(i for i in c.get("/api/media", params={"kind": "movie", "q": "Novo Filme"}).json()["items"])
        assert item["has_poster"] and item["title"] == "Filme Novo (PT)"            # capa e título do Radarr logo, sem «Analisar biblioteca»


def test_sync_new_item_takes_plex_poster_with_priority_and_asks_only_for_recent(cfg, fake_probe, media_root):
    import httpx
    seen = []

    def h(req: httpx.Request):
        if req.url.host == "192.168.1.20":
            seen.append((req.url.path, dict(req.url.params)))
            if req.url.path == "/library/sections":
                return httpx.Response(200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}})
            if req.url.path == "/library/sections/1/all":
                return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": 99, "title": "Novo Filme", "thumb": "/library/metadata/99/thumb/1", "addedAt": 1793000000,
                                                                              "Media": [{"Part": [{"file": "/movies/Novo Filme (2025)/Novo Filme (2025).mkv"}]}]}]}})
        return httpx.Response(404)
    app = create_app(cfg, transport=httpx.MockTransport(h))
    with TestClient(app) as c:
        core = app.state.core
        c.post("/api/libraries", json={"name": "Filmes", "type": "movies", "path": str(media_root / "Filmes"), "writable": True})
        c.put("/api/settings", json={"plex": {"enabled": True, "url": "http://192.168.1.20:32400", "path_mappings": [{"remote": "/movies", "local": str(media_root / "Filmes")}]}})
        c.put("/api/secrets", json={"values": {"plex_token": "PT"}})
        new_movie(type("X", (), {"root": media_root}))
        wait(c, c.post("/api/scan", json={}).json()["job_id"])
        mid = core.db.one("SELECT id FROM media_items WHERE file_name LIKE 'Novo Filme%'")["id"]
        core.db.execute("UPDATE media_items SET poster='https://image.tmdb.org/t/p/w342/abc.jpg' WHERE id=?", (mid,))
        res = core.sync_new_item(mid)
        m = core.db.one("SELECT * FROM media_items WHERE id=?", (mid,))
        assert res["plex"] is True and m["plex_key"] == "99" and m["poster"].startswith("plex:/library/metadata/99/thumb/1|https://image.tmdb.org")   # Plex à frente, TMDb como alternativa
        assert any(p == "/library/sections/1/all" and q.get("sort") == "addedAt:desc" and q.get("X-Plex-Container-Size") == "80" for p, q in seen)   # só os mais recentes
