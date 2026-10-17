import pytest

from app.subtitle_tools import ToolError, fix_mojibake, plan_convert
from app.subtitles.parser import parse_srt
from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)

ASS = ("[Script Info]\nTitle: x\n\n[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n\n[Events]\n"
       "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       "Dialogue: 0,0:00:05.00,0:00:07.50,Default,,0,0,0,,{\\an8}{\\i1}Olá{\\i0}, mundo\\Nsegunda linha\n"
       "Dialogue: 0,0:00:10.10,0:00:12.00,Default,,0,0,0,,{\\p1}m 0 0 l 10 10{\\p0}\n"
       "Comment: 0,0:00:11.00,0:00:12.00,Default,,0,0,0,,isto é um comentário\n"
       "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Primeira, com vírgula\n")
VTT = ("WEBVTT\n\nNOTE comentário\n\nintro\n00:01.000 --> 00:03.000 align:start\n<v João>Olá <i>mundo</i></v>\n\n"
       "01:00:00.500 --> 01:00:02.000\n<c.cor>Tudo &amp; nada</c.cor>\n")


def test_ass_to_srt_keeps_times_italics_and_drops_drawings():
    p = plan_convert(ASS.encode("utf-8"), ".ass")
    cues = parse_srt(p["new_text"]).cues
    assert [(c.start_ms, c.end_ms) for c in cues] == [(1000, 3000), (5000, 7500)]            # ordenado; desenho e comentário fora
    assert cues[1].text == "Olá, mundo\nsegunda linha" and "<i>Olá</i>, mundo" in p["new_text"]
    assert p["fmt"] == "ass" and p["dropped"] == 1 and any("Posições" in w for w in p["warnings"])


def test_vtt_to_srt():
    p = plan_convert(VTT.encode("utf-8"), ".vtt")
    cues = parse_srt(p["new_text"]).cues
    assert [(c.start_ms, c.end_ms) for c in cues] == [(1000, 3000), (3600500, 3602000)]
    assert cues[0].text == "Olá mundo" and "<i>mundo</i>" in p["new_text"] and cues[1].text == "Tudo & nada"


def test_mojibake_fix_is_conservative():
    assert fix_mojibake("Ã£o Ã© vocÃª")[0] == "ão é você" or fix_mojibake("Ã£o Ã© vocÃª")[1] == 1
    assert fix_mojibake("Não, é você") == ("Não, é você", 0)                                    # texto bom não se toca
    assert fix_mojibake("Ã£ misto não") == ("Ã£ misto não", 0)                                   # misto: não arrisca


def test_srt_cp1252_and_bom_become_utf8():
    srt = "1\n00:00:01,000 --> 00:00:02,000\nNão sei o que é isso, você.\n"
    p = plan_convert(srt.encode("cp1252"), ".srt")
    assert p["enc"] == "cp1252" and p["new_text"] == srt and p["changes"]
    p = plan_convert(b"\xef\xbb\xbf" + srt.encode("utf-8"), ".srt")
    assert p["bom"] and "sem BOM" in p["changes"][0]
    with pytest.raises(ToolError):
        plan_convert(srt.encode("utf-8"), ".srt")                                             # já está bem
    moji = srt.encode("utf-8").decode("cp1252").encode("utf-8")                               # UTF-8 gravado duas vezes
    p = plan_convert(moji, ".srt")
    assert p["new_text"] == srt and p["moji"] == 1


def test_api_convert_ass_creates_srt_backs_up_and_restores(setup):
    d = setup.dir
    ass = d / "Filme Teste (2024).pt-BR.ass"
    ass.write_text(ASS, "utf-8")
    setup.post("/api/scan", json={})
    from tests.test_subtitle_tools import wait_job
    wait_job(setup.core, setup.post("/api/scan", json={}).json()["job_id"])
    sid = setup.core.db.one("SELECT id FROM subtitles WHERE kind='external' AND path=? AND present=1", (str(ass),))["id"]
    pv = setup.post(f"/api/subtitles/{sid}/preview", json={"action": "convert", "params": {}}).json()
    assert pv["kind"] == "convert" and pv["target"] == "Filme Teste (2024).pt-BR.srt" and ass.exists()
    r = setup.post(f"/api/subtitles/{sid}/apply", json={"action": "convert", "params": {}, "base_hash": pv["base_hash"]})
    assert r.status_code == 200 and not ass.exists() and (d / "Filme Teste (2024).pt-BR.srt").exists()
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 1
    mid = setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (sid,))["media_id"]
    eid = setup.get("/api/subtitle-edits", params={"media_id": mid}).json()[0]["id"]
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={}).status_code == 200
    assert ass.read_text("utf-8") == ASS and not (d / "Filme Teste (2024).pt-BR.srt").exists()      # restauro exato; .srt removido


def test_api_convert_never_overwrites_existing_srt(setup):
    d = setup.dir
    vtt = d / "Filme Teste (2024).pt-PT.vtt"
    vtt.write_text(VTT, "utf-8")
    from tests.test_subtitle_tools import wait_job
    wait_job(setup.core, setup.post("/api/scan", json={}).json()["job_id"])
    sid = setup.core.db.one("SELECT id FROM subtitles WHERE kind='external' AND path=? AND present=1", (str(vtt),))["id"]
    r = setup.post(f"/api/subtitles/{sid}/preview", json={"action": "convert", "params": {}})
    assert r.status_code == 409 and "não é substituído" in r.json()["detail"]                       # já existe o .pt-PT.srt
