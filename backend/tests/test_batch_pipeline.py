import json

import numpy as np

from app import audio_sync as A
from app.providers.base import DownloadedFile
from app.subtitles.parser import parse_srt
from tests.test_audio_sync import audio_for, make_cues, srt_from, wait_job
from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)


def prime(setup, monkeypatch, offset_s):
    row = setup.core.db.one("SELECT media_id, library_id FROM subtitles WHERE id=?", (setup.pt,))
    truth = make_cues(300, 3600, seed=5)
    setup.ptfile.write_text(srt_from(truth, offset_s=offset_s), "utf-8")
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=?", (row["library_id"],))), None, str(setup.dir), force_subs=True)
    monkeypatch.setattr(A, "extract_window", lambda path, t0, d, ff="ffmpeg", timeout=0: audio_for(truth, t0, d, offset=0.0))
    setup.core.db.execute("UPDATE media_items SET duration=3600 WHERE id=?", (row["media_id"],))
    return row["media_id"], truth


def test_batch_verifies_in_background_waits_for_approval_then_applies(setup, monkeypatch):
    mid, truth = prime(setup, monkeypatch, 7.0)
    r = setup.post("/api/audio-checks/start", json={"media_ids": [mid]})
    assert r.status_code == 202 and r.json()["count"] == 1                          # só a pt-PT: a EN não é um dos idiomas que interessam
    j = wait_job(setup, r.json()["job_id"])
    assert j["status"] == "done" and j["result"]["pending"] == 1, j
    q = setup.get("/api/audio-checks", params={"status": "pending"}).json()
    it = q["items"][0]
    assert it["subtitle_id"] == setup.pt and it["mode"] == "offset" and abs(it["offset_s"] + 7.0) < 0.3 and not it["stale"] and q["counts"]["pending"] == 1
    before = setup.ptfile.read_bytes()
    assert before == setup.ptfile.read_bytes()                                       # a análise nunca altera a legenda sozinha
    # já analisada: não volta a entrar no lote
    assert setup.post("/api/audio-checks/start", json={"media_ids": [mid]}).status_code == 422
    ap = setup.post(f"/api/audio-checks/{setup.pt}/apply")
    assert ap.status_code == 200, ap.text
    cues = parse_srt(setup.ptfile.read_text("utf-8")).cues
    assert abs(cues[0].start_ms - truth[0][0]) < 300 and setup.ptfile.read_bytes() != before
    assert setup.core.db.one("SELECT status FROM audio_checks WHERE subtitle_id=?", (setup.pt,))["status"] == "applied"
    assert "áudio" in setup.core.db.one("SELECT sync_detail FROM subtitles WHERE id=?", (setup.pt,))["sync_detail"]
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 1                       # backup do original
    assert setup.post(f"/api/audio-checks/{setup.pt}/apply").status_code == 404      # já aplicada


def test_batch_dismiss_and_stale_apply_refused(setup, monkeypatch):
    mid, _ = prime(setup, monkeypatch, 7.0)
    wait_job(setup, setup.post("/api/audio-checks/start", json={"media_ids": [mid]}).json()["job_id"])
    setup.ptfile.write_text(setup.ptfile.read_text("utf-8") + "\n", "utf-8")         # a legenda mudou depois da análise…
    assert setup.post(f"/api/audio-checks/{setup.pt}/apply").status_code == 409      # …a correção calculada já não vale
    assert setup.post(f"/api/audio-checks/{setup.pt}/dismiss").status_code == 200
    assert setup.core.db.one("SELECT status FROM audio_checks WHERE subtitle_id=?", (setup.pt,))["status"] == "dismissed"
    assert setup.post("/api/audio-checks/999/dismiss").status_code == 404


