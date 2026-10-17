from pathlib import Path

from app.languages import detect_language, normalize_lang
from app.naming import identify, parse_subtitle_name
from app.subtitles.parser import (analyze, align_with_reference, parse_ass, parse_bytes, parse_srt, parse_vtt,
                                  looks_like_html_error)
from tests.conftest import make_srt


def test_identify_movie_with_year_in_folder():
    r = Path("/m/Filmes")
    mid = identify(r / "Filme Teste (2024)" / "Filme Teste (2024).mkv", r, "movies")
    assert (mid.kind, mid.title, mid.year) == ("movie", "Filme Teste", 2024)


def test_identify_movie_scene_name():
    r = Path("/m")
    mid = identify(r / "The.Great.Movie.2019.1080p.BluRay.x264-GRP.mkv", r, "movies")
    assert mid.title == "The Great Movie" and mid.year == 2019


def test_identify_episode_from_tree_and_tag():
    r = Path("/m/Séries")
    mid = identify(r / "Minha Série" / "Season 02" / "Minha Série - S02E05 - Titulo.mkv", r, "series")
    assert (mid.kind, mid.series_title, mid.season, mid.episode) == ("episode", "Minha Série", 2, 5)
    assert mid.episode_title == "Titulo"


def test_identify_episode_nxnn_and_flat_layout():
    r = Path("/m/S")
    mid = identify(r / "Show.Name.1x07.720p.mkv", r, "series")
    assert (mid.season, mid.episode) == (1, 7) and mid.series_title.startswith("Show Name")


def test_subtitle_name_tags():
    n = parse_subtitle_name(Path("/x/Filme (2024).pt-PT.forced.srt"))
    assert (n.lang, n.forced, n.hi, n.stem_match) == ("pt-PT", True, False, "Filme (2024)")
    n = parse_subtitle_name(Path("/x/Serie.S01E01.en.sdh.srt"))
    assert (n.lang, n.hi, n.stem_match) == ("en", True, "Serie.S01E01")
    n = parse_subtitle_name(Path("/x/Serie.S01E01.srt"))
    assert n.lang is None and n.stem_match == "Serie.S01E01"


def test_pt_variants_not_equivalent():
    assert normalize_lang("pt-PT") == "pt-PT" and normalize_lang("pt-br") == "pt-BR" and normalize_lang("por") == "pt"
    assert normalize_lang("und") is None


def test_language_detection():
    assert detect_language(make_srt(60, lang="en") .replace("-->", " "))[0] == "en"
    lang, conf = detect_language(" ".join(__import__("tests.conftest", fromlist=["x"]).PT_PT * 12))
    assert lang == "pt-PT"


def test_parse_srt_and_timestamps():
    p = parse_srt("1\n00:00:01,500 --> 00:00:03,000\n<i>Olá</i>\n\n2\n01:02:03,004 --> 01:02:05,000\nFim\n")
    assert [(c.start_ms, c.end_ms, c.text) for c in p.cues] == [(1500, 3000, "Olá"), (3723004, 3725000, "Fim")]


def test_parse_vtt_and_ass():
    v = parse_vtt("WEBVTT\n\n00:01.000 --> 00:02.500\nHi\n\nNOTE x\n\n01:00:00.000 --> 01:00:01.000\nBye")
    assert len(v.cues) == 2 and v.cues[0].start_ms == 1000
    a = parse_ass("[Script Info]\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                  "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\i1}Hello, world\\Nline2\n")
    assert a.cues[0].start_ms == 1000 and a.cues[0].text == "Hello, world\nline2"


def test_parse_bytes_encodings():
    data = "1\n00:00:01,000 --> 00:00:02,000\nCoração e ação\n".encode("cp1252")
    p = parse_bytes(data, ".srt")
    assert p.encoding == "cp1252" and "Coração" in p.cues[0].text


def test_invalid_and_empty_detection():
    assert analyze(None, 100, size=10).status == "invalid"
    assert analyze(None, 100, size=0).status == "empty"
    assert looks_like_html_error(b"<!DOCTYPE html><html>error</html>")


def test_duration_checks_never_claim_sync():
    p = parse_srt(make_srt(120, 30_000))     # termina ~ 3612 s
    a = analyze(p, 3700)
    assert a.sync_status == "plausible" and "não prova" in a.sync_detail
    assert analyze(p, 1800).sync_status == "suspect"        # lá do vídeo
    short = parse_srt(make_srt(30, 30_000))
    assert analyze(short, 7200).sync_status == "suspect"    # truncada
    assert analyze(p, None).sync_status == "unchecked"


def test_alignment_detects_offset_and_same_timing():
    ref = parse_srt(make_srt(120, 30_000, lang="pt-PT"))
    same = parse_srt(make_srt(120, 30_000, lang="en"))
    r = align_with_reference(same.cues, ref.cues)
    assert r.ok and abs(r.offset_ms) <= 500 and r.match_ratio > 0.9
    shifted = parse_srt(make_srt(120, 30_000, lang="en", offset_ms=7000))
    r2 = align_with_reference(shifted.cues, ref.cues)
    assert abs(abs(r2.offset_ms) - 7000) <= 500 and r2.match_ratio > 0.9


def test_titles_starting_with_tech_words_are_not_erased():
    r = Path("/media/filmes")
    for fname, title, year in [("Web.Of.Lies.2009.1080p.WEBRip.x264.AAC5.1-[YTS.MX].mp4", "Web Of Lies", 2009),
                                ("HDTV.Stories.2015.720p.mkv", "HDTV Stories", 2015),
                                ("Spider.Web.2002.1080p.BluRay.mp4", "Spider Web", 2002)]:
        mid = identify(r / "X (2009) [1080p] [YTS.MX]" / fname, r, "movies")
        assert mid.title == title, (fname, mid)


def test_title_without_year_keeps_leading_tech_word():
    r = Path("/media/filmes")
    assert identify(r / "Web.Of.Lies.1080p.WEBRip.x264.mp4", r, "movies").title == "Web Of Lies"
    assert identify(r / "Some.Movie.1080p.WEBRip.x264.mp4", r, "movies").title == "Some Movie"
