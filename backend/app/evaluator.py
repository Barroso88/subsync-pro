"""Avaliação de compatibilidade e sincronização de resultados de legendas.

Princípios (ver README):
- A pontuação é uma heurística explicável, NÃO uma probabilidade calibrada.
- "Compatibilidade" (esta legenda foi feita para este vídeo?) e "sincronização"
  (os tempos batem?) são eixos separados.
- Sincronização só é "validada" quando existe prova real: alinhamento com uma
  legenda de referência local. Duração compatível sozinha nunca valida.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .providers.base import SubtitleResult
from .subtitles.parser import AlignResult, Analysis, ParsedSub, align_with_reference, analyze_duration, fmt_ms

COMPAT = {
    "hash": "Confirmada por hash",
    "probable": "Provável (metadados)",
    "weak": "Correspondência fraca",
    "incompatible": "Provavelmente incompatível",
}
SYNC = {
    "validated_reference": "Alinhada com legenda de referência",
    "not_confirmed": "Sincronização não confirmada",
    "suspect": "Possível dessincronização",
    "inconclusive": "Inconclusivo",
}

_RES = re.compile(r"(2160p|1080p|720p|480p|4k)", re.I)
_SRC = re.compile(r"(blu-?ray|bdrip|brrip|remux|web-?dl|webrip|hdtv|dvdrip|hdrip|amzn|nf|dsnp)", re.I)
_GROUP = re.compile(r"-([A-Za-z0-9]+)(?:\.[a-z0-9]{2,4})?$")
_TOK = re.compile(r"[a-z0-9]+", re.I)
_NOISE_TOK = {"the", "a", "of", "and", "srt", "mkv", "mp4", "avi", "x264", "x265", "h264", "h265", "hevc", "aac", "ac3", "dts", "ddp5", "1", "5"}


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _tokens(s: str) -> set[str]:
    s = _fold(s)
    return {t.lower() for t in _TOK.findall(s) if t.lower() not in _NOISE_TOK and len(t) > 1}


def _norm_src(s: str) -> str:
    s = s.lower().replace("-", "").replace("bluray", "bluray")
    return {"bdrip": "bluray", "brrip": "bluray", "webdl": "web", "webrip": "web", "amzn": "web", "nf": "web", "dsnp": "web"}.get(s, s)


_YEAR_TOK = re.compile(r"^(?:19|20)\d{2}$")


def _title_tokens(s: str) -> set[str]:
    """Termos do título sem anos soltos: o OpenSubtitles devolve por vezes «2026 - Lockbox»."""
    t = _tokens(s)
    nt = {x for x in t if not _YEAR_TOK.match(x)}
    return nt or t


def _title_similarity(feature_title: str, titles: list[str | None]) -> float:
    a = _title_tokens(feature_title)
    best = 0.0
    for t in titles:
        if not t or not a:
            continue
        b = _title_tokens(t)
        if b:
            best = max(best, len(a & b) / len(a | b))
    return best


def release_group(name: str) -> str | None:
    m = _GROUP.search(name.strip())
    return m.group(1).lower() if m else None


@dataclass
class Factor:
    label: str
    points: int
    kind: str = "info"   # positive | negative | info

    def to_dict(self) -> dict:
        return {"label": self.label, "points": self.points, "kind": self.kind}


@dataclass
class Evaluation:
    score: int
    compat: str            # hash | probable | weak | incompatible
    sync: str              # validated_reference | not_confirmed | suspect | inconclusive
    factors: list[Factor] = field(default_factory=list)
    duration_diff_s: float | None = None
    alignment: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score, "compat": self.compat, "compat_label": COMPAT[self.compat],
            "sync": self.sync, "sync_label": SYNC[self.sync],
            "factors": [f.to_dict() for f in self.factors], "duration_diff_s": self.duration_diff_s,
            "alignment": self.alignment, "notes": self.notes,
            "disclaimer": "Pontuação heurística para ordenar resultados; não é uma probabilidade.",
        }


def evaluate_metadata(media: dict, r: SubtitleResult, *, hi_policy: str = "exclude", prefer_forced: bool = False) -> Evaluation:
    """Avaliação antes de descarregar (só metadados do fornecedor)."""
    f: list[Factor] = []
    score = 0
    incompatible = False

    def add(label: str, pts: int, kind: str | None = None):
        nonlocal score
        score += pts
        f.append(Factor(label, pts, kind or ("positive" if pts > 0 else "negative" if pts < 0 else "info")))

    if r.hash_match:
        add("Hash do ficheiro de vídeo coincide (feita para este ficheiro)", 55)
    if media["kind"] == "episode":
        if r.season is not None and r.episode is not None and media.get("season") is not None:
            if (r.season, r.episode) == (media["season"], media["episode"]):
                add("Temporada e episódio coincidem", 20)
                if r.feature_title and media.get("series_title"):
                    a, b = _tokens(r.feature_title), _tokens(media["series_title"])
                    if a and b and len(a & b) / len(a | b) >= 0.6:
                        add("Título da série coincide", 10)
            else:
                add(f"Episódio diferente (S{r.season:02d}E{r.episode:02d})", -60)
                incompatible = True
        else:
            add("Fornecedor não indicou temporada/episódio", 0, "info")
    else:
        id_match = False
        if media.get("imdb_id") and r.imdb_id and str(r.imdb_id).lstrip("t0") == str(media["imdb_id"]).lstrip("t0"):
            add("IMDb coincide", 15)
            id_match = True
        elif media.get("tmdb_id") and r.tmdb_id and r.tmdb_id == media["tmdb_id"]:
            add("TMDb coincide", 15)
            id_match = True
        if r.feature_title and media.get("title"):
            # o título do ficheiro e o do Radarr (muitas vezes noutra língua) são ambos válidos
            sim = _title_similarity(r.feature_title, [media.get("title"), media.get("radarr_title")])
            if sim >= 0.6:
                add("Título coincide", 8)
            elif id_match:
                add("Título escrito de outra forma (o IMDb/TMDb coincide)", 0, "info")   # os identificadores pesam mais do que o texto
            elif _title_tokens(r.feature_title) and _title_tokens(media["title"]):
                add("Título parece diferente", -25)
                incompatible = True
    if media.get("year") and r.feature_year:
        if media["year"] == r.feature_year:
            add("Ano coincide", 5)
        elif abs(media["year"] - r.feature_year) > 1:
            add(f"Ano diferente ({r.feature_year})", -15)
    # release
    vname = media["file_name"]
    rname = r.release or r.file_name
    if rname:
        a, b = _tokens(vname), _tokens(rname)
        if a and b:
            j = len(a & b) / len(a | b)
            pts = round(j * 20)
            add(f"Nome da release semelhante ({round(j * 100)} % de termos em comum)", pts, "positive" if pts >= 6 else "info")
        g1, g2 = release_group(media["file_name"].rsplit(".", 1)[0]), release_group(rname.rsplit(".", 1)[0] if "." in rname else rname)
        if g1 and g2 and g1 == g2:
            add(f"Mesmo release group ({g1})", 10)
        r1, r2 = _RES.search(vname), _RES.search(rname)
        if r1 and r2:
            add("Mesma resolução" if r1.group(1).lower() == r2.group(1).lower() else "Resolução diferente",
                3 if r1.group(1).lower() == r2.group(1).lower() else -3)
        s1, s2 = _SRC.search(vname), _SRC.search(rname)
        if s1 and s2:
            same = _norm_src(s1.group(1)) == _norm_src(s2.group(1))
            add("Mesma origem (web/bluray...)" if same else "Origem diferente (risco de offset)", 4 if same else -6)
    if r.fps and media.get("fps"):
        if abs(r.fps - media["fps"]) / media["fps"] < 0.005:
            add(f"Frame rate igual ({media['fps']:g} fps)", 4)
        else:
            add(f"Frame rate diferente ({r.fps:g} vs {media['fps']:g}): risco de deriva", -8)
    # qualidade/origem
    if r.from_trusted:
        add("Uploader de confiança", 3)
    if r.download_count:
        pts = min(4, int(math.log10(r.download_count + 1)))
        if pts:
            add(f"{r.download_count} downloads no fornecedor", pts)
    if r.ai_translated or r.machine_translated:
        add("Tradução automática/IA", -12)
    if r.hearing_impaired and hi_policy == "exclude":
        add("SDH/HI (não preferido)", -10)
    if r.forced and not prefer_forced:
        add("Legenda forçada (só partes estrangeiras)", -20)

    score = max(0, min(100, score))
    if incompatible:
        compat = "incompatible"
    elif r.hash_match:
        compat = "hash"
    elif score >= 35:
        compat = "probable"
    else:
        compat = "weak"
    ev = Evaluation(score, compat, "not_confirmed", f)
    if compat == "hash":
        ev.notes.append("O fornecedor indica que esta legenda foi enviada para um ficheiro com o mesmo hash. "
                        "Isto confirma o ficheiro-alvo, mas não foi medido o alinhamento temporal.")
    ev.notes.append("Sincronização não confirmada: só é possível validar após descarregar e comparar com uma referência.")
    return ev


def evaluate_content(ev: Evaluation, media: dict, parsed: ParsedSub, analysis: Analysis,
                     reference: tuple[str, ParsedSub] | None) -> Evaluation:
    """Refina a avaliação com a legenda descarregada (fase de preparação)."""
    def add(label: str, pts: int):
        ev.score = max(0, min(100, ev.score + pts))
        ev.factors.append(Factor(label, pts, "positive" if pts > 0 else "negative" if pts < 0 else "info"))

    ev.notes = [n for n in ev.notes if not n.startswith("Sincronização não confirmada")]
    dur = media.get("duration")
    if analysis.last_end_ms is not None and dur:
        ev.duration_diff_s = round(analysis.last_end_ms / 1000 - dur, 1)
    issues, dsync, detail = analyze_duration(parsed, dur)
    for i in issues:
        if i.code == "beyond_video":
            add("A legenda termina depois do fim do vídeo", -35)
            ev.compat = "incompatible"
        elif i.code == "truncated":
            add("A legenda parece truncada face à duração do vídeo", -15)
        else:
            ev.notes.append(i.message)
    if dsync == "plausible":
        add("Limites temporais compatíveis com a duração do vídeo (não prova sincronização)", 4)
    elif dsync == "unchecked":
        ev.notes.append(detail or "Duração do vídeo desconhecida.")

    if reference:
        ref_name, ref = reference
        res: AlignResult = align_with_reference(parsed.cues, ref.cues)
        if not res.ok:
            ev.sync = "inconclusive"
            ev.notes.append(f"Comparação com a referência «{ref_name}» inconclusiva: {res.reason}.")
        else:
            ev.alignment = {"reference": ref_name, "offset_ms": res.offset_ms, "match_ratio": res.match_ratio,
                            "drift_ms": res.drift_ms, "events_compared": res.compared}
            if res.match_ratio >= 0.6 and abs(res.offset_ms) <= 1500 and abs(res.drift_ms) <= 2000:
                ev.sync = "validated_reference"
                add(f"Alinhada com a legenda de referência «{ref_name}» ({round(res.match_ratio * 100)} % dos eventos)", 20)
                ev.notes.append("Validação feita contra uma legenda já existente que se assume sincronizada; não foi analisado o áudio.")
            elif res.match_ratio >= 0.4 and (abs(res.offset_ms) > 1500 or abs(res.drift_ms) > 2000):
                ev.sync = "suspect"
                add(f"Offset estimado de {res.offset_ms / 1000:+.1f} s face à referência (deriva {res.drift_ms / 1000:+.1f} s)", -20)
            elif res.match_ratio < 0.3:
                ev.sync = "inconclusive"
                ev.notes.append("Pouca correspondência com a referência (idiomas/cortes diferentes?): resultado inconclusivo.")
            else:
                ev.sync = "inconclusive"
    else:
        ev.sync = "suspect" if dsync == "suspect" else "not_confirmed"
        if ev.sync == "not_confirmed":
            ev.notes.append("Sincronização não confirmada: não existe legenda de referência local para comparar.")
    if dsync == "suspect" and ev.sync != "validated_reference":
        ev.sync = "suspect"
    return ev