def test_batch_aligned_is_confirmed_without_asking_and_inconclusive_is_honest(setup, monkeypatch):
    mid, truth = prime(setup, monkeypatch, 0.0)
    j = wait_job(setup, setup.post("/api/audio-checks/start", json={"media_ids": [mid]}).json()["job_id"])
    assert j["result"]["aligned"] == 1
    assert setup.core.db.one("SELECT sync_status FROM subtitles WHERE id=?", (setup.pt,))["sync_status"] == "aligned"
    monkeypatch.setattr(A, "extract_window", lambda *a, **k: np.random.default_rng(3).normal(0, 80, A.SR * 240).astype(np.float32))   # só ruído
    setup.core.db.execute("UPDATE subtitles SET sync_status='plausible' WHERE id=?", (setup.pt,))
    j = wait_job(setup, setup.post("/api/audio-checks/start", json={"media_ids": [mid], "redo": True}).json()["job_id"])
    assert j["result"]["inconclusive"] == 1 and j["result"]["pending"] == 0          # sem evidência: não inventa correção
    assert setup.get("/api/audio-checks").json()["counts"]["inconclusive"] == 1


def test_pipeline_runs_steps_on_new_subtitle_with_backups(setup, monkeypatch):
    mid, _ = prime(setup, monkeypatch, 0.0)
    monkeypatch.setattr(A, "extract_window", lambda *a, **k: None)                   # a verificação do áudio fica em fila e falha em silêncio
    text = "1\n00:00:01,000 --> 00:00:04,000\nLegendas por Fulano www.site.com\n\n2\n00:00:05,000 --> 00:00:08,000\nOlá  ,  você está bem ?\n\n3\n00:00:09,000 --> 00:00:12,000\nTudo certo.\n"
    setup.ptfile.write_bytes(text.encode("cp1252"))                                  # ficheiro antigo em cp1252
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=1")), None, str(setup.dir), force_subs=True)
    media = setup.core.media_dict(mid)
    # desligado por omissão: nada acontece
    assert setup.core._pipeline_after_install(media, str(setup.ptfile)) == []
    assert setup.ptfile.read_bytes() == text.encode("cp1252")
    setup.put("/api/settings", json={"pipeline": {"enabled": True, "convert": True, "clean": True, "audio": True}})
    done = setup.core._pipeline_after_install(media, str(setup.ptfile))
    assert any("convertida" in d for d in done) and any("texto limpo" in d for d in done) and any("áudio" in d for d in done), done
    new = setup.ptfile.read_text("utf-8")                                            # agora UTF-8
    assert "Olá, você está bem?" in new and "Fulano" not in new
    eds = setup.get("/api/subtitle-edits", params={"media_id": mid}).json()
    assert len(eds) == 1 and eds[0]["action"] == "convert" and eds[0]["backup_exists"]            # uma só linha e uma só cópia para a cadeia toda
    assert eds[0]["params"]["pipeline"] == ["convertida para SRT/UTF-8", "texto limpo"]
    assert setup.core.db.one("SELECT 1 FROM events WHERE message LIKE 'Pipeline automático%'")
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 1                       # só a cópia do original
    for e in eds:                                                                    # tudo restaurável, do último para o primeiro
        assert setup.post(f"/api/subtitle-edits/{e['id']}/restore", json={"force": True}).status_code == 200
    assert setup.ptfile.read_bytes() == text.encode("cp1252")
    for j in setup.core.db.query("SELECT id FROM jobs WHERE type='audio_sync'"):
        wait_job(setup, j["id"])


def test_pipeline_setting_validation(setup):
    assert setup.put("/api/settings", json={"pipeline": {"enabled": True, "convert": "sim"}}).status_code == 400
    assert setup.put("/api/settings", json={"pipeline": {"enabled": True, "x": True}}).status_code == 400
    assert setup.put("/api/settings", json={"pipeline": {"enabled": True, "durations": True}}).status_code == 200


class FakeProv:
    def __init__(self, files):
        self.files = files

    def download(self, r):
        return DownloadedFile(f"{r.file_id}.srt", self.files[r.file_id].encode("utf-8"))


