from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)


def test_sort_by_added_date_with_mtime_fallback(setup):
    ids = [r["id"] for r in setup.core.db.query("SELECT id FROM media_items WHERE kind='movie' AND present=1")]
    assert ids
    m = ids[0]
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")     # recente (dentro da tolerância de 1 dia)
    setup.core.db.execute("UPDATE media_items SET added_at=? WHERE id=?", (when, m))
    r = setup.get("/api/media", params={"kind": "movie", "sort": "added"}).json()
    assert r["items"][0]["id"] == m and r["items"][0]["added_at"] == when
    r = setup.get("/api/media", params={"kind": "movie", "sort": "added_old"}).json()
    assert all(i["added_at"] for i in r["items"])                                    # sem data vinda de integrações: usa a do ficheiro
    ep = setup.get("/api/media", params={"kind": "episode", "sort": "added"}).json()
    assert ep["total"] >= 1 and ep["items"][0]["added_at"]


def test_future_dates_are_ignored_in_sort_and_view(setup):
    ids = [r["id"] for r in setup.core.db.query("SELECT id FROM media_items WHERE kind='movie' AND present=1")]
    m = ids[0]
    setup.core.db.execute("UPDATE media_items SET added_at='2098-01-01T02:00:00Z', mtime=4070908800 WHERE id=?", (m,))     # tudo no futuro
    r = setup.get("/api/media", params={"kind": "movie", "sort": "added"}).json()
    assert r["items"][0]["id"] != m or r["total"] == 1                       # não fica no topo
    it = next(i for i in r["items"] if i["id"] == m)
    assert it["added_at"] is None
    r = setup.get("/api/media", params={"kind": "movie", "sort": "added_old"}).json()
    assert r["items"][-1]["id"] == m or r["total"] == 1                      # sem data fiável: vai para o fim nas duas ordens
