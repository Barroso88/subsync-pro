import shutil
import subprocess
import wave

import numpy as np
import pytest

from app import audio_sync as A
from app.subtitles.parser import parse_srt
from tests.conftest import make_srt
from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)


def wait_job(c, jid):
    import time
    for _ in range(300):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("tarefa não terminou")

RNG = np.random.default_rng(7)


def make_cues(n=700, dur=7200, seed=1):
    rng = np.random.default_rng(seed)
    cues, last = [], 0.0
    for x in np.sort(rng.uniform(30, dur - 60, n)):
        x = max(float(x), last + 0.3)
        ln = float(rng.uniform(1.2, 4.0))
        cues.append((int(x * 1000), int((x + ln) * 1000)))
        last = x + ln
    return cues


def audio_for(cues, t0, dur, offset=0.0, scale=1.0, bg=80, speech=3000, music=0):
    """Áudio sintético: ruído de fundo + «fala» nos instantes das falas (tempo da legenda = scale·áudio + offset)."""
    n = int(dur * A.SR)
    a = RNG.normal(0, bg, n).astype(np.float32)
    for s, e in cues:
        i0, i1 = int(((s / 1000 - offset) / scale - t0) * A.SR), int(((e / 1000 - offset) / scale - t0) * A.SR)
        if i1 <= 0 or i0 >= n:
            continue
        a[max(0, i0):min(n, i1)] += RNG.normal(0, speech, min(n, i1) - max(0, i0))
    if music:
        a += RNG.normal(0, music, n).astype(np.float32)             # ruído/música contínua por cima
    return a.astype(np.int16)


def run(cues, **kw):
    ext = kw.pop("extract_cues", cues)
    return A.analyze("x", cues, kw.pop("dur", 7200), extract=lambda path, t0, d, ff, *a: audio_for(ext, t0, d, **kw))


def test_constant_offset_both_directions_and_already_aligned():
    cues = make_cues()
    r = run(cues, offset=3.2)                                        # legenda 3,2 s atrasada → somar −3,2 s
    assert r["mode"] == "offset" and abs(r["offset_s"] + 3.2) < 0.15 and r["scale"] == 1.0 and r["agree"] == r["windows_total"] == 10
    r = run(cues, offset=-7.0)
    assert r["mode"] == "offset" and abs(r["offset_s"] - 7.0) < 0.15
    r = run(cues, offset=0.0)
    assert r["mode"] == "aligned" and abs(r["offset_s"]) < 0.4 and r["confidence"] == 1.0


def test_frame_rate_drift_beyond_search_range_is_recovered():
    cues = make_cues()
    r = run(cues, scale=25 / 23.976)                                 # legenda a 25 fps num vídeo a 23,976: deriva de ~5 min no fim
    assert r["mode"] == "linear" and abs(r["scale"] - 23.976 / 25) < 2e-4 and abs(r["offset_s"]) < 0.5
    r = run(cues, scale=25 / 23.976, offset=4.0)                     # deriva + desvio inicial
    assert r["mode"] == "linear" and abs(r["scale"] - 23.976 / 25) < 2e-4
    fixed = [(int(r["scale"] * s + r["offset_s"] * 1000), int(r["scale"] * e + r["offset_s"] * 1000)) for s, e in cues]
    truth = [(int(((s / 1000 - 4.0) / (25 / 23.976)) * 1000), 0) for s, e in cues]
    assert max(abs(f[0] - t[0]) for f, t in zip(fixed, truth)) < 400      # depois do ajuste, as falas ficam a <0,4 s da verdade


def test_unrelated_subtitle_and_music_only_are_inconclusive_not_guessed():
    cues = make_cues()
    r = run(cues, extract_cues=make_cues(seed=99))                   # a legenda é de outro filme
    assert r["mode"] == "inconclusive" and r["agree"] == 0 and r["reason"]
    r = run(cues, speech=0, music=2000)                              # sem fala, só ruído contínuo
    assert r["mode"] == "inconclusive"


