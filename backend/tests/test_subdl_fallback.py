import httpx
import pytest

from app import coverage as cov
from app.providers.base import ProviderContext, ProviderError, SearchQuery, SubtitleResult
from app.providers.subdl import SubDL, from_subdl_lang, to_subdl_lang
from tests.conftest import make_srt, wait_job
from tests.test_scanner_coverage import add_libs


def prov(handler, key="K"):
    ctx = ProviderContext(lambda k: key if k == "subdl_api_key" else "", lambda: {}, lambda **kw: None,
                          lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return SubDL(ctx)


def test_language_mapping():
    assert to_subdl_lang("pt-BR") == "BR_PT" and to_subdl_lang("pt-PT") == "PT" and to_subdl_lang("en") == "EN"
    assert from_subdl_lang("BR_PT") == "pt-BR" and from_subdl_lang("EN") == "en"
    assert from_subdl_lang("PT") == "pt"          # variante não confirmada pelo fornecedor


def test_search_and_download_with_validation():
    seen = {}

    def h(req: httpx.Request):
        if req.url.host == "api.subdl.com":
            seen["p"] = dict(req.url.params)
            return httpx.Response(200, json={"status": True, "subtitles": [
                {"release_name": "Show.S01E02.1080p", "name": "SUBDL::x", "lang": "BR_PT", "author": "a", "url": "/subtitle/123-456.zip",
                 "subtitlePage": "/subtitle/123/x", "season": 1, "episode": 2, "hi": False},
                {"release_name": "evil", "lang": "EN", "url": "/../../etc/passwd"}]})
        assert req.url.host == "dl.subdl.com" and req.url.path == "/subtitle/123-456.zip"
        return httpx.Response(200, content=b"PK-fake")

    p = prov(h)
    out = p.search(SearchQuery("episode", "Show", season=1, episode=2, series_title="Show", languages=["pt-PT", "pt-BR"], imdb_id="tt123"))
    assert seen["p"]["languages"] == "PT,BR_PT" and seen["p"]["type"] == "tv" and seen["p"]["imdb_id"] == "tt123"
    assert len(out) == 1 and out[0].language == "pt-BR" and not out[0].hash_match   # id malicioso descartado
    assert p.download(out[0]).data == b"PK-fake"
    with pytest.raises(ProviderError):
        p.download(SubtitleResult(provider="subdl", file_id="https://evil/x.zip", language="en"))


def test_auth_error_and_no_key():
    p = prov(lambda r: httpx.Response(200, json={"status": False, "error": "Invalid API key"}))
    with pytest.raises(ProviderError) as e:
        p.search(SearchQuery("movie", "X"))
    assert e.value.auth
    with pytest.raises(ProviderError):
        prov(lambda r: httpx.Response(200), key="").search(SearchQuery("movie", "X"))


def test_pt_br_fallback_coverage(core, media_root):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt-BR.srt").write_text(make_srt(60, lang="pt-PT"), "utf-8")
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}])
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    assert cov.compute(core.db, core.settings, [mid])[mid]["missing"] == ["pt-PT"]      # desligado: em falta
    core.settings.set("accept_pt_br_fallback", True)
    c = cov.compute(core.db, core.settings, [mid])[mid]
    assert c["missing"] == [] and c["fallback"] == {"pt-PT": "pt-BR"}                     # ligado: coberto, assinalado
    q = core.build_query(core.media_dict(mid))
    assert "pt-BR" in q.languages
