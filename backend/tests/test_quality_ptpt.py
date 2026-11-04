import pytest

from app.subtitle_tools import ToolError, adapt_ptpt, adapt_ptpt_line, analyze_quality, reflow_lines, reflow_srt
from app.subtitles.parser import parse_srt
from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)

ALL = {"vocab", "spelling", "gerund"}


def test_reflow_balances_merges_and_keeps_dialogue():
    new, tags = reflow_lines(["Esta é uma frase bastante longa que precisa de ser", "partida de outra forma"], 42)
    assert len(new) == 2 and all(len(l) <= 42 for l in new) and "reformatar" in tags
    assert " ".join(new) == "Esta é uma frase bastante longa que precisa de ser partida de outra forma"
    assert reflow_lines(["Olá,", "tudo bem?"], 42) == (["Olá, tudo bem?"], ["juntar"])
    assert reflow_lines(["Olá,", "tudo bem?"], 42, merge=False)[0] == ["Olá,", "tudo bem?"]
    d = ["- Onde estás?", "- Em casa."]
    assert reflow_lines(d, 42) == (d, [])                                       # diálogo nunca é mexido
    assert reflow_lines(["{\\an8}Em cima, texto com posição"], 42)[1] == []       # posições ficam
    ok = ["Esta frase tem a primeira linha longa,", "e a segunda também é longa o bastante."]
    assert reflow_lines(ok, 42) == (ok, [])
    # parte uma linha única demasiado longa; nunca perde palavras
    one = ["Uma linha única muito comprida que deve ser repartida em duas partes iguais"]
    new, _ = reflow_lines(one, 42)
    assert len(new) == 2 and " ".join(new) == one[0]
    # não cabe em 2 linhas: assinala e mantém as palavras
    huge = ["palavra " * 14]
    new, tags = reflow_lines(huge, 42)
    assert "continua longa" in tags and " ".join(" ".join(new).split()) == " ".join(huge[0].split())


def test_reflow_reopens_italic_across_lines():
    new, _ = reflow_lines(["<i>Esta é uma frase bastante longa que precisa de ser partida em duas</i>"], 42)
    assert len(new) == 2 and new[0].startswith("<i>") and new[0].endswith("</i>") and new[1].startswith("<i>") and new[1].endswith("</i>")


def test_reflow_srt_keeps_times_and_crlf():
    src = "1\r\n00:00:01,000 --> 00:00:04,000\r\nOlá,\r\ntudo bem contigo?\r\n\r\n2\r\n00:00:05,000 --> 00:00:08,000 X1:10\r\nEsta é uma frase bastante longa que precisa de ser partida\r\n"
    out, stats, changes = reflow_srt(src, {})
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "") and "X1:10" in out
    a, b = parse_srt(src).cues, parse_srt(out).cues
    assert [(c.start_ms, c.end_ms) for c in a] == [(c.start_ms, c.end_ms) for c in b] and stats["changed"] == 2
    with pytest.raises(ToolError):
        reflow_srt(src, {"max_len": 10})


def test_quality_report_finds_real_problems():
    src = ("1\n00:00:01,000 --> 00:00:01,400\nOi\n\n"
           "2\n00:00:01,300 --> 00:00:03,000\nEsta é uma linha com muito mais de quarenta e dois caracteres seguidos\n\n"
           "3\n00:00:03,500 --> 00:00:04,000\nUma fala com muitos caracteres a passar demasiado depressa no ecrã\n\n"
           "4\n00:00:02,000 --> 00:00:06,000\nA\nB\nC\n\n"
           "5\n00:00:09,000 --> 00:00:10,000\nLegendas por Fulano www.site.com\n")
    r = analyze_quality(src)
    keys = {i["key"]: i["count"] for i in r["issues"]}
    assert keys["overlap"] >= 1 and keys["order"] == 1 and keys["long_line"] >= 1 and keys["many_lines"] == 1
    assert keys["cps"] >= 1 and keys["short"] >= 1 and keys["watermark"] == 1
    assert r["cues"] == 5 and not r["clean"]
    d = analyze_quality("1\n00:00:01,000 --> 00:00:05,000\n- Consegues despachar isso antes do pôr do sol?\n- Com certeza.\n")
    assert [i["key"] for i in d["issues"]] == ["long_dialogue"] and d["issues"][0]["fix"] is None
    clean = analyze_quality("1\n00:00:01,000 --> 00:00:04,000\nTudo certo\naqui.\n")
    assert clean["clean"] and clean["issues"] == []