def test_robust_to_background_noise_and_some_bad_windows():
    cues = make_cues()
    r = run(cues, offset=2.0, bg=600, music=700)                     # muito ruído de fundo
    assert r["mode"] == "offset" and abs(r["offset_s"] + 2.0) < 0.25
    calls = {"n": 0}

    def flaky(path, t0, d, ff, *a):
        calls["n"] += 1
        if calls["n"] in (2, 5):
            return None                                              # ffmpeg falhou em 2 janelas
        return audio_for(cues, t0, d, offset=-5.0)
    r = A.analyze("x", cues, 7200, extract=flaky)
    assert r["mode"] == "offset" and abs(r["offset_s"] - 5.0) < 0.15 and r["windows_failed"] == 2 and r["agree"] == 8


def test_no_audio_at_all_raises_and_ffmpeg_command_is_safe(monkeypatch):
    with pytest.raises(A.AudioSyncError, match="extrair o áudio"):
        A.analyze("x", make_cues(), 7200, extract=lambda *a, **k: None)
    with pytest.raises(A.AudioSyncError, match="não tem falas"):
        A.analyze("x", [], 7200)
    seen = {}

    class CP:
        returncode, stdout = 0, np.zeros(A.SR * 10, dtype=np.int16).tobytes()

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return CP()
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    out = A.extract_window("/media/filmes/x y.mkv", 600, 180, "ffmpeg")
    cmd = seen["cmd"]
    assert isinstance(cmd, list) and cmd.index("-ss") < cmd.index("-i") and "/media/filmes/x y.mkv" in cmd and not seen["kw"].get("shell")   # sem shell; procura antes de abrir
    assert len(out) == A.SR * 10
    CP.returncode = 1
    assert A.extract_window("/x.mkv", 0, 60) is None


def srt_from(cues, offset_s=0.0):
    def ts(ms):
        ms = max(0, int(ms + offset_s * 1000))
        s, m = divmod(ms, 1000)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{m:03d}"
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\nFala número {i} do filme.\n" for i, (a, b) in enumerate(cues, 1))


def test_api_job_then_preview_apply_with_backup_and_restore(setup, monkeypatch):
    row = setup.core.db.one("SELECT media_id, library_id FROM subtitles WHERE id=?", (setup.pt,))
    mid = row["media_id"]
    truth = make_cues(300, 3600, seed=5)                              # falas irregulares como numa legenda real
    setup.ptfile.write_text(srt_from(truth, offset_s=7.0), "utf-8")   # a legenda está 7 s atrasada face ao áudio
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=?", (row["library_id"],))), None, str(setup.dir), force_subs=True)
    monkeypatch.setattr(A, "extract_window", lambda path, t0, d, ff="ffmpeg", timeout=0: audio_for(truth, t0, d, offset=0.0))
    setup.core.db.execute("UPDATE media_items SET duration=3600 WHERE id=?", (mid,))
    r = setup.post(f"/api/subtitles/{setup.pt}/audio-sync/start")
    assert r.status_code == 202
    j = wait_job(setup, r.json()["job_id"])
    assert j["status"] == "done", j
    res = j["result"]
    assert res["mode"] == "offset" and abs(res["offset_s"] + 7.0) < 0.3 and res["agree"] >= 6 and res["base_hash"]
    prm = {"scale": res["scale"], "offset_seconds": res["offset_s"]}
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "linear", "params": prm}).json()
    assert pv["first_start_after"] < pv["first_start_before"] and "áudio" in pv["summary"]
    before = setup.ptfile.read_bytes()
    ap = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "linear", "params": prm, "base_hash": pv["base_hash"]})
    assert ap.status_code == 200 and setup.ptfile.read_bytes() != before and len(list(setup.core.cfg.backup_dir.iterdir())) == 1
    # nova análise: já alinhada (prova do áudio) e marca a legenda como alinhada
    j2 = wait_job(setup, setup.post(f"/api/subtitles/{setup.pt}/audio-sync/start").json()["job_id"])
    assert j2["status"] == "done" and j2["result"]["mode"] == "aligned" and abs(j2["result"]["offset_s"]) < 0.4
    st = setup.core.db.one("SELECT sync_status, sync_detail FROM subtitles WHERE id=?", (setup.pt,))
    assert st["sync_status"] == "aligned" and "áudio" in st["sync_detail"]
    # restaurar devolve o original exato
    eid = ap.json()["edit_id"]
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={"force": True}).status_code == 200 and setup.ptfile.read_bytes() == before


