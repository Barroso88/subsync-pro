import json

import httpx
import pytest

from app.evaluator import evaluate_content, evaluate_metadata
from app.integrations.common import IntegrationError, map_local_to_remote, map_remote_to_local, validate_service_url
from app.integrations.plex import Plex
from app.integrations.arr import Arr
from app.moviehash import moviehash
from app.providers.base import ProviderContext, ProviderError, RateLimiter, SearchQuery, SubtitleResult
from app.providers.opensubtitles import OpenSubtitles
from app.subtitles.parser import analyze, parse_srt
from tests.conftest import make_srt

EP = {"kind": "episode", "series_title": "Show", "season": 1, "episode": 2, "year": None, "title": "Show S01E02", "file_name": "Show.S01E02.1080p.WEB-DL.x264-GRP.mkv",
      "fps": 23.976, "duration": 3700.0, "imdb_id": None, "tmdb_id": None}


def res(**kw):
    base = dict(provider="opensubtitles", file_id="1", language="pt-PT", release="Show.S01E02.1080p.WEB-DL.x264-GRP", season=1, episode=2)
    base.update(kw)
    return SubtitleResult(**base)


def test_hash_match_is_confirmed_but_sync_not_confirmed():
    ev = evaluate_metadata(EP, res(hash_match=True))
    assert ev.compat == "hash" and ev.sync == "not_confirmed"
    assert any("hash" in f.label.lower() for f in ev.factors)
    d = ev.to_dict()
    assert "não é uma probabilidade" in d["disclaimer"]


def test_wrong_episode_is_incompatible():
    ev = evaluate_metadata(EP, res(season=1, episode=5))
    assert ev.compat == "incompatible"


def test_metadata_match_without_hash_is_only_probable():
    ev = evaluate_metadata(EP, res(download_count=5000, fps=23.976))
    assert ev.compat == "probable" and ev.sync == "not_confirmed"
    weak = evaluate_metadata(EP, res(release="Completely.Different.720p.HDTV-XYZ", season=None, episode=None))
    assert weak.compat == "weak"


def test_ai_translation_and_hi_penalised():
    base = evaluate_metadata(EP, res()).score
    assert evaluate_metadata(EP, res(ai_translated=True)).score < base
    assert evaluate_metadata(EP, res(hearing_impaired=True), hi_policy="exclude").score < base
    assert evaluate_metadata(EP, res(hearing_impaired=True), hi_policy="include").score == base


def _content(ev, cand_srt, ref_srt=None, dur=3700.0):
    parsed = parse_srt(cand_srt)
    an = analyze(parsed, dur, size=len(cand_srt))
    ref = ("ref.pt-PT.srt", parse_srt(ref_srt)) if ref_srt else None
    return evaluate_content(ev, dict(EP, duration=dur), parsed, an, ref)


def test_duration_match_alone_never_validates_sync():
    ev = _content(evaluate_metadata(EP, res(hash_match=True)), make_srt(120, 30_000))
    assert ev.sync == "not_confirmed"
    assert any("não prova" in f.label for f in ev.factors)


def test_validated_only_against_reference():
    ev = _content(evaluate_metadata(EP, res()), make_srt(120, 30_000, lang="en"), make_srt(120, 30_000, lang="pt-PT"))
    assert ev.sync == "validated_reference" and ev.alignment["match_ratio"] > 0.9
    assert any("não foi analisado o áudio" in n for n in ev.notes)


def test_offset_is_flagged_suspect():
    ev = _content(evaluate_metadata(EP, res()), make_srt(120, 30_000, lang="en", offset_ms=9000), make_srt(120, 30_000, lang="pt-PT"))
    assert ev.sync == "suspect" and abs(ev.alignment["offset_ms"]) > 1500


def test_subtitle_longer_than_video_incompatible():
    ev = _content(evaluate_metadata(EP, res(hash_match=True)), make_srt(300, 30_000), dur=3700.0)
    assert ev.compat == "incompatible" and ev.sync == "suspect"


def test_moviehash_known_algorithm(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"\x01" * (65536 * 3))
    h = moviehash(str(p))
    assert len(h) == 16 and h == moviehash(str(p))
    small = tmp_path / "s.bin"
    small.write_bytes(b"abc")
    assert moviehash(str(small)) is None


# ---- OpenSubtitles com transporte simulado -------------------------------------

def os_provider(handler, secrets=None, limiter=None):
    secrets = secrets or {"opensubtitles_api_key": "KEY", "opensubtitles_username": "u", "opensubtitles_password": "p"}
    rec = []
    ctx = ProviderContext(lambda k: secrets.get(k, ""), lambda: {}, lambda **kw: rec.append(kw),
                          lambda: httpx.Client(transport=httpx.MockTransport(handler)), limiter)
    OpenSubtitles._token = None
    return OpenSubtitles(ctx), rec


