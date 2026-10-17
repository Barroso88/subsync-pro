import os
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.subtitle_tools import ToolError, estimate_sync, make_mapper, parse_time_ms, retime_srt
from app.subtitles.parser import parse_srt
from tests.conftest import make_srt, wait_job

SRT = ("1\n00:00:05,000 --> 00:00:07,000 X1:30 Y2\n<i>Olá, mundo</i>\n\n2\n00:00:10,000 --> 00:00:12,500\nLinha A\nLinha B\n\n3\n00:01:00,000 --> 00:01:02,000\nFim\n")


def test_retime_preserves_text_formatting_and_coordinates():
    out, st = retime_srt(SRT, lambda t: t + 2500)
    assert "00:00:07,500 --> 00:00:09,500 X1:30 Y2" in out and "<i>Olá, mundo</i>" in out and "Linha A\nLinha B" in out
    assert "00:01:02,500 --> 00:01:04,500" in out and st == {"cues": 3, "dropped": 0, "clamped": 0}
    back, _ = retime_srt(out, lambda t: t - 2500)
    assert back == SRT                                   # ida e volta devolve exatamente o original


def test_retime_negative_clamps_and_drops_and_renumbers():
    out, st = retime_srt(SRT, lambda t: t - 8000)        # 1.ª fala fica toda antes de 0
    assert st["dropped"] == 1 and st["cues"] == 3
    c = parse_srt(out).cues
    assert len(c) == 2 and c[0].start_ms == 2000 and out.startswith("1\n")      # renumerado
    out2, st2 = retime_srt(SRT, lambda t: t - 6000)
    assert st2["clamped"] == 1 and "00:00:00,000 --> 00:00:01,000" in out2


def test_mappers_math_and_validation():
    cues = parse_srt(make_srt(60, 30_000)).cues
    fn, info = make_mapper("shift", {"seconds": -1.5}, cues)
    assert fn(10_000) == 8500
    fn, _ = make_mapper("fps", {"from_fps": 23.976, "to_fps": 25}, cues)
    assert fn(1_000_000) == round(1_000_000 * 23.976 / 25)
    fn, _ = make_mapper("twopoint", {"cue1": 1, "time1": "00:00:12,000", "cue2": 60, "time2": "00:30:20"}, cues)
    assert fn(cues[0].start_ms) == 12_000 and abs(fn(cues[59].start_ms) - 1_820_000) <= 1
    for bad in (("shift", {"seconds": 0}), ("shift", {"seconds": 99999}), ("fps", {"from_fps": 25, "to_fps": 25}), ("fps", {"from_fps": 5, "to_fps": 100}),
                ("twopoint", {"cue1": 1, "time1": "0", "cue2": 1, "time2": "10"}), ("twopoint", {"cue1": 1, "time1": "0", "cue2": 60, "time2": "99999"}), ("zzz", {})):
        with pytest.raises(ToolError):
            make_mapper(bad[0], bad[1], cues)
    assert parse_time_ms("01:02:03,500") == 3723500 and parse_time_ms("12,5") == 12500 and parse_time_ms(2) == 2000


def test_estimate_sync_recovers_offset_and_linear_drift():
    ref = parse_srt(make_srt(120, 30_000, lang="pt-PT")).cues
    cand = parse_srt(make_srt(120, 30_000, lang="en", offset_ms=7000)).cues
    est = estimate_sync(cand, ref)
    assert est["ok"] and abs(est["offset_ms"] + 7000) <= 500 and est["match_ratio"] > 0.9
    drift = parse_srt(make_srt(120, 30_060, lang="en")).cues          # deriva crescente (~+0,2 %, 7 s no fim)
    est2 = estimate_sync(drift, ref)
    assert est2["ok"] and est2.get("linear")
    fn, _ = make_mapper("reference", {"mode": "linear", "reference_id": 0}, drift, type("P", (), {"cues": ref})())
    assert abs(fn(drift[100].start_ms) - ref[100].start_ms) < 1500


# ---- API: pré-visualizar, aplicar, backup, restaurar, apagar -----------------------

@pytest.fixture
def setup(cfg, fake_probe, media_root):
    d = media_root / "Filmes" / "Filme Teste (2024)"
    (d / "Filme Teste (2024).pt-PT.srt").write_text(make_srt(120, 30_000, lang="pt-PT", offset_ms=7000), "utf-8")   # desfasada +7 s vs EN
    app = create_app(cfg)
    with TestClient(app) as c:
        c.core, c.root, c.dir = app.state.core, media_root, d
        c.post("/api/libraries", json={"name": "Filmes", "type": "movies", "path": str(media_root / "Filmes"), "writable": True})
        c.post("/api/libraries", json={"name": "Séries", "type": "series", "path": str(media_root / "Séries"), "writable": False})
        wait_job(c.core, c.post("/api/scan", json={}).json()["job_id"])
        q = lambda name: c.core.db.one("SELECT id FROM subtitles WHERE kind='external' AND path=?", (str(d / name),))["id"]
        c.pt, c.en = q("Filme Teste (2024).pt-PT.srt"), q("Filme Teste (2024).en.srt")
        c.ptfile = d / "Filme Teste (2024).pt-PT.srt"
        yield c


