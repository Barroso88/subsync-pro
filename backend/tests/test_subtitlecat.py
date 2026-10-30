import httpx
import pytest

from app.providers.base import ProviderContext, ProviderError, SearchQuery
from app.providers.subtitlecat import Subtitlecat, UA

SEARCH = """<table><tr>
<td><a href="subs/1555/Man%20On%20Fire%202004%201080p%20BluRay%20HEVC%20x265%205.1%20BONE.html">Man On Fire 2004 1080p BluRay HEVC x265 5.1 BONE</a> (translated from Croatian)</td>
<td class="sub-table__stars">&nbsp;</td>
<td class="sub-table__metric"><span class="sub-table__metric-label"><img src="a.png" alt=""> Size</span><span class="sub-table__metric-value">47 KB</span></td>
<td class="sub-table__metric"><span class="sub-table__metric-label"><img src="b.png" alt=""> Downloads</span><span class="sub-table__metric-value">12<span class="sub-table__metric-unit"> downloads</span></span></td>
</tr>
<tr>
<td><a href="subs/1754/Hawaii.Five-O.S09E05.Man.On.Fire.1080p.html">Hawaii.Five-O.S09E05.Man.On.Fire.1080p</a> (translated from English)</td>
<td class="sub-table__metric"><span class="sub-table__metric-label"><img src="a.png" alt=""> Size</span><span class="sub-table__metric-value">47 KB</span></td>
</tr>
<tr>
<td><a href="subs/1600/Man.on.Fire.2004.REMASTERED.html">Man.on.Fire.2004.REMASTERED</a> (translated from English)</td>
<td class="sub-table__metric"><span class="sub-table__metric-label"><img src="a.png" alt=""> Size</span><span class="sub-table__metric-value">60 KB</span></td>
<td class="sub-table__metric"><span class="sub-table__metric-label"><img src="b.png" alt=""> Downloads</span><span class="sub-table__metric-value">3<span class="sub-table__metric-unit"> downloads</span></span></td>
</tr></table>"""

PAGE_1555 = """<div class="sub-single"><span>Afrikaans</span><span><button id="af" onclick="translate_from_server_folder('af','x-orig.srt','/subs/1555/')">Translate</button></span></div>
<div class="sub-single"><span>Croatian</span><span><a id="download_hr"  onclick="log_download(1);" href="/subs/1555/Man On Fire 2004 1080p BluRay HEVC x265 5.1 BONE-hr.srt" class="green-link">Download</a></span></div>
<div class="sub-single"><span>Portuguese (Brazil)</span><span><a id="download_pt-BR"  onclick="log_download(2);" href="/subs/1580/Man On Fire 2004 1080p BluRay HEVC x265 5.1 BONE-pt-BR.srt" class="green-link">Download</a></span></div>
<div class="sub-single"><span>Portuguese</span><span><a id="download_pt"  onclick="log_download(3);" href="/subs/1581/Man On Fire 2004 1080p BluRay HEVC x265 5.1 BONE-pt.srt" class="green-link">Download</a></span></div>
<div class="sub-single"><span>English</span><span><a id="download_en"  onclick="log_download(4);" href="/subs/1558/Man On Fire 2004 1080p BluRay HEVC x265 5.1 BONE-en.srt" class="green-link">Download</a></span></div>
<a href="/subs/9/evil/../../x.srt">outro</a><a id="download_xx" href="/subs/bad/path.srt">mau</a>"""

PAGE_1600 = """<div class="sub-single"><span>English</span><span><a id="download_en" href="/subs/1600/Man.on.Fire.2004.REMASTERED-en.srt" class="green-link">Download</a></span></div>"""


def make(handler, per_minute=100):
    from app.providers.base import RateLimiter
    ctx = ProviderContext(lambda k: "", lambda: {"max_pages": 3}, lambda **kw: None, lambda: httpx.Client(transport=httpx.MockTransport(handler)), RateLimiter(per_minute))
    Subtitlecat._robots = None
    return Subtitlecat(ctx)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(Subtitlecat, "_sleep", staticmethod(lambda s: None))


def site(seen, robots="User-agent: *\nAllow: /\n", blocked=None):
    def h(req: httpx.Request):
        seen.append((req.url.path, dict(req.url.params), req.headers["user-agent"]))
        if blocked:
            return blocked
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if req.url.path == "/index.php":
            return httpx.Response(200, text=SEARCH, headers={"content-type": "text/html"})
        if req.url.path.startswith("/subs/1555/"):
            return httpx.Response(200, text=PAGE_1555, headers={"content-type": "text/html"})
        if req.url.path.startswith("/subs/1600/") and req.url.path.endswith(".html"):
            return httpx.Response(200, text=PAGE_1600, headers={"content-type": "text/html"})
        if req.url.path.endswith(".srt"):
            return httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nOla\n")
        return httpx.Response(404)
    return h


def q(**kw):
    base = dict(kind="movie", title="Man on Fire", year=2004, languages=["pt-PT", "pt-BR"], file_name="Man.on.Fire.2004.1080p.BluRay.x264-GRP.mkv", max_results=20)
    base.update(kw)
    return SearchQuery(**base)


def test_parse_search_rows_and_only_ready_downloads():
    rows = Subtitlecat.parse_results(SEARCH)
    assert [r["name"] for r in rows][:1] == ["Man On Fire 2004 1080p BluRay HEVC x265 5.1 BONE"] and rows[0]["orig"] == "Croatian" and rows[0]["downloads"] == 12
    d = Subtitlecat.parse_downloads(PAGE_1555)
    assert [c for c, _ in d] == ["hr", "pt-BR", "pt", "en"]                       # «Translate» (tradução no servidor deles) e caminhos maus ficam de fora


