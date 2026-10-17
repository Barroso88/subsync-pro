import os
import shutil
from pathlib import Path

import pytest

from app import coverage as cov
from app.scanner import ScanAborted, associate
from tests.conftest import make_srt, wait_job


def add_libs(core, root):
    a = core.add_library("Filmes", "movies", str(root / "Filmes"), True)
    b = core.add_library("Séries", "series", str(root / "Séries"), False)
    return a, b


def test_scan_finds_media_and_subs(core, media_root):
    add_libs(core, media_root)
    jid = core.start_scan(None)
    j = wait_job(core, jid)
    assert j["status"] == "done", j
    assert core.db.scalar("SELECT COUNT(*) FROM media_items WHERE kind='movie'") == 1
    assert core.db.scalar("SELECT COUNT(*) FROM media_items WHERE kind='episode'") == 2
    ext = core.db.query("SELECT * FROM subtitles WHERE kind='external'")
    assert {(s["lang"], s["lang_source"]) for s in ext} == {("en", "filename"), ("pt-PT", "filename")}
    emb = core.db.query("SELECT * FROM subtitles WHERE kind='embedded'")
    assert len(emb) == 1 and emb[0]["lang"] == "en"
    movie = core.db.one("SELECT * FROM media_items WHERE kind='movie'")
    assert movie["duration"] == 3700.0 and movie["title"] == "Filme Teste" and movie["year"] == 2024


def test_incremental_scan_skips_unchanged(core, media_root, fake_probe):
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    n = len(fake_probe)
    wait_job(core, core.start_scan(None))
    assert len(fake_probe) == n                      # nada reanalisado
    # alterar um ficheiro -> só esse volta ao ffprobe
    v = media_root / "Filmes" / "Filme Teste (2024)" / "Filme Teste (2024).mkv"
    v.write_bytes(b"\3" * 300_000)
    os.utime(v, (v.stat().st_atime, v.stat().st_mtime + 10))
    wait_job(core, core.start_scan(None))
    assert len(fake_probe) == n + 1


def test_removed_files_marked_not_deleted(core, media_root):
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    (media_root / "Séries" / "Minha Série" / "Season 01" / "Minha Série - S01E02.mkv").unlink()
    wait_job(core, core.start_scan(None))
    assert core.db.scalar("SELECT COUNT(*) FROM media_items WHERE present=0") == 1
    assert core.db.scalar("SELECT COUNT(*) FROM media_items") == 3


def test_unmounted_share_does_not_wipe_inventory(core, media_root, tmp_path):
    # >5 itens para ativar a salvaguarda
    d = media_root / "Filmes"
    for i in range(8):
        (d / f"Outro {i} (2020)").mkdir()
        (d / f"Outro {i} (2020)" / f"Outro {i} (2020).mkv").write_bytes(b"x" * 1000)
    a, _ = add_libs(core, media_root)
    wait_job(core, core.start_scan(a))
    for p in list(d.iterdir()):
        shutil.rmtree(p)           # simula share vazia mas montada
    j = wait_job(core, core.start_scan(a))
    assert j["status"] == "failed"
    assert core.db.scalar("SELECT COUNT(*) FROM media_items WHERE present=1") >= 9


def test_scan_never_modifies_media(core, media_root):
    before = {p: (p.stat().st_size, p.stat().st_mtime) for p in media_root.rglob("*") if p.is_file()}
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    after = {p: (p.stat().st_size, p.stat().st_mtime) for p in media_root.rglob("*") if p.is_file()}
    assert before == after


def test_coverage_and_embedded_setting(core, media_root):
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}, {"code": "en", "priority": "primary"}])
    c = cov.compute(core.db, core.settings)
    by = {core.db.one("SELECT file_name FROM media_items WHERE id=?", (k,))["file_name"]: v for k, v in c.items()}
    e1, e2, mv = by["Minha Série - S01E01.mkv"], by["Minha Série - S01E02.mkv"], by["Filme Teste (2024).mkv"]
    assert e1["covered"] == {"pt-PT": "external"} and e1["missing"] == ["en"]
    assert e2["covered"] == {"en": "embedded"} and e2["missing"] == ["pt-PT"]       # embutida conta
    assert mv["missing"] == ["pt-PT"]
    core.settings.set("embedded_counts", False)
    c = cov.compute(core.db, core.settings)
    e2id = core.db.one("SELECT id FROM media_items WHERE file_name LIKE '%S01E02%'")["id"]
    assert c[e2id]["missing"] == ["pt-PT", "en"]


