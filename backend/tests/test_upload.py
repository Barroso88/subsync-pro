from app import ffprobe as fp
from tests.conftest import PT_BR_TEXT, make_srt
from tests.test_subtitle_tools import setup, wait_job  # noqa: F401  (fixture reutilizada)


def _media(setup):
    return setup.core.db.one("SELECT id FROM media_items WHERE kind='movie' AND present=1")["id"]


def test_upload_prepares_validates_and_installs_without_overwriting(setup):
    mid = _media(setup)
    srt = make_srt(120, 30_000, lang="pt-PT", offset_ms=0)
    r = setup.post(f"/api/media/{mid}/upload", params={"filename": "minha legenda.srt", "lang": "pt-PT"}, content=srt.encode("utf-8"))
    assert r.status_code == 200, r.text
    d = r.json()
    ev = d["evaluation"]
    assert d["status"] == "prepared" and d["provider"] == "upload" and ev["compat"] == "user" and ev["compat_label"] == "Enviada por si"
    assert d["lang"] == "pt-PT" and d["dest_path"].endswith("Filme Teste (2024).pt-PT.srt") and d["conflict"]       # já existe uma pt-PT: conflito, não é substituída
    assert ev["alignment"] and ev["alignment"]["reference"]                                                         # comparada com a legenda de referência (en)
    # a biblioteca não foi tocada até instalar
    assert not any(p.name == "minha legenda.srt" for p in setup.dir.iterdir())
    inst = setup.post(f"/api/downloads/{d['id']}/install", json={"policy": "fail"})
    assert inst.status_code == 409                                                                                   # nunca substitui em silêncio


def test_upload_detects_language_flags_mismatch_and_rejects_junk(setup):
    mid = _media(setup)
    en = make_srt(120, 30_000, lang="en")
    d = setup.post(f"/api/media/{mid}/upload", params={"filename": "x.srt"}, content=en.encode()).json()
    assert d["lang"] == "en" and any("estimado pelo conteúdo" in n for n in d["evaluation"]["notes"])                # sem idioma indicado: estima
    d = setup.post(f"/api/media/{mid}/upload", params={"filename": "y.srt", "lang": "pt-PT"}, content=en.encode()).json()
    assert any("não parece corresponder" in f["label"] for f in d["evaluation"]["factors"])                         # idioma indicado ≠ conteúdo
    br = make_srt(120, 30_000, lang="pt-PT", pool=PT_BR_TEXT)
    d = setup.post(f"/api/media/{mid}/upload", params={"filename": "z.srt", "lang": "pt-PT"}, content=br.encode()).json()
    assert any("parece «pt-BR»" in f["label"] for f in d["evaluation"]["factors"])
    assert setup.post(f"/api/media/{mid}/upload", params={"filename": "vazio.srt"}, content=b"").status_code == 422
    assert setup.post(f"/api/media/{mid}/upload", params={"filename": "lixo.srt"}, content=b"<html>erro</html>").status_code == 422
    assert setup.post(f"/api/media/{mid}/upload", params={"filename": "../../etc/passwd.srt"}, content=en.encode()).json()["file_name"] == "passwd.srt"   # nome sanitizado
    assert setup.post(f"/api/media/{mid}/upload", params={"filename": "grande.srt"}, content=b"0" * (9 * 1024 * 1024)).status_code == 413
    assert setup.post("/api/media/999999/upload", params={"filename": "a.srt"}, content=en.encode()).status_code == 404


def test_upload_new_language_installs_with_backup_free_path(setup):
    mid = _media(setup)
    es = make_srt(120, 30_000, lang="en")                                                                            # conteúdo qualquer válido
    d = setup.post(f"/api/media/{mid}/upload", params={"filename": "pt.srt", "lang": "pt-BR"}, content=es.encode()).json()
    assert not d["conflict"] and d["dest_path"].endswith("Filme Teste (2024).pt-BR.srt")
    inst = setup.post(f"/api/downloads/{d['id']}/install", json={"policy": "fail"})
    assert inst.status_code == 200 and (setup.dir / "Filme Teste (2024).pt-BR.srt").exists()
