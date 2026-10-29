import httpx
import pytest

from app.integrations.bazarr import Bazarr, BAZARR_LANG
from app.integrations.common import IntegrationError
from app.services import ServiceError
from tests.conftest import make_srt, wait_job
from tests.test_scanner_coverage import add_libs


def test_client_params_and_error_mapping():
    seen = {}

    def h(req: httpx.Request):
        seen["m"], seen["p"], seen["h"] = req.method, dict(req.url.params), req.headers["x-api-key"]
        return httpx.Response(204)

    b = Bazarr("http://bz", "K", httpx.MockTransport(h), validate=False)
    assert b.download_movie_subtitle(7111, "pt-PT")["ok"]
    assert seen["m"] == "PATCH" and seen["p"] == {"radarrid": "7111", "language": "pt", "forced": "False", "hi": "False"} and seen["h"] == "K"
    b.download_movie_subtitle(1, "pt-BR")
    assert seen["p"]["language"] == "pb" and BAZARR_LANG["pt-BR"] == "pb"
    with pytest.raises(IntegrationError, match="API key"):
        Bazarr("http://bz", "K", httpx.MockTransport(lambda r: httpx.Response(401)), validate=False).test()
    with pytest.raises(IntegrationError, match="não conhece"):
        Bazarr("http://bz", "K", httpx.MockTransport(lambda r: httpx.Response(404)), validate=False).download_movie_subtitle(1, "pt-PT")
    r = Bazarr("http://bz", "K", httpx.MockTransport(lambda r: httpx.Response(500, json={"message": "sem resultados"})), validate=False).download_movie_subtitle(1, "pt-PT")
    assert r["ok"] is False and "sem resultados" in r["message"]
    with pytest.raises(IntegrationError):
        b.download_movie_subtitle(1, "xx")
    assert Bazarr("http://bz", "K", httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"bazarr_version": "1.5.0"}})), validate=False).test()["version"] == "1.5.0"


@pytest.fixture
def ready(core, media_root):
    a, b = add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    mv = core.db.one("SELECT * FROM media_items WHERE kind='movie'")
    core.db.execute("UPDATE media_items SET radarr_id=7111 WHERE id=?", (mv["id"],))
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}])
    core.settings.set("bazarr", {"enabled": True, "url": "http://192.168.1.10:6767", "path_mappings": []})
    core.secrets.set("bazarr_api_key", "K")
    return core, dict(mv), media_root


def _transport(core, media_root, write=True, status=204):
    def h(req: httpx.Request):
        if write:
            d = media_root / "Filmes" / "Filme Teste (2024)"
            (d / "Filme Teste (2024).pt-PT.srt").write_text(make_srt(60, lang="pt-PT"), "utf-8")    # o «Bazarr» escreve o ficheiro
        return httpx.Response(status)
    core.transport = httpx.MockTransport(h)


def test_request_finds_and_validates_new_subtitle(ready):
    core, mv, root = ready
    _transport(core, root)
    r = core.bazarr_request(mv["id"], "pt-PT")
    assert r["found"] is True and r["had_before"] is False
    assert core.db.one("SELECT status,lang FROM subtitles WHERE path LIKE '%pt-PT.srt'")["status"] == "ok"      # validada pela app
    assert any("Bazarr" in e["message"] for e in core.db.query("SELECT message FROM events"))


def test_request_reports_no_result_honestly(ready):
    core, mv, root = ready
    _transport(core, root, write=False, status=500)
    r = core.bazarr_request(mv["id"], "pt-PT")
    assert r["found"] is False


def test_empty_file_from_bazarr_is_not_counted_as_found(ready):
    core, mv, root = ready
    def h(req):
        (root / "Filmes" / "Filme Teste (2024)" / "Filme Teste (2024).pt-PT.srt").write_bytes(b"")
        return httpx.Response(204)
    core.transport = httpx.MockTransport(h)
    assert core.bazarr_request(mv["id"], "pt-PT")["found"] is False         # o Bazarr diria «existe»; a app vê 0 bytes


def test_refuses_readonly_library_and_unknown_radarr_id(ready):
    core, mv, root = ready
    core.db.execute("UPDATE libraries SET writable=0")
    with pytest.raises(ServiceError, match="só de leitura"):
        core.bazarr_request(mv["id"], "pt-PT")
    core.db.execute("UPDATE libraries SET writable=1")
    core.db.execute("UPDATE media_items SET radarr_id=NULL WHERE id=?", (mv["id"],))
    with pytest.raises(ServiceError, match="id do Radarr"):
        core.bazarr_request(mv["id"], "pt-PT")


def test_batch_job_counts(ready):
    core, mv, root = ready
    _transport(core, root)
    j = core.start_bazarr_batch([mv["id"]], ["pt-PT"])
    job = wait_job(core, j)
    assert job["status"] == "done"
    import json
    assert json.loads(job["result"])["found"] == 1


def test_origin_of_bazarr_downloads_comes_from_its_history_and_is_stored(ready):
    core, mv, root = ready
    d = root / "Filmes" / "Filme Teste (2024)"
    seen = []

    def h(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "GET" and req.url.path == "/api/movies/history":
            return httpx.Response(200, json={"total": 3, "data": [
                {"radarrId": 7111, "action": 1, "provider": "opensubtitlescom", "score": "99.44%", "parsed_timestamp": "10/29/26 23:33:29",
                 "subtitles_path": "/movies/Filme Teste (2024)/Filme Teste (2024).pt-PT.srt", "language": {"name": "Portuguese"}},
                {"radarrId": 7111, "action": 1, "provider": "legendasdivx", "score": "80%", "subtitles_path": "/movies/Filme Teste (2024)/Filme Teste (2024).pt-PT.srt"},   # mais antigo: ignora
                {"radarrId": 7111, "action": 4, "provider": "x", "subtitles_path": None}]})
        (d / "Filme Teste (2024).pt-PT.srt").write_text(make_srt(60, lang="pt-PT"), "utf-8")
        return httpx.Response(204)
    core.transport = httpx.MockTransport(h)
    r = core.bazarr_request(mv["id"], "pt-PT")
    assert r["found"] and r["source"]["provider_label"] == "OpenSubtitles.com" and "Bazarr · OpenSubtitles.com, score 99.44%" in r["message"]
    assert ("GET", "/api/movies/history", {"start": "0", "length": "60", "radarrid": "7111"}) in seen
    o = core.media_origins(mv["id"])
    assert len(o) == 1 and o[0]["origin"] == "bazarr" and o[0]["provider"] == "opensubtitlescom" and o[0]["detail"] == "score 99.44%"
    n_hist = sum(1 for m, p, _ in seen if p == "/api/movies/history")
    core.media_origins(mv["id"])
    assert sum(1 for m, p, _ in seen if p == "/api/movies/history") == n_hist          # já guardado: não volta a perguntar ao Bazarr


def test_origin_unknown_when_bazarr_has_no_record_and_unreachable_is_harmless(ready):
    core, mv, root = ready
    d = root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt-PT.srt").write_text(make_srt(60, lang="pt-PT"), "utf-8")
    core.scanner.scan_library(dict(core.db.one("SELECT * FROM libraries WHERE id=?", (mv["library_id"],))), None, mv["dir"])
    core.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
    assert core.media_origins(mv["id"]) == []                                           # sem registo: não se inventa origem
    core.transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("sem rede")))
    assert core.media_origins(mv["id"]) == []                                           # Bazarr em baixo: não rebenta