SEARCH_JSON = {"total_count": 1, "data": [{"id": "1", "attributes": {
    "language": "pt-PT", "download_count": 1234, "hearing_impaired": False, "moviehash_match": True, "fps": 23.976,
    "release": "Show.S01E02.1080p.WEB-DL-GRP", "from_trusted": True, "ai_translated": False,
    "uploader": {"name": "bob"}, "url": "https://www.opensubtitles.com/x",
    "feature_details": {"movie_name": "Show", "season_number": 1, "episode_number": 2, "year": 2020, "imdb_id": 123, "tmdb_id": 9},
    "files": [{"file_id": 777, "file_name": "Show.S01E02.srt"}]}}]}


def test_opensubtitles_search_params_and_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["h"] = req.url, req.headers
        return httpx.Response(200, json=SEARCH_JSON)

    p, rec = os_provider(handler)
    q = SearchQuery("episode", "Show", season=1, episode=2, series_title="Show", moviehash="abcd", languages=["pt-PT", "en"])
    out = p.search(q)
    prm = dict(seen["url"].params)
    assert prm["moviehash"] == "abcd" and prm["season_number"] == "1" and prm["episode_number"] == "2" and prm["query"] == "show"
    assert prm["languages"] == "en,pt-pt" and prm["type"] == "episode"
    assert seen["h"]["api-key"] == "KEY"
    assert out[0].hash_match and out[0].language == "pt-PT" and out[0].file_id == "777" and out[0].download_count == 1234


def test_opensubtitles_download_flow_and_host_allowlist():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "JWT"})
        if req.url.path.endswith("/download"):
            assert req.headers["authorization"] == "Bearer JWT"
            return httpx.Response(200, json={"link": "https://dl.opensubtitles.com/f/1.srt", "file_name": "a.srt", "remaining": 18})
        return httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")

    p, rec = os_provider(handler)
    f = p.download(res(file_id="777"))
    assert f.data.startswith(b"1\n") and f.remaining == 18
    assert any(r.get("remaining") == 18 for r in rec)

    def evil(req):
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "JWT"})
        return httpx.Response(200, json={"link": "https://evil.example.com/x.srt"})

    p, _ = os_provider(evil)
    with pytest.raises(ProviderError, match="não autorizado"):
        p.download(res(file_id="1"))
    def http_link(req):
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "JWT"})
        return httpx.Response(200, json={"link": "http://dl.opensubtitles.com/x.srt"})
    p, _ = os_provider(http_link)
    with pytest.raises(ProviderError, match="HTTPS"):
        p.download(res(file_id="1"))


def test_opensubtitles_errors_are_reported_not_leaked():
    p, rec = os_provider(lambda r: httpx.Response(401, json={"message": "bad key KEY"}))
    with pytest.raises(ProviderError) as e:
        p.search(SearchQuery("movie", "X"))
    assert e.value.auth and "KEY" not in str(e.value)
    assert rec[-1]["auth_ok"] is False
    p, _ = os_provider(lambda r: httpx.Response(429, headers={"retry-after": "7"}))
    with pytest.raises(ProviderError) as e:
        p.search(SearchQuery("movie", "X"))
    assert e.value.rate_limited and e.value.retry_after == 7
    p, _ = os_provider(lambda r: httpx.Response(200), secrets={})
    with pytest.raises(ProviderError):
        p.search(SearchQuery("movie", "X"))


def test_download_size_limit_and_oversize():
    def handler(req):
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "T"})
        if req.url.path.endswith("/download"):
            return httpx.Response(200, json={"link": "https://dl.opensubtitles.com/big"})
        return httpx.Response(200, content=b"x" * (11 * 1024 * 1024))
    p, _ = os_provider(handler)
    with pytest.raises(ProviderError, match="tamanho"):
        p.download(res(file_id="1"))


def test_rate_limiter_blocks_when_exhausted():
    rl = RateLimiter(2)
    rl.acquire(); rl.acquire()
    with pytest.raises(ProviderError) as e:
        rl.acquire(wait=False)
    assert e.value.rate_limited


# ---- integrações --------------------------------------------------------------

def test_path_mapping_longest_prefix():
    m = [{"remote": "/data", "local": "/media"}, {"remote": "/data/tv", "local": "/media/Séries"}]
    assert map_remote_to_local("/data/tv/Show/a.mkv", m) == "/media/Séries/Show/a.mkv"
    assert map_remote_to_local("/data/movies/a.mkv", m) == "/media/movies/a.mkv"
    assert map_remote_to_local("/other/a.mkv", m) == "/other/a.mkv"
    assert map_local_to_remote("/media/Séries/x.mkv", m) == "/data/tv/x.mkv"


