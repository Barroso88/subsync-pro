from app import ffprobe as fp
from app.evaluator import evaluate_content, evaluate_metadata
from app.providers.base import SubtitleResult
from app.subtitles.parser import analyze, parse_srt
from tests.conftest import make_srt
from tests.test_subtitle_tools import setup  # noqa: F401  (fixture reutilizada)


def _ep(setup):
    r = setup.core.db.one("SELECT id FROM media_items WHERE file_name LIKE '%S01E02%'")
    return setup.core.media_dict(r["id"])


def test_embedded_track_is_used_as_reference_once_and_cached(setup, monkeypatch):
    calls = []
    emb = make_srt(120, 30_000, lang="en").encode()
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda path, idx, ffmpeg="ffmpeg", timeout=300: calls.append(idx) or emb)
    media = _ep(setup)
    ref = setup.core._reference_for(media, "pt-PT")
    assert ref and ref[0].startswith("faixa embutida #2") and len(ref[1].cues) == 120
    assert setup.core._reference_for(media, "pt-PT") and calls == [2]                      # 2.ª vez: cache, sem reabrir o vídeo
    # candidata alinhada → validada; desfasada 20 s → suspeita (a prova funciona nos dois sentidos)
    good = parse_srt(make_srt(120, 30_000, lang="pt-PT", offset_ms=300))
    bad = parse_srt(make_srt(120, 30_000, lang="pt-PT", offset_ms=20_000))
    r = SubtitleResult(provider="opensubtitles", file_id="1", language="pt-PT", release="x", file_name="x", feature_title="Minha Série")
    for parsed, want in ((good, "validated_reference"), (bad, "suspect")):
        ev = evaluate_metadata(media, r)
        ev = evaluate_content(ev, media, parsed, analyze(parsed, media.get("duration")), ref)
        assert ev.sync == want, (want, ev.sync, ev.notes)
    assert any("embutida" in n for n in evaluate_content(evaluate_metadata(media, r), media, good, analyze(good, None), ref).notes)


def test_embedded_reference_can_be_disabled_and_failures_are_safe(setup, monkeypatch):
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda *a, **k: None)                # ffmpeg falha / faixa ilegível
    media = _ep(setup)
    assert setup.core._reference_for(media, "pt-PT") is None
    called = []
    monkeypatch.setattr(fp, "extract_subtitle_text", lambda *a, **k: called.append(1) or b"")
    setup.core.settings.set("embedded_reference", False)
    setup.core._ref_cache.clear()
    assert setup.core._reference_for(media, "pt-PT") is None and not called                # desativado: nunca abre o vídeo