def test_ptpt_rules_precise():
    f = lambda s: adapt_ptpt_line(s, ALL)[0]
    assert f("Vou apanhar o ônibus.") == "Vou apanhar o autocarro."
    assert f("Estou fazendo o café da manhã.") == "Estou a fazer o pequeno-almoço."
    assert f("Ele está me ouvindo?") == "Ele está a ouvir-me?"
    assert f("Estamos nos preparando.") == "Estamos a preparar-nos."
    assert f("Ela estava dormindo, ele estava vendo TV.") == "Ela estava a dormir, ele estava a ver TV."
    assert f("Está chovendo!") == "Está a chover!"
    assert f("Ela está lindo.") == "Ela está lindo."                        # adjetivo, não gerúndio
    assert f("Onde está o meu celular?") == "Onde está o meu telemóvel?" and f("O tecido celular") == "O tecido celular"
    assert f("A GELADEIRA está vazia.") == "O FRIGORÍFICO está vazia." or f("A geladeira está vazia.") == "O frigorífico está vazia."
    assert f("Vá ao banheiro.") == "Vá à casa de banho."
    assert f("O Antônio e a Mônica compraram tênis.") == "O António e a Mónica compraram ténis."
    assert f("Meu contato, sem recepção.") == "Meu contacto, sem receção."
    assert f("O garoto e as garotas.") == "O rapaz e as raparigas."
    assert f("Você tem fato novo?") == "Você tem fato novo?"                 # ambíguos ficam de fora
    assert f("Tem estado a fazer") == "Tem estado a fazer"                   # já é pt-PT
    assert f("Chama o Jeff") == "Chama o Jeff" and f("Mundo lindo") == "Mundo lindo"
    assert adapt_ptpt_line("ônibus", {"spelling"})[0] == "ônibus"            # categorias independentes


def test_ptpt_srt_keeps_times_and_needs_category():
    src = "1\r\n00:00:01,000 --> 00:00:03,000\r\n<i>Estou comendo no ônibus</i>\r\n\r\n2\r\n00:00:04,000 --> 00:00:05,000\r\nNada.\r\n"
    out, stats, ch = adapt_ptpt(src, {})
    assert "<i>Estou a comer no autocarro</i>" in out and stats["changed"] == 1 and "\r\n" in out
    assert [(c.start_ms, c.end_ms) for c in parse_srt(src).cues] == [(c.start_ms, c.end_ms) for c in parse_srt(out).cues]
    with pytest.raises(ToolError):
        adapt_ptpt(src, {"vocab": False, "spelling": False, "gerund": False})


def test_endpoints_quality_reflow_ptpt(setup):  # noqa: F811
    src = ("1\n00:00:01,000 --> 00:00:04,000\nEstou fazendo café da manhã no ônibus e esta linha é bem comprida demais\n\n"
           "2\n00:00:05,000 --> 00:00:06,000\nNada.\n")
    setup.ptfile.write_text(src, "utf-8")
    setup.core.scanner.scan_library(dict(setup.core.db.one("SELECT * FROM libraries WHERE id=1")), None, str(setup.dir), force_subs=True)
    q = setup.get(f"/api/subtitles/{setup.pt}/quality").json()
    assert any(i["key"] == "long_line" for i in q["issues"])
    for action, expect in (("reflow", None), ("ptpt", "autocarro")):
        pv = setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": action, "params": {}}).json()
        assert pv["kind"] == "text" and pv["stats"]["changed"] == 1 and setup.ptfile.read_text("utf-8") == src
        ap = setup.post(f"/api/subtitles/{setup.pt}/apply", json={"action": action, "params": {}, "base_hash": pv["base_hash"]})
        assert ap.status_code == 200, ap.text
        new = setup.ptfile.read_text("utf-8")
        assert [(c.start_ms, c.end_ms) for c in parse_srt(new).cues] == [(1000, 4000), (5000, 6000)]
        if expect:
            assert expect in new
        assert setup.post(f"/api/subtitle-edits/{ap.json()['edit_id']}/restore", json={}).status_code == 200 and setup.ptfile.read_text("utf-8") == src
    assert setup.post(f"/api/subtitles/{setup.pt}/preview", json={"action": "reflow", "params": {"max_len": 5}}).status_code == 400