def test_api_refuses_unknown_duration_and_failed_extraction_is_reported(setup, monkeypatch):
    mid = setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]
    setup.core.db.execute("UPDATE media_items SET duration=NULL WHERE id=?", (mid,))
    assert setup.post(f"/api/subtitles/{setup.pt}/audio-sync/start").status_code == 422
    setup.core.db.execute("UPDATE media_items SET duration=3600 WHERE id=?", (mid,))
    monkeypatch.setattr(A, "extract_window", lambda *a, **k: None)
    j = wait_job(setup, setup.post(f"/api/subtitles/{setup.pt}/audio-sync/start").json()["job_id"])
    assert j["status"] == "failed" and "extrair o áudio" in j["error"]
    r = setup.post(f"/api/subtitles/{setup.en}/audio-sync/start")
    assert r.status_code in (202, 422)
    if r.status_code == 202:
        wait_job(setup, r.json()["job_id"])                                  # não deixar tarefas a correr quando o teste acaba


def test_weak_per_window_evidence_adds_up_across_windows():
    """Áudio real é ruidoso: cada janela, sozinha, é fraca (z baixo), mas o desvio verdadeiro reforça-se de janela em janela."""
    cues = make_cues()
    r = run(cues, offset=3.0, speech=900, bg=500, music=600)
    assert r["mode"] == "offset" and abs(r["offset_s"] + 3.0) < 0.3 and r["z_total"] >= A.ZT_MIN
    assert max(w["z"] for w in r["windows"]) < 6 or r["agree"] >= 4                      # nenhuma janela chegava sozinha ao limiar antigo
    assert all("speech_pct" in w for w in r["windows"])                                   # mostra quanta «fala» foi detetada em cada janela


def test_audio_filter_uses_center_channel_only_for_multichannel(monkeypatch):
    class CP:
        returncode = 0

    def probe(layout):
        def fake(cmd, **kw):
            CP.stdout = f"{layout}\n".encode()
            return CP()
        return fake
    for layout, center in (("6,5.1(side)", True), ("8,7.1", True), ("6,", True), ("2,stereo", False), ("1,mono", False)):
        A._FILTER_CACHE.clear()
        monkeypatch.setattr(A.subprocess, "run", probe(layout))
        f = A.audio_filter("/x.mkv")
        assert ("pan=mono|c0=FC" in f) is center and "highpass=f=150" in f and "lowpass=f=4000" in f
    A._FILTER_CACHE.clear()
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("sem ffprobe")))
    assert A.audio_filter("/y.mkv") == "highpass=f=150,lowpass=f=4000"                    # sem ffprobe: mistura normal, não falha
    A._FILTER_CACHE.clear()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe em falta")
