import shutil

from tests.test_subtitle_tools import setup, wait_job  # noqa: F401  (fixture reutilizada)


def _scan(c):
    wait_job(c.core, c.post("/api/scan", json={}).json()["job_id"])


def test_duplicates_listed_resolved_with_backup_and_revalidated(setup):
    d = setup.dir
    dup = d / "Filme Teste (2024).pt.srt"
    shutil.copy(setup.ptfile, dup)
    _scan(setup)
    r = setup.get("/api/subtitles/duplicates").json()
    assert r["total"] == 1 and r["removable"] == 1 and setup.get("/api/sidebar").json()["duplicate_subtitles"] == 1
    g = r["groups"][0]
    keep = next(f for f in g["files"] if f["keep"])
    rem = next(f for f in g["files"] if not f["keep"])
    assert keep["name"] == "Filme Teste (2024).pt-PT.srt" and rem["name"] == "Filme Teste (2024).pt.srt"      # fica a variante explícita
    # se o conteúdo mudar entretanto, não remove
    dup.write_text(dup.read_text("utf-8") + "\n", "utf-8")
    out = setup.post("/api/subtitles/duplicates/resolve", json={"items": [{"keep": keep["id"], "remove": [rem["id"]]}]}).json()
    assert out["moved"] == 0 and "já não é igual" in out["skipped"][0]["reason"] and dup.exists()
    shutil.copy(setup.ptfile, dup)
    out = setup.post("/api/subtitles/duplicates/resolve", json={"items": [{"keep": keep["id"], "remove": [rem["id"]]}]}).json()
    assert out["moved"] == 1 and not dup.exists() and setup.ptfile.exists()
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 1
    # nunca remove o que fica, nem pares de filmes diferentes
    other = setup.core.db.one("SELECT id FROM subtitles WHERE kind='external' AND path LIKE '%S01E01.pt-PT.srt'")["id"]
    out = setup.post("/api/subtitles/duplicates/resolve", json={"items": [{"keep": keep["id"], "remove": [keep["id"], other]}]}).json()
    assert out["moved"] == 0 and len(out["skipped"]) == 2 and setup.ptfile.exists()
    _scan(setup)
    assert setup.get("/api/subtitles/duplicates").json()["total"] == 0


def test_same_content_with_different_variant_names_keeps_the_one_matching_content(setup):
    from tests.conftest import PT_BR_TEXT, make_srt
    d = setup.dir
    br = make_srt(120, 30_000, lang="pt-PT", pool=PT_BR_TEXT)
    (d / "Filme Teste (2024).pt-BR.srt").write_text(br, "utf-8")
    (d / "Filme Teste (2024).pt-PT.srt").write_text(br, "utf-8")           # cópia mal etiquetada: conteúdo pt-BR com nome pt-PT
    _scan(setup)
    g = setup.get("/api/subtitles/duplicates").json()
    grp = g["groups"][0]
    assert grp["lang_conflict"] and g["conflicts"] == 1 and g["removable"] == 0 and grp["content_lang"] == "pt-BR"
    assert next(f for f in grp["files"] if f["keep"])["name"].endswith("pt-BR.srt")
    assert setup.get("/api/subtitles/duplicates/all").json()["items"] == []                # não entra em «resolver todos»


def test_duplicate_that_reappears_after_being_moved_is_flagged(setup):
    d = setup.dir
    dup = d / "Filme Teste (2024).pt.srt"
    shutil.copy(setup.ptfile, dup)
    _scan(setup)
    g = setup.get("/api/subtitles/duplicates").json()["groups"][0]
    assert g["reappeared"] == 0
    keep = next(f for f in g["files"] if f["keep"])["id"]
    rem = next(f for f in g["files"] if not f["keep"])["id"]
    assert setup.post("/api/subtitles/duplicates/resolve", json={"items": [{"keep": keep, "remove": [rem]}]}).json()["moved"] == 1
    shutil.copy(setup.ptfile, dup)                                              # «outra ferramenta» recria o mesmo ficheiro no mesmo sítio
    _scan(setup)
    g = setup.get("/api/subtitles/duplicates").json()["groups"][0]
    again = next(f for f in g["files"] if f["name"] == "Filme Teste (2024).pt.srt")
    assert g["reappeared"] == 1 and again["reappeared"] == 1                      # a app sabe que já o tinha movido e que voltou
    assert again["keep"] is True and not any(f["keep"] for f in g["files"] if f is not again)    # e passa a propor ficar com ele (senão voltaria sempre)