def test_generic_pt_does_not_satisfy_pt_pt(core, media_root):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nOlá.\n", "utf-8")   # variante indeterminável
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}])
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    assert "pt-PT" in cov.compute(core.db, core.settings, [mid])[mid]["missing"]
    core.settings.set("languages", [{"code": "pt", "priority": "primary"}])
    assert cov.compute(core.db, core.settings, [mid])[mid]["missing"] == []


def test_problem_detection_on_existing(core, media_root):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).fr.srt").write_text("", "utf-8")                        # vazia
    (d / "Filme Teste (2024).de.srt").write_text("isto não é uma legenda", "utf-8")  # inválida
    (d / "Filme Teste (2024).es.srt").write_text(make_srt(300, 30_000, lang="en"), "utf-8")   # passa do fim + idioma errado
    (d / "Filme Teste (2024).en.copy.srt").write_text((d / "Filme Teste (2024).en.srt").read_text(), "utf-8")
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    def issues(lang_path):
        import json
        r = core.db.one("SELECT * FROM subtitles WHERE path LIKE ?", (f"%{lang_path}",))
        return r["status"], {i["code"] for i in json.loads(r["issues"])}
    assert issues(".fr.srt")[0] == "empty"
    assert issues(".de.srt")[0] == "invalid"
    st, codes = issues(".es.srt")
    assert "beyond_video" in codes and "language_mismatch" in codes
    assert "duplicate" in issues("(2024).en.srt")[1]


def test_association_rules():
    v = [Path("/a/Show.S01E01.mkv"), Path("/a/Show.S01E02.mkv")]
    assert associate(v, Path("/a/Show.S01E01.en.srt")) == (v[0], "certain")
    assert associate(v, Path("/a/Outro.nome.S01E02.srt"))[1] == "probable"
    assert associate(v, Path("/a/random.srt")) == (None, "uncertain")
    assert associate([v[0]], Path("/a/random.srt")) == (v[0], "probable")


def test_subs_folder_is_scanned(core, media_root):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Subs").mkdir()
    (d / "Subs" / "Filme Teste (2024).fr.srt").write_text(make_srt(60, lang="en"), "utf-8")
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    assert core.db.scalar("SELECT COUNT(*) FROM subtitles WHERE lang='fr' AND media_id IS NOT NULL") == 1


def test_persistence_after_restart(cfg, fake_probe, media_root):
    from app.services import App
    a = App(cfg)
    a.add_library("Filmes", "movies", str(media_root / "Filmes"), True)
    wait_job(a, a.start_scan(None))
    a.settings.set("auto_download", True)
    a.stop()
    b = App(cfg)
    try:
        assert b.db.scalar("SELECT COUNT(*) FROM media_items") == 1
        assert b.settings.get("auto_download") is True
        assert len(b.libraries()) == 1
    finally:
        b.stop()


def test_generic_pt_name_refined_by_content_and_policy(core, media_root):
    from tests.conftest import PT_BR_TEXT
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt.srt").write_text(make_srt(60, pool=PT_BR_TEXT), "utf-8")
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    r = core.db.one("SELECT lang,lang_source,issues FROM subtitles WHERE path LIKE '%.pt.srt'")
    assert (r["lang"], r["lang_source"]) == ("pt-BR", "content") and "variant_estimated" in r["issues"]
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}, {"code": "pt-BR", "priority": "primary"}])
    assert cov.compute(core.db, core.settings, [mid])[mid]["missing"] == ["pt-PT"]          # pt-BR já coberto pela variante estimada