def test_real_ffmpeg_51_dialogue_in_center_beats_loud_music_in_other_channels(tmp_path):
    rng = np.random.default_rng(11)
    dur, sr = 1500, 16000
    cues = make_cues(260, dur, seed=4)
    n = dur * sr
    ch = np.zeros((n, 6), dtype=np.float32)
    for c in range(6):
        ch[:, c] = rng.normal(0, 12000 if c != 2 else 100, n)                              # música/efeitos fortes nos outros canais; centro quase só ruído de fundo
    for s, e in cues:
        i0, i1 = int(s / 1000 * sr), int(e / 1000 * sr)
        ch[i0:i1, 2] += rng.normal(0, 3500, i1 - i0)                                      # o diálogo está no canal central (índice 2)
    path = tmp_path / "filme51.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(6)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(ch, -32768, 32767).astype(np.int16).tobytes())
    A._FILTER_CACHE.clear()
    late = [(s + 3500, e + 3500) for s, e in cues]                                        # legenda 3,5 s atrasada
    assert "pan=mono|c0=FC" in A.audio_filter(str(path))                                  # o ffprobe diz 6 canais → usa o canal central
    r = A.analyze(str(path), late, dur, "ffmpeg")
    assert r["mode"] == "offset" and abs(r["offset_s"] + 3.5) < 0.2 and r["agree"] >= 6   # com o canal central encontra o desvio mesmo com música muito mais forte
    A._FILTER_CACHE.clear()


def test_short_cues_that_vanish_early_do_not_bias_the_offset():
    """Se a legenda tira a fala do ecrã cedo demais, o fim «curto» não deve enviesar o desvio estimado (usa-se o tempo que a fala demora)."""
    truth = make_cues(400, 3600, seed=8)
    short = [(s, s + 400) for s, _ in truth]                          # a legenda mostra cada fala só 0,4 s…
    ext = lambda cs: A.spoken_span([(s, e, 55) for s, e in cs])       # noqa: E731  (55 caracteres ≈ 3,9 s de fala)
    shifted = [(s + 3000, e + 3000) for s, e in short]                # …e está 3 s atrasada
    r = A.analyze("x", ext(shifted), 3600, extract=lambda p, t0, d, ff, *a: audio_for(truth, t0, d, offset=0.0))
    assert r["mode"] == "offset" and abs(r["offset_s"] + 3.0) < 0.3
    assert A.spoken_span([(0, 300, 10), (0, 9000, 400), (1000, 1500, 70)]) == [(0, 1200), (0, 9000), (1000, 6000)]    # mínimo 1,2 s, nunca encurta, máximo ~7 s de fala


def test_api_duration_tool_preview_apply_verifies_and_restores(setup):
    short = ("1\n00:00:01,000 --> 00:00:01,400\nOlá, tudo bem contigo hoje, meu amigo?\n\n"
             "2\n00:00:01,600 --> 00:00:02,000\nEstou bem, obrigado por perguntares.\n\n3\n00:00:10,000 --> 00:00:14,000\nUma fala longa com tempo de sobra para ser lida.\n")
    setup.ptfile.write_text(short, "utf-8")
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=1")), None, str(setup.dir), force_subs=True)
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "durations", "params": {}}).json()
    assert pv["kind"] == "text" and pv["stats"]["changed"] == 2 and "mais tempo no ecrã" in pv["summary"] and setup.ptfile.read_text("utf-8") == short
    ap = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "durations", "params": {}, "base_hash": pv["base_hash"]})
    assert ap.status_code == 200
    cues = parse_srt(setup.ptfile.read_text("utf-8")).cues
    assert [c.start_ms for c in cues] == [1000, 1600, 10000] and cues[0].end_ms == 1520 and cues[1].end_ms > 2000 and cues[2].end_ms == 14000   # inícios iguais; 1.ª limitada pela 2.ª
    assert setup.post(f"/api/subtitle-edits/{ap.json()['edit_id']}/restore", json={}).status_code == 200 and setup.ptfile.read_text("utf-8") == short
    # nada a alterar / opções inválidas
    ok = "1\n00:00:01,000 --> 00:00:05,000\nOlá.\n"
    setup.ptfile.write_text(ok, "utf-8")
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "durations", "params": {}}).status_code == 422
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "durations", "params": {"cps": 2}}).status_code == 400
