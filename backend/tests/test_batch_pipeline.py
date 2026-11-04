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
    assert {e["action"] for e in eds} >= {"convert", "textfix"} and all(e["backup_exists"] for e in eds)
    assert setup.core.db.one("SELECT 1 FROM events WHERE message LIKE 'Pipeline automático%'")
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 2                       # um backup por passo, sem se sobreporem
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