def test_candidates_checked_against_audio_and_best_is_the_aligned_one(setup, monkeypatch):
    mid, truth = prime(setup, monkeypatch, 0.0)
    calls = {"n": 0}
    real = A.extract_window

    def counting(path, t0, d, ff="ffmpeg", timeout=0):
        calls["n"] += 1
        return real(path, t0, d, ff)
    monkeypatch.setattr(A, "extract_window", counting)
    setup.core._providers["opensubtitles"] = FakeProv({"good": srt_from(truth, 0.0), "late": srt_from(truth, 6.0)})
    res = [{"provider": "opensubtitles", "file_id": f, "language": "pt-PT", "release": "x", "file_name": f"{f}.srt"} for f in ("late", "good")]
    r = setup.post(f"/api/media/{mid}/candidates/audio-check", json={"results": res, "lang": "pt-PT"})
    assert r.status_code == 202, r.text
    j = wait_job(setup, r.json()["job_id"])
    assert j["status"] == "done", j
    out = {c["key"]: c for c in j["result"]["candidates"]}
    assert out["opensubtitles:good"]["audio"]["mode"] == "aligned" and out["opensubtitles:late"]["audio"]["mode"] == "offset"
    assert abs(out["opensubtitles:late"]["audio"]["offset_s"] + 6.0) < 0.3
    assert j["result"]["best"] == "opensubtitles:good"
    assert calls["n"] <= A.RIGOR_WINDOWS                                                          # o áudio do filme foi lido uma única vez para as duas candidatas
    d = setup.get(f"/api/downloads/{out['opensubtitles:good']['download_id']}").json()
    assert d["status"] == "prepared" and d["evaluation"]["audio"]["mode"] == "aligned"      # pronta a instalar sem voltar a descarregar
    assert setup.post(f"/api/media/{mid}/candidates/audio-check", json={"results": res * 2}).status_code == 422     # no máximo 3 (quota)


# ---- modo rigoroso -------------------------------------------------------------------------

def test_rigorous_flags_uneven_film_that_the_normal_mode_calls_fine():
    cues = make_cues(700, 3600, seed=9)
    jump = lambda p, t0, d, ff="ffmpeg", *a, **k: audio_for(cues, t0, d, offset=0.0 if t0 < 1800 else 2.0)      # a 2.ª metade do filme está 2 s desfasada
    normal = A.analyze("x", cues, 3600, extract=jump)
    rig = A.analyze("x", cues, 3600, extract=jump, rigorous=True)
    assert normal["mode"] != "uneven"
    assert rig["mode"] == "uneven" and rig["uneven_windows"] >= 2 and "zonas" in rig["reason"] and rig["windows_total"] == A.RIGOR_WINDOWS
    even = A.analyze("x", cues, 3600, extract=lambda p, t0, d, ff="ffmpeg", *a, **k: audio_for(cues, t0, d, offset=0.0), rigorous=True)
    assert even["mode"] == "aligned" and even["agree"] >= 12 and even["residual_p90_s"] <= 0.3 and all("dev" in w for w in even["windows"] if w["offset"] is not None)
    late = A.analyze("x", cues, 3600, extract=lambda p, t0, d, ff="ffmpeg", *a, **k: audio_for(cues, t0, d, offset=4.7), rigorous=True)
    assert late["mode"] == "offset" and abs(abs(late["offset_s"]) - 4.7) < 0.3


