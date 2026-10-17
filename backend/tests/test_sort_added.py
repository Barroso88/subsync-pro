from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)


def test_sort_by_added_date_with_mtime_fallback(setup):
    ids = [r["id"] for r in setup.core.db.query("SELECT id FROM media_items WHERE kind='movie' AND present=1")]
    assert ids
    m = ids[0]
    setup.core.db.execute("UPDATE media_items SET added_at='2030-01-01T00:00:00Z' WHERE id=?", (m,))
    r = setup.get("/api/media", params={"kind": "movie", "sort": "added"}).json()
    assert r["items"][0]["id"] == m and r["items"][0]["added_at"] == "2030-01-01T00:00:00Z"
    r = setup.get("/api/media", params={"kind": "movie", "sort": "added_old"}).json()
    assert all(i["added_at"] for i in r["items"])                                    # sem data vinda de integrações: usa a do ficheiro
    ep = setup.get("/api/media", params={"kind": "episode", "sort": "added"}).json()
    assert ep["total"] >= 1 and ep["items"][0]["added_at"]
