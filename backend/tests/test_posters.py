import httpx
import pytest

from app.integrations.arr import Arr
from app.integrations.common import fetch_public_image, IntegrationError, public_image_host_ok
from tests.conftest import wait_job
from tests.test_scanner_coverage import add_libs


def test_poster_ref_keeps_local_and_public_fallback():
    a = Arr("radarr", "http://r", "K", [], validate=False)
    ref = a._poster([{"coverType": "poster", "url": "/MediaCover/1/poster.jpg?lastWrite=1", "remoteUrl": "https://image.tmdb.org/t/p/original/x.jpg"}])
    assert ref == "arr:radarr:/MediaCover/1/poster.jpg?lastWrite=1|https://image.tmdb.org/t/p/w342/x.jpg"
    assert a._poster([{"coverType": "poster", "remoteUrl": "https://evil.example/x.jpg"}]) is None


def test_public_image_allowlist_and_validation():
    assert public_image_host_ok("https://image.tmdb.org/x.jpg") and not public_image_host_ok("http://image.tmdb.org/x.jpg")
    assert not public_image_host_ok("https://169.254.169.254/x")
    ok = httpx.MockTransport(lambda r: httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"}))
    assert fetch_public_image("https://image.tmdb.org/x.jpg", ok) == (b"img", "image/jpeg")
    html = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    with pytest.raises(IntegrationError):
        fetch_public_image("https://image.tmdb.org/x.jpg", html)


def test_arr_fetch_tries_header_then_query():
    def h(req: httpx.Request):
        if req.headers.get("x-api-key"):
            return httpx.Response(302, headers={"location": "/login"})
        if req.url.params.get("apikey") == "K":
            return httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
        return httpx.Response(401)
    a = Arr("radarr", "http://r", "K", [], httpx.MockTransport(h), validate=False)
    assert a.fetch_image("/MediaCover/1/poster.jpg") == (b"png", "image/png")


def test_poster_falls_back_to_public_url(core, media_root):
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    core.db.execute("UPDATE media_items SET poster=? WHERE id=?", ("arr:radarr:/MediaCover/1/p.jpg|https://image.tmdb.org/t/p/w342/x.jpg", mid))
    core.settings.set("radarr", {"enabled": False, "url": "", "path_mappings": []})
    core.transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"}))
    assert core.fetch_poster(mid) == (b"IMG", "image/jpeg")
    # sem hipótese: devolve motivo legível
    mid2 = mid
    (core.cfg.poster_dir / f"{mid2}.img").unlink()
    core.db.execute("UPDATE media_items SET poster='arr:radarr:/MediaCover/1/p.jpg' WHERE id=?", (mid,))
    assert core.fetch_poster(mid) is None and "desativado" in core.poster_error