def test_audio_track_is_passed_to_ffmpeg_and_tracks_are_listed(monkeypatch):
    seen = {}

    class CP:
        returncode = 0
        stdout = np.zeros(A.SR * 10, dtype=np.int16).tobytes()

    def fake_run(cmd, **kw):
        seen.setdefault("cmds", []).append(cmd)
        return CP()
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    A._FILTER_CACHE.clear()
    A.extract_window("/m/x.mkv", 10, 60, "ffmpeg", track=2)
    ff = next(c for c in seen["cmds"] if c[0] == "ffmpeg")
    assert "0:a:2" in ff and any(c[0] == "ffprobe" and "a:2" in c for c in seen["cmds"])
    CP.stdout = json.dumps({"streams": [{"codec_name": "eac3", "channels": 6, "tags": {"language": "ger", "title": "Deutsch"}}, {"codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}}]}).encode()
    tr = A.list_audio_tracks("/m/x.mkv")
    assert [t["lang"] for t in tr] == ["ger", "eng"] and tr[1]["index"] == 1 and tr[0]["channels"] == 6
    A._FILTER_CACHE.clear()


# ---- pipeline com conversão para outro ficheiro + limpeza de cópias --------------------------

def test_pipeline_ass_keeps_single_backup_and_restore_removes_created_srt(setup, monkeypatch):
    mid, _ = prime(setup, monkeypatch, 0.0)
    monkeypatch.setattr(A, "extract_window", lambda *a, **k: None)
    ass = setup.dir / "Filme Teste (2024).es.ass"
    ass_text = ("[Script Info]\nTitle: x\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,Hola  ,  mundo !\n"
                "Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0,0,0,,Todo bien.\n")
    ass.write_text(ass_text, "utf-8")
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=1")), None, str(setup.dir), force_subs=True)
    setup.put("/api/settings", json={"pipeline": {"enabled": True, "convert": True, "clean": True, "audio": False}})
    done = setup.core._pipeline_after_install(setup.core.media_dict(mid), str(ass))
    srt = setup.dir / "Filme Teste (2024).es.srt"
    assert srt.exists() and not ass.exists() and "Hola, mundo!" in srt.read_text("utf-8") and len(done) == 2
    eds = [e for e in setup.get("/api/subtitle-edits", params={"media_id": mid}).json() if e["name"].startswith("Filme Teste (2024).es")]
    assert len(eds) == 1 and eds[0]["params"]["pipeline"] and len(list(setup.core.cfg.backup_dir.iterdir())) == 1
    assert setup.post(f"/api/subtitle-edits/{eds[0]['id']}/restore", json={}).status_code == 200      # sem «forçar»: o hash final foi atualizado
    assert ass.read_text("utf-8") == ass_text and not srt.exists()


def test_backup_cleanup_rules(setup):
    import os
    import time as _t
    core, bd = setup.core, setup.core.cfg.backup_dir
    bd.mkdir(parents=True, exist_ok=True)

    def mk(name, size, age_days, edit=None):
        p = bd / name
        p.write_bytes(b"x" * size)
        os.utime(p, (_t.time() - age_days * 86400,) * 2)
        if edit:
            core.db.execute("INSERT INTO subtitle_edits(subtitle_id,media_id,library_id,path,action,params,backup_path,created_at,restored_at) VALUES(1,1,1,?,?,?,?,?,?)",
                            ("/x/" + name, edit[0], "{}", str(p), "2026-01-01", edit[1]))
        return p
    recent, old, orphan_old = mk("recent.srt", 100, 1, ("textfix", None)), mk("old.srt", 200, 90, ("textfix", None)), mk("orphan.srt", 400, 90)
    restored = mk("restored.srt", 800, 1, ("shift", "2026-02-02"))                       # já restaurada: apaga-se mesmo sendo recente
    moved_old = mk("moved.srt", 1600, 90, ("delete", None))                              # a única cópia de uma legenda que moveu para backup
    sm = setup.get("/api/backups", params={"days": 30}).json()
    assert sm["total"] == {"files": 5, "bytes": 3100} and sm["restored"]["bytes"] == 800 and sm["old"]["files"] == 2 and sm["recent"]["files"] == 1
    assert sm["moved"]["files"] == 1 and sm["moved"]["eligible"]["files"] == 0                # por omissão NÃO apaga as legendas movidas
    assert sm["will_free"] == {"files": 3, "bytes": 1400}
    r = setup.post("/api/backups/cleanup", json={"days": 30}).json()
    assert r["deleted"] == 3 and r["freed_bytes"] == 1400 and recent.exists() and moved_old.exists() and not old.exists() and not restored.exists() and not orphan_old.exists()
    ed = [e for e in setup.get("/api/subtitle-edits", params={"media_id": 1}).json() if e["name"] == "old.srt"]
    assert ed and ed[0]["backup_exists"] is False                                        # o histórico mantém-se; só não se pode restaurar
    assert setup.post("/api/backups/cleanup", json={"days": 0}).json()["deleted"] == 1       # days=0 apaga também a recente
    assert moved_old.exists()
    r = setup.post("/api/backups/cleanup", json={"days": 30, "include_moved": True}).json()
    assert r["deleted"] == 1 and not moved_old.exists()


# ---- referência = faixa embutida --------------------------------------------------------------

def test_reference_can_be_an_embedded_track_extracted_on_demand(setup, monkeypatch):
    from app import ffprobe as fp
    row = setup.core.db.one("SELECT media_id, library_id, path FROM subtitles WHERE id=?", (setup.pt,))
    mid, lib = row["media_id"], row["library_id"]
    vpath = setup.core.db.one("SELECT path FROM media_items WHERE id=?", (mid,))["path"]
    truth = make_cues(150, 3600, seed=2)
    setup.ptfile.write_text(srt_from(truth, offset_s=7.0), "utf-8")                                  # a legenda está 7 s atrasada
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=?", (lib,))), None, str(setup.dir), force_subs=True)
    setup.core.db.execute("INSERT INTO subtitles(media_id,library_id,kind,path,stream_index,format,lang,forced,hi,text_based,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (mid, lib, "embedded", vpath, 3, "subrip", "de", 0, 0, 1, "ok"))
    emb = setup.core.db.one("SELECT id FROM subtitles WHERE kind='embedded' AND stream_index=3")["id"]
    setup.core.db.execute("INSERT INTO subtitles(media_id,library_id,kind,path,stream_index,format,lang,forced,hi,text_based,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (mid, lib, "embedded", vpath, 4, "subrip", "de", 1, 0, 1, "ok"))        # forçada: não serve de referência
    calls = []
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda path, idx, ffmpeg="ffmpeg", timeout=300: calls.append(idx) or srt_from(truth, 0.0).encode())
    refs = setup.get(f"/api/subtitles/{setup.pt}/cues").json()["references"]
    emb_refs = [r for r in refs if r.get("embedded")]
    assert [r["id"] for r in emb_refs] == [emb] and "#3" in emb_refs[0]["name"] and calls == []         # só se lista: nada é extraído
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": emb, "mode": "offset"}}).json()
    assert calls == [3] and pv["first_start_after"] < pv["first_start_before"] and abs((pv["first_start_before"] - pv["first_start_after"]) - 7000) < 400
    assert any("faixa embutida #3" in w and "tempos" in w for w in pv["warnings"])
    ap = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "reference", "params": {"reference_id": emb, "mode": "offset"}, "base_hash": pv["base_hash"]})
    assert ap.status_code == 200 and calls == [3]                                                      # a faixa extraída ficou em cache
    cues = parse_srt(setup.ptfile.read_text("utf-8")).cues
    assert abs(cues[0].start_ms - truth[0][0]) < 400
    # recusas: a própria legenda, forçada, imagem, extração falhada
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": setup.pt}}).status_code == 422
    forced = setup.core.db.one("SELECT id FROM subtitles WHERE kind='embedded' AND stream_index=4")["id"]
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": forced}}).status_code == 422
    setup.core._ref_cache.clear()
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda *a, **k: None)
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": emb, "mode": "offset"}}).status_code == 422


def test_already_aligned_message_shows_evidence(setup, monkeypatch):
    from app import ffprobe as fp
    row = setup.core.db.one("SELECT media_id, library_id FROM subtitles WHERE id=?", (setup.pt,))
    vpath = setup.core.db.one("SELECT path FROM media_items WHERE id=?", (row["media_id"],))["path"]
    truth = make_cues(150, 3600, seed=2)
    setup.ptfile.write_text(srt_from(truth, offset_s=0.0), "utf-8")
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=?", (row["library_id"],))), None, str(setup.dir), force_subs=True)
    setup.core.db.execute("INSERT INTO subtitles(media_id,library_id,kind,path,stream_index,format,lang,forced,hi,text_based,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (row["media_id"], row["library_id"], "embedded", vpath, 3, "subrip", "en", 0, 1, 1, "ok"))
    emb = setup.core.db.one("SELECT id FROM subtitles WHERE kind='embedded' AND stream_index=3")["id"]
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda *a, **k: srt_from(truth, 0.0).encode())
    r = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": emb}})
    d = r.json()["detail"]
    assert r.status_code == 400 and "offset ≈ 0" in d and "100 % a menos de 100 ms" in d and "diferença mediana 0 ms" in d and "Maiores diferenças" in d and "por construção" in d


def test_audio_analysis_ignores_hearing_impaired_descriptions():
    from app.services import App
    from app.subtitles.parser import Cue
    cues = [Cue(1000, 3000, "- Olá, tudo bem?"), Cue(3500, 5000, "[MÚSICA CORAL A TOCAR]"), Cue(6000, 8000, "FATIHA: Quem merece o meu melhor?"), Cue(9000, 10000, "(GEMIDOS CONTINUAM)")]
    out = App._speech_cues(cues)
    assert len(out) == 2 and out[0][0] == 1000 and out[1][0] == 6000
    assert len(App._speech_cues([Cue(0, 1000, "[MÚSICA]")])) == 1                                         # nada restaria: usa tudo em vez de falhar


def test_providers_are_searched_in_parallel(setup):
    import time as _t
    core = setup.core
    calls = []

    class Slow:
        supports_hash = False

        def __init__(self, n):
            self.n = n

        def configured(self):
            return True

        def search(self, q):
            calls.append((self.n, q.mode))
            _t.sleep(0.3)
            return []
    setup.put("/api/settings", json={"providers": {**core.settings.get("providers"), "subtitlecat": {**core.settings.get("providers")["subtitlecat"], "enabled": True}}})
    core.provider = lambda name: Slow(name)                                              # type: ignore[assignment]
    mid = core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]
    t0 = _t.monotonic()
    r = setup.post(f"/api/media/{mid}/search", json={"languages": ["en"], "sources": ["app"]})
    dt = _t.monotonic() - t0
    assert r.status_code == 200 and {n for n, _ in calls} >= {"opensubtitles", "subtitlecat"}
    assert dt < 0.9, dt                                                                  # em série seriam ≥ 1,2 s (2 fornecedores × 2 passos × 0,3 s)
    assert [t.split(":")[0] for t in r.json()["query"]["tried"]] == ["opensubtitles", "opensubtitles", "subtitlecat", "subtitlecat"]     # ordem de prioridade mantida


# ---- referência embutida por trechos (rápida) -----------------------------------------------

import shutil as _shutil
import subprocess as _sp

import pytest as _pytest


@_pytest.mark.skipif(not _shutil.which("ffmpeg"), reason="ffmpeg em falta")
def test_windowed_subtitle_extraction_keeps_original_timestamps(tmp_path):
    from app import ffprobe as fp
    cues = [(5000 + i * 4000, 7000 + i * 4000, f"Linha {i} aos {5000 + i * 4000}") for i in range(370)]               # ≈ 25 min
    srt = tmp_path / "s.srt"
    def fmt(ms):
        s, m = divmod(ms, 1000)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{m:03d}"
    srt.write_text("\n".join(f"{i}\n{fmt(a)} --> {fmt(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)), "utf-8")
    mkv = tmp_path / "t.mkv"
    r = _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1500:size=64x64:rate=1", "-f", "lavfi", "-i", "sine=duration=1500",
                 "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2", "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt", str(mkv)], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    wins = [(120.0, 60.0), (700.0, 60.0), (1300.0, 60.0)]
    data = fp.extract_subtitle_windows(str(mkv), 2, wins)
    got = parse_srt(data.decode("utf-8")).cues
    assert 40 <= len(got) <= 50 and all(any(w0 * 1000 <= c.start_ms < (w0 + d) * 1000 for w0, d in wins) for c in got)      # só dos trechos pedidos
    for c in got:
        assert f"aos {c.start_ms}" in c.text                                      # o tempo é o ORIGINAL (a mesma fala nunca mudou de instante)
    assert fp.extract_subtitle_windows("relativo.mkv", 2, wins) is None and fp.extract_subtitle_windows(str(mkv), 2, []) is None


def test_evaluator_compares_only_inside_reference_windows():
    from app.evaluator import Evaluation, evaluate_content
    from app.subtitles.parser import Cue, ParsedSub, analyze
    truth = make_cues(400, 1600, seed=3)                                                           # falas irregulares (como numa legenda real)
    wins = [(100_000, 280_000), (700_000, 880_000), (1_200_000, 1_380_000)]
    ref = ParsedSub("srt", [Cue(a, b, "x") for a, b in truth if any(w0 <= a < w1 for w0, w1 in wins)], windows=wins)
    cand = ParsedSub("srt", [Cue(a, b, "olá") for a, b in truth])                                    # a candidata cobre o filme todo
    an = analyze(cand, 1650.0, size=1000)
    ev = Evaluation(score=50, compat="probable", sync="not_confirmed")
    ev = evaluate_content(ev, {"duration": 1650.0}, cand, an, ("faixa embutida #2 (en)", ref))
    assert ev.sync == "validated_reference" and ev.alignment["windows"] == 3 and ev.alignment["match_ratio"] >= 0.95
    assert ev.alignment["events_compared"] < 200                                                    # só as falas dos trechos, não as 400