def test_search_is_polite_filters_titles_and_marks_machine_translation():
    seen = []
    res = make(site(seen)).search(q())
    paths = [p for p, _, _ in seen]
    assert paths[0] == "/robots.txt" and paths.count("/robots.txt") == 1               # lê o robots.txt uma vez e respeita-o
    assert ("/index.php", {"search": "Man on Fire 2004"}) in [(p, qq) for p, qq, _ in seen]
    assert all("SubSyncPro" in ua for _, _, ua in seen)                                # identifica-se
    assert not any("Hawaii" in p for p in paths)                                       # episódio de outra série: nem sequer se abre a página
    langs = sorted((r.language, r.file_id.rsplit("-", 1)[-1]) for r in res)
    assert [r.language for r in res].count("pt-BR") == 1 and "pt" in [r.language for r in res] and "en" not in [r.language for r in res]
    assert all(r.provider == "subtitlecat" and r.machine_translated for r in res)       # tradução de croata/inglês: assinalada
    assert res[0].download_count == 12 and res[0].page_url.endswith(".html")


def test_robots_disallow_stops_everything():
    seen = []
    p = make(site(seen, robots="User-agent: *\nDisallow: /\n"))
    with pytest.raises(ProviderError, match="robots.txt"):
        p.search(q())
    assert [x[0] for x in seen] == ["/robots.txt"]                                    # nenhum acesso às páginas


@pytest.mark.parametrize("resp", [httpx.Response(403, text="no"), httpx.Response(429), httpx.Response(200, text="<html>Just a moment... captcha</html>", headers={"content-type": "text/html"})])
def test_blocking_or_captcha_is_reported_and_not_bypassed(resp):
    seen = []

    def h(req):
        seen.append(req.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /\n") if req.url.path == "/robots.txt" else resp
    with pytest.raises(ProviderError) as e:
        make(h).search(q())
    assert e.value.rate_limited and "não contorna" in str(e.value) and seen.count("/index.php") == 1     # não insiste


def test_download_validates_path_host_and_rejects_html():
    seen = []
    p = make(site(seen))
    res = p.search(q())
    pt = next(r for r in res if r.language == "pt-BR")
    d = p.download(pt)
    assert d.name.endswith("-pt-BR.srt") and b"Ola" in d.data
    assert any(path.endswith(".srt") and path.startswith("/subs/1580/") for path, _, _ in seen)
    for bad in ("/subs/1/../../etc/passwd.srt", "https://evil.com/x.srt", "/subs/abc/x.srt"):
        pt2 = pt.__class__(**{**pt.to_dict(), "file_id": bad})
        with pytest.raises(ProviderError):
            p.download(pt2)
    html_page = make(lambda r: httpx.Response(200, text="User-agent: *\nAllow: /\n") if r.url.path == "/robots.txt" else httpx.Response(200, content=b"<!DOCTYPE html><html>bloqueado</html>"))
    with pytest.raises(ProviderError, match="página"):
        html_page.download(pt)


def test_rate_limit_and_min_interval_are_applied(monkeypatch):
    sleeps = []
    monkeypatch.setattr(Subtitlecat, "_sleep", staticmethod(sleeps.append))
    Subtitlecat._last = 0.0
    seen = []
    p = make(site(seen))

    class Counter:
        n = 0

        def acquire(self, *a, **k):
            Counter.n += 1
    p.ctx.limiter = Counter()
    p.search(q())
    pages = [x for x in seen if x[0] != "/robots.txt"]
    assert Counter.n == len(pages) >= 3                                                  # cada pedido ao site passa pelo limite por minuto das Definições
    assert len([s for s in sleeps if 3 < s <= 4.0]) >= 2                                 # e é espaçado (~4 s entre pedidos)


def test_year_from_filename_and_plus_minus_one_year_and_no_series_rows():
    page = """<table>
<tr><td><a href="subs/1686/Knightfall.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-English.html">Knightfall.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-English</a> (translated from English)</td><td class="sub-table__metric"><span class="sub-table__metric-label"><img src="a" alt=""> Languages</span><span class="sub-table__metric-value">20</span></td></tr>
<tr><td><a href="subs/1700/Knightfall.S01.html">Knightfall.S01</a> (translated from English)</td></tr>
<tr><td><a href="subs/1701/Knightfall.2010.DVDRip.html">Knightfall.2010.DVDRip</a> (translated from English)</td></tr></table>"""
    seen = []

    def h(req):
        seen.append((req.url.path, dict(req.url.params)))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/index.php":
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if req.url.path.startswith("/subs/1686/"):
            return httpx.Response(200, text='<a id="download_pt-BR" href="/subs/1686/Knightfall.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-English-pt-BR.srt">Download</a>', headers={"content-type": "text/html"})
        return httpx.Response(404)
    # Radarr diz 2025, o ficheiro diz 2026 (o site também): tem de aparecer
    res = make(h).search(SearchQuery(kind="movie", title="Knightfall", year=2025, languages=["pt-PT", "pt-BR"], file_name="Knightfall.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-CHORTLE.mkv", max_results=20))
    assert [r.language for r in res] == ["pt-BR"] and res[0].file_id.startswith("/subs/1686/")
    assert ("/index.php", {"search": "Knightfall 2026"}) in seen                                   # pesquisa com o ano do ficheiro
    paths = [p for p, _ in seen]
    assert not any("1700" in p or "1701" in p for p in paths)                                      # série (S01) e outro ano (2010): nem se abrem