def test_ssrf_validation():
    for bad in ("file:///etc/passwd", "ftp://x", "http://169.254.169.254/latest", "http://user:pw@127.0.0.1", "http://0.0.0.0:80", "gopher://x"):
        with pytest.raises(IntegrationError):
            validate_service_url(bad)
    assert validate_service_url("http://192.168.1.10:32400/") == "http://192.168.1.10:32400"


def test_radarr_and_plex_parsing():
    def h(req: httpx.Request):
        if req.url.path == "/api/v3/movie":
            assert req.headers["x-api-key"] == "K"
            return httpx.Response(200, json=[{"title": "F", "year": 2024, "imdbId": "tt1", "tmdbId": 5,
                                              "movieFile": {"path": "/movies/F (2024)/F (2024).mkv"},
                                              "images": [{"coverType": "poster", "url": "/MediaCover/1/poster.jpg"}]}, {"title": "no file"}])
        if req.url.path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}})
        if req.url.path == "/library/sections/1/all":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": 42, "title": "F", "thumb": "/library/metadata/42/thumb/1",
                                                                              "Media": [{"Part": [{"file": "/movies/F (2024)/F (2024).mkv"}]}]}]}})
        return httpx.Response(404)
    t = httpx.MockTransport(h)
    maps = [{"remote": "/movies", "local": "/media/Filmes"}]
    m = Arr("radarr", "http://r", "K", maps, t, validate=False).movies()
    assert m == [{"id": None, "path": "/media/Filmes/F (2024)/F (2024).mkv", "title": "F", "year": 2024, "imdb_id": "tt1", "tmdb_id": 5,
                  "poster": "arr:radarr:/MediaCover/1/poster.jpg"}]
    it = Plex("http://p", "T", maps, t, validate=False).items()
    assert it[0]["rating_key"] == "42" and it[0]["path"] == "/media/Filmes/F (2024)/F (2024).mkv"


def test_opensubtitles_follows_normalising_redirect_within_domain():
    hops = []

    def h(req: httpx.Request):
        hops.append(str(req.url))
        if req.url.path.endswith("/subtitles") and "order_by" in str(req.url.params) and len(hops) == 1:
            return httpx.Response(301, headers={"location": "https://api.opensubtitles.com/api/v1/subtitles?languages=en&query=x&type=movie"})
        return httpx.Response(200, json={"data": []})

    p, _ = os_provider(h)
    assert p.search(SearchQuery("movie", "X", languages=["en"])) == [] and len(hops) == 2

    def evil(req):
        return httpx.Response(302, headers={"location": "https://evil.example.com/x"})
    p, _ = os_provider(evil)
    with pytest.raises(ProviderError, match="não autorizado"):
        p.search(SearchQuery("movie", "X"))


def test_opensubtitles_params_lowercase_sorted():
    prm = OpenSubtitles.build_params(SearchQuery("movie", "Born To Be King", year=2000, languages=["pt-PT", "en"], moviehash="ABCDEF0123456789"))
    assert list(prm) == sorted(prm) and prm["query"] == "born to be king" and prm["moviehash"] == "abcdef0123456789"
    assert prm["languages"] == "en,pt-pt"


def test_opensubtitles_invalid_json_error_has_http_status():
    p, _ = os_provider(lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    with pytest.raises(ProviderError, match=r"HTTP 200, text/html"):
        p.search(SearchQuery("movie", "X"))


def test_year_not_sent_when_identifier_present_and_modes():
    q = SearchQuery("movie", "Web Of Lies", year=2010, imdb_id="tt1493287", moviehash="ABC123", languages=["pt-PT"])
    full = OpenSubtitles.build_params(q)
    assert full["imdb_id"] == "1493287" and "year" not in full and full["moviehash"] == "abc123"
    from dataclasses import replace
    assert set(OpenSubtitles.build_params(replace(q, mode="hash"))) == {"order_by", "order_direction", "languages", "hearing_impaired", "foreign_parts_only", "moviehash"}
    t = OpenSubtitles.build_params(replace(q, mode="title"))
    assert t["query"] == "web of lies" and "imdb_id" not in t and "year" not in t and "moviehash" not in t
    no_id = OpenSubtitles.build_params(SearchQuery("movie", "Some Film", year=2001))
    assert no_id["year"] == 2001 and no_id["query"] == "some film"


def test_radarr_id_and_title_are_exposed():
    def h(req: httpx.Request):
        return httpx.Response(200, json=[{"id": 7111, "title": "Clean Hands", "year": 2024, "imdbId": "tt1", "tmdbId": 5, "movieFile": {"path": "/movies/a/a.mkv"}}])
    m = Arr("radarr", "http://r", "K", [], httpx.MockTransport(h), validate=False).movies()
    assert m[0]["id"] == 7111 and m[0]["title"] == "Clean Hands"