def test_generic_pt_undetermined_policy_and_revalidate(core, media_root, fake_probe):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt.srt").write_text(make_srt(60, lang="en"), "utf-8")   # sem marcadores: continua «pt»
    sub = d / "Filme Teste (2024).pt.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nOlá.\n", "utf-8")             # curto: variante indeterminada
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}])
    c = cov.compute(core.db, core.settings, [mid])[mid]
    assert c["missing"] == ["pt-PT"] and "pt" in c["covered"]
    core.settings.set("generic_pt_variant", "pt-PT")
    c = cov.compute(core.db, core.settings, [mid])[mid]
    assert c["missing"] == [] and c["fallback"] == {"pt-PT": "pt"}
    n_probe = len(fake_probe)
    assert wait_job(core, core.jobs.submit("revalidate", "r", {}))["status"] == "done"
    assert len(fake_probe) == n_probe                                                      # relê legendas sem reexecutar ffprobe


def test_embedded_generic_pt_refined_by_extracted_text(core, media_root, monkeypatch):
    from app import ffprobe as fp
    from tests.conftest import PT_BR_TEXT, PT_PT
    probes = []
    monkeypatch.setattr(fp, "probe", lambda path, b="ffprobe", timeout=60: fp.ProbeResult(
        True, 3600.0, "h264", 1920, 1080, 24.0,
        [fp.EmbeddedSub(3, "subrip", "pt", "por", None, False, False, True),
         fp.EmbeddedSub(4, "subrip", "pt", "por", None, False, False, True)]))
    texts = {3: make_srt(60, pool=PT_PT), 4: make_srt(60, pool=PT_BR_TEXT)}
    calls = []
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda path, idx, b="ffmpeg", timeout=300: (calls.append(idx), texts[idx].encode())[1])
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    langs = {r["stream_index"]: (r["lang"], r["lang_source"]) for r in core.db.query("SELECT * FROM subtitles WHERE media_id=? AND kind='embedded'", (mid,))}
    assert langs[3] == ("pt-PT", "content") and langs[4] == ("pt-BR", "content")
    assert sorted(set(calls)) == [3, 4]
    n = len(calls)
    wait_job(core, core.jobs.submit("revalidate", "r", {}))
    assert len(calls) == n                              # já resolvidas: não relê o ficheiro
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}, {"code": "pt-BR", "priority": "primary"}])
    assert cov.compute(core.db, core.settings, [mid])[mid]["missing"] == []


def test_probe_title_disambiguates_pt_variant():
    import json
    from app import ffprobe as fp
    def stream(i, title):
        return {"index": i, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "por", **({"title": title} if title else {})}, "disposition": {}}
    r = fp.parse_probe_json(json.dumps({"format": {"duration": "10"}, "streams": [
        stream(1, "Português (Brasil)"), stream(2, "Portuguese (Portugal)"), stream(3, "European Portuguese"), stream(4, "Portuguese"), stream(5, None)]}))
    assert [s.lang for s in r.subs] == ["pt-BR", "pt-PT", "pt-PT", "pt", "pt"]


def test_generic_pt_variant_policy_only_covers_chosen_and_skips_extraction(core, media_root, monkeypatch):
    from app import ffprobe as fp
    calls = []
    monkeypatch.setattr(fp, "probe", lambda path, b="ffprobe", timeout=60: fp.ProbeResult(
        True, 3600.0, "h264", 1920, 1080, 24.0, [fp.EmbeddedSub(3, "subrip", "pt", "por", None, False, False, True)]))
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda *a, **k: calls.append(a) or None)
    core.settings.set("generic_pt_variant", "pt-PT")
    add_libs(core, media_root)
    wait_job(core, core.start_scan(None))
    assert calls == []                                       # nada lido com o ffmpeg
    mid = core.db.one("SELECT id FROM media_items WHERE kind='movie'")["id"]
    core.settings.set("languages", [{"code": "pt-PT", "priority": "primary"}, {"code": "pt-BR", "priority": "primary"}])
    c = cov.compute(core.db, core.settings, [mid])[mid]
    assert c["missing"] == ["pt-BR"] and c["fallback"] == {"pt-PT": "pt"}      # só cobre a variante escolhida
