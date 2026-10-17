import pytest

from app.subtitle_tools import ToolError, common_fixes, fix_uppercase, remove_hi, strip_style, transform_srt_text
from app.subtitles.parser import parse_srt
from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)
from tests.conftest import make_srt


def test_strip_style_only_known_tags():
    assert strip_style(["<i>Olá</i>, <b>mundo</b>", "{\\an8}Em cima", "<font color=\"#fff\">x</font>", "2 < 3 and 5 > 4"]) == ["Olá, mundo", "Em cima", "x", "2 < 3 and 5 > 4"]


def test_remove_hi_rules():
    assert remove_hi(["[porta a bater]"]) == []
    assert remove_hi(["(música tranquila)"]) == []
    assert remove_hi(["JOÃO: Olá, tudo bem?"]) == ["Olá, tudo bem?"]
    assert remove_hi(["♪ ♪"]) == [] and remove_hi(["♪ Parabéns a você ♪"]) == ["♪ Parabéns a você ♪"]          # letra fica
    assert remove_hi(["- [grita] Socorro!", "- (ri) Calma."]) == ["- Socorro!", "- Calma."]
    assert remove_hi(["- [porta]", "- Olá."]) == ["Olá."]                                                        # sobra 1 fala: sem travessão
    assert remove_hi(["Eu (pensava) que sim"]) == ["Eu (pensava) que sim"]                                      # parêntesis a meio da frase: preserva
    assert remove_hi(["Ele disse (baixo)"]) == ["Ele disse"]
    assert remove_hi(["Mr. Smith: hello"]) == ["Mr. Smith: hello"]                                              # não é só maiúsculas


def test_common_fixes():
    assert common_fixes(["Olá  ,  mundo !"]) == ["Olá, mundo!"]
    assert common_fixes(["-Olá", "– Tudo?"]) == ["- Olá", "- Tudo?"]
    assert common_fixes(["Isso é bom.Mas não é.."]) == ["Isso é bom. Mas não é..."]
    assert common_fixes(["Não…sei"]) == ["Não...sei"]
    assert common_fixes(["Les amis !"], "fr") == ["Les amis !"]                                                   # francês mantém o espaço antes de !
    assert common_fixes(["Hora: 10:30"]) == ["Hora: 10:30"] and common_fixes(["U.S.A"]) == ["U.S.A"]
    assert common_fixes(["   "]) == []


def test_fix_uppercase():
    assert fix_uppercase(["EU NÃO SEI O QUE FAZER."]) == ["Eu não sei o que fazer."]
    assert fix_uppercase(["THE FBI AND I ARE HERE. WE GO."], "en") == ["The FBI and I are here. We go."]
    assert fix_uppercase(["- OLÁ, JOÃO.", "- COMO ESTÁS?"]) == ["- Olá, joão.", "- Como estás?"]
    assert fix_uppercase(["Olá, JOÃO"]) == ["Olá, JOÃO"] and fix_uppercase(["STOP!"]) == ["STOP!"]               # não é uma fala toda em maiúsculas


SRT = ("1\r\n00:00:01,000 --> 00:00:02,000\r\n[música]\r\n\r\n2\r\n00:00:03,000 --> 00:00:05,000 X1:10 Y2\r\n<i>JOÃO: OLÁ  ,  TUDO BEM?</i>\r\n\r\n"
       "3\r\n00:00:06,000 --> 00:00:08,000\r\nNada a mexer aqui.\r\n\r\n4\r\n00:00:09,000 --> 00:00:10,000\r\nLegendas por Fulano www.site.com\r\n")


def test_transform_keeps_times_numbering_crlf_and_drops_empty():
    new, stats, changes = transform_srt_text(SRT, {"style": 1, "hi": 1, "common": 1, "uppercase": 1})
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")                       # CRLF preservado
    cues = parse_srt(new).cues
    assert [(c.start_ms, c.end_ms) for c in cues] == [(3000, 5000), (6000, 8000)]       # tempos originais das falas que restam
    assert cues[0].text == "Olá, tudo bem?" and cues[1].text == "Nada a mexer aqui."
    assert "00:00:03,000 --> 00:00:05,000 X1:10 Y2" in new and new.startswith("1\r\n00:00:03")   # renumerado, posição preservada
    assert stats["dropped"] == 2 and stats["watermark"] == 1 and stats["changed"] == 3 and stats["cues"] == 4
    assert {c["n"] for c in changes} == {1, 2, 4} and "(fala removida)" in changes[0]["after"]


def test_watermark_only_at_edges_and_options_required():
    mid = "\n\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n" + ("visite www.exemplo.com agora" if i == 5 else "texto normal") for i in range(1, 10)) + "\n"
    new, stats, _ = transform_srt_text(mid, {"common": 1})
    assert stats["watermark"] == 0 and "www.exemplo.com" in new                         # fala 5 de 9: não é nas pontas
    with pytest.raises(ToolError):
        transform_srt_text(SRT, {})


# ---- API ----------------------------------------------------------------------------

def test_api_preview_apply_restore_textfix(setup):
    dirty = ("1\n00:00:05,000 --> 00:00:07,000\n[música]\n\n2\n00:00:10,000 --> 00:00:12,500\n<i>JOÃO: Olá  mundo</i>\n\n3\n00:01:00,000 --> 00:01:02,000\nFim.\n")
    setup.ptfile.write_text(dirty, "utf-8")
    prm = {"hi": True, "style": True, "common": True}
    pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "textfix", "params": prm}).json()
    assert pv["kind"] == "text" and pv["stats"]["changed"] == 2 and pv["stats"]["dropped"] == 1 and setup.ptfile.read_text("utf-8") == dirty
    r = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": "textfix", "params": prm, "base_hash": pv["base_hash"]})
    assert r.status_code == 200
    cues = parse_srt(setup.ptfile.read_text("utf-8")).cues
    assert [(c.start_ms, c.text) for c in cues] == [(10000, "Olá mundo"), (60000, "Fim.")]
    assert len(list(setup.core.cfg.backup_dir.iterdir())) == 1
    mid = setup.core.db.one("SELECT media_id FROM subtitles WHERE id=?", (setup.pt,))["media_id"]
    eid = setup.get("/api/subtitle-edits", params={"media_id": mid}).json()[0]["id"]
    assert setup.post(f"/api/subtitle-edits/{eid}/restore", json={}).status_code == 200
    assert setup.ptfile.read_text("utf-8") == dirty                                    # restauro exato
    # nada a alterar / stale hash
    setup.ptfile.write_text("1\n00:00:01,000 --> 00:00:02,000\nTudo limpo.\n", "utf-8")
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "textfix", "params": prm}).status_code == 422