def test_preview_does_not_touch_file_then_apply_backs_up_and_validates(setup):
    before = setup.ptfile.read_bytes()
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "shift", "params": {"seconds": -7}}).json()
    assert setup.ptfile.read_bytes() == before and pv["total"] == 120 and pv["head"][0]["new_start"] == pv["head"][0]["old_start"] - 7000
    r = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "shift", "params": {"seconds": -7}, "base_hash": pv["base_hash"]}).json()
    assert setup.ptfile.read_bytes() != before and len(parse_srt(setup.ptfile.read_text("utf-8")).cues) == 120
    ed = setup.get("/api/subtitle-edits", params={"media_id": setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]}).json()
    assert ed[0]["id"] == r["edit_id"] and ed[0]["backup_exists"]
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 1
    assert (setup.dir / "Filme Teste (2024).mkv").exists() and not [p for p in setup.dir.iterdir() if p.name.endswith("subsync-tmp")]


def test_stale_hash_readonly_nonsrt_symlink_are_refused(setup):
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "shift", "params": {"seconds": 1}}).json()
    setup.ptfile.write_text(make_srt(120, lang="pt-PT"), "utf-8")                    # alterado entretanto
    r = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "shift", "params": {"seconds": 1}, "base_hash": pv["base_hash"]})
    assert r.status_code == 409
    # biblioteca só de leitura
    sd = setup.root / "Séries" / "Minha Série" / "Season 01"
    ro = sd / "Minha Série - S01E01.pt-PT.srt"
    sid = setup.core.db.one("SELECT id FROM subtitles WHERE path=?", (str(ro),))["id"]
    assert setup.post(f"/api/subtitles/{sid}/preview", json={"action": "shift", "params": {"seconds": 1}}).status_code == 403
    assert setup.post(f"/api/subtitles/{sid}/delete").status_code == 403
    # atalho para fora
    outside = setup.root / "fora.srt"
    outside.write_text(make_srt(60), "utf-8")
    setup.ptfile.unlink()
    setup.ptfile.symlink_to(outside)
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "shift", "params": {"seconds": 1}}).status_code in (403, 422)
    assert outside.read_text("utf-8") == make_srt(60)


def test_reference_sync_fixes_offset_and_restore_returns_original(setup):
    original = setup.ptfile.read_bytes()
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": setup.en}}).json()
    assert abs(pv["estimate"]["offset_ms"] + 7000) <= 500 and pv["estimate"]["match_ratio"] > 0.9
    setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "reference", "params": {"reference_id": setup.en}, "base_hash": pv["base_hash"]})
    en = parse_srt(open(setup.dir / "Filme Teste (2024).en.srt", encoding="utf-8").read()).cues
    new = parse_srt(setup.ptfile.read_text("utf-8")).cues
    assert abs(new[0].start_ms - en[0].start_ms) < 1000                                # ficou alinhada com a referência
    mid = setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]
    eid = setup.get("/api/subtitle-edits", params={"media_id": mid}).json()[0]["id"]
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={}).json()["restored"]
    assert setup.ptfile.read_bytes() == original                                       # restauro exato
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={}).status_code == 409   # só uma vez


def test_restore_refuses_if_file_changed_unless_forced_and_reference_must_be_same_movie(setup):
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "shift", "params": {"seconds": -7}}).json()
    setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "shift", "params": {"seconds": -7}, "base_hash": pv["base_hash"]})
    mid = setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]
    eid = setup.get("/api/subtitle-edits", params={"media_id": mid}).json()[0]["id"]
    setup.ptfile.write_text(make_srt(30), "utf-8")                                     # alguém mexeu no ficheiro
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={}).status_code == 409
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={"force": True}).status_code == 200
    other = setup.core.db.one("SELECT id FROM subtitles WHERE media_id!=? AND kind='external' AND status='ok' LIMIT 1", (mid,))
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reference", "params": {"reference_id": other["id"]}}).status_code == 422


def test_delete_moves_to_backup_and_restore_recreates(setup):
    original = setup.ptfile.read_bytes()
    assert setup.post(f"/api/subtitles/{setup.pt}/delete").status_code == 200
    assert not setup.ptfile.exists() and (setup.dir / "Filme Teste (2024).mkv").exists()
    mid = setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]
    ed = setup.get("/api/subtitle-edits", params={"media_id": mid}).json()[0]
    assert ed["action"] == "delete" and ed["backup_exists"]
    setup.ptfile.write_text("novo", "utf-8")                                          # entretanto criaram outro com o mesmo nome
    assert setup.post(f"/api/subtitle-edits/{ed['id']}/restore", json={}).status_code == 409
    setup.ptfile.unlink()
    assert setup.post(f"/api/subtitle-edits/{ed['id']}/restore", json={}).status_code == 200
    assert setup.ptfile.read_bytes() == original


def test_cues_endpoint_lists_head_tail_and_references(setup):
    c = setup.get(f"/api/subtitles/{setup.pt}/cues").json()
    assert c["total"] == 120 and len(c["head"]) == 30 and len(c["tail"]) == 30 and any(r["lang"] == "en" for r in c["references"])
    assert setup.get("/api/subtitles/empty").status_code == 200                       # rota fixa não é capturada por {sid}
