import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_srt, wait_job
from tests.test_scanner_coverage import add_libs


@pytest.fixture
def setup(cfg, fake_probe, media_root):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt-br.srt").write_bytes(b"")
    (d / "Filme Teste (2024).fr.srt").write_bytes(b"")
    s = media_root / "Séries" / "Minha Série" / "Season 01"
    (s / "Minha Série - S01E01.es.srt").write_bytes(b"")           # biblioteca Séries: SÓ LEITURA nos testes
    app = create_app(cfg)
    with TestClient(app) as c:
        c.core, c.root, c.dir, c.sdir = app.state.core, media_root, d, s
        c.post("/api/libraries", json={"name": "Filmes", "type": "movies", "path": str(media_root / "Filmes"), "writable": True})
        c.post("/api/libraries", json={"name": "Séries", "type": "series", "path": str(media_root / "Séries"), "writable": False})
        j = c.post("/api/scan", json={}).json()["job_id"]
        wait_job(c.core, j)
        yield c


def ids(c, **p):
    return c.get("/api/subtitles/empty", params=p).json()


def test_lists_empty_with_deletable_flag(setup):
    d = ids(setup)
    assert d["total"] == 3 and d["deletable"] == 2
    byname = {i["name"]: i for i in d["items"]}
    assert byname["Filme Teste (2024).pt-br.srt"]["deletable"] is True
    assert byname["Minha Série - S01E01.es.srt"]["deletable"] is False         # biblioteca só de leitura
    assert ids(setup, lang="pt-BR")["total"] == 1 and ids(setup, q="série")["total"] == 1
    assert setup.get("/api/sidebar").json()["empty_subtitles"] == 3


def test_delete_only_deletable_and_updates_state(setup):
    all_ids = [i["id"] for i in ids(setup)["items"]]
    r = setup.post("/api/subtitles/empty/delete", json={"ids": all_ids}).json()
    assert r["deleted"] == 2 and len(r["skipped"]) == 1 and "escrita" in r["skipped"][0]["reason"]
    assert not (setup.dir / "Filme Teste (2024).pt-br.srt").exists() and not (setup.dir / "Filme Teste (2024).fr.srt").exists()
    assert (setup.sdir / "Minha Série - S01E01.es.srt").exists()               # só leitura: intacto
    assert (setup.dir / "Filme Teste (2024).en.srt").exists()                  # legendas boas intactas
    assert (setup.dir / "Filme Teste (2024).mkv").exists()
    assert ids(setup)["total"] == 1
    assert any("apagada" in e["message"] for e in setup.get("/api/events?limit=10").json())


def test_file_that_is_no_longer_empty_is_never_deleted(setup):
    item = next(i for i in ids(setup)["items"] if i["name"].endswith("pt-br.srt"))
    (setup.dir / "Filme Teste (2024).pt-br.srt").write_text(make_srt(30), "utf-8")   # entretanto ganhou conteúdo
    r = setup.post("/api/subtitles/empty/delete", json={"ids": [item["id"]]}).json()
    assert r["deleted"] == 0 and "já não está vazio" in r["skipped"][0]["reason"]
    assert (setup.dir / "Filme Teste (2024).pt-br.srt").read_text().startswith("1\n")


def test_symlink_and_non_subtitle_and_unknown_ids_are_skipped(setup):
    item = next(i for i in ids(setup)["items"] if i["name"].endswith(".fr.srt"))
    p = setup.dir / "Filme Teste (2024).fr.srt"
    p.unlink()
    outside = setup.root / "fora.txt"
    outside.write_bytes(b"")
    p.symlink_to(outside)                                                       # atalho para fora
    r = setup.post("/api/subtitles/empty/delete", json={"ids": [item["id"], 999999]}).json()
    assert r["deleted"] == 0 and outside.exists() and os.path.islink(p)
    # um id de VÍDEO nunca pode ser apagado por esta rota (só linhas de legendas externas)
    vid = setup.core.db.one("SELECT id FROM subtitles WHERE kind='embedded' LIMIT 1")
    if vid:
        assert setup.post("/api/subtitles/empty/delete", json={"ids": [vid["id"]]}).json()["deleted"] == 0
    assert (setup.dir / "Filme Teste (2024).mkv").exists()


def test_db_row_pointing_to_a_video_is_refused(setup):
    sid = setup.core.db.execute("INSERT INTO subtitles(library_id,kind,path,status,present) VALUES(1,'external',?, 'empty',1)",
                                (str(setup.dir / "Filme Teste (2024).mkv"),)).lastrowid
    (setup.dir / "vazio.mkv").write_bytes(b"")
    r = setup.post("/api/subtitles/empty/delete", json={"ids": [sid]}).json()
    assert r["deleted"] == 0 and "extensão" in r["skipped"][0]["reason"]
    assert (setup.dir / "Filme Teste (2024).mkv").stat().st_size > 0
