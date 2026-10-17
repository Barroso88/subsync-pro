"""Identificação de filmes/episódios e de etiquetas em nomes de legendas."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .languages import normalize_lang

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
TEXT_SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

_SXXEXX = re.compile(r"[Ss](\d{1,3})[ ._-]*[Ee](\d{1,4})(?:[-_ ]?(?:[Ee]|-)(\d{1,4}))?")
_NXNN = re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)")
_SEASON_DIR = re.compile(r"^(?:season|temporada|série|serie|s)[ ._-]*(\d{1,3})$", re.I)
_YEAR = re.compile(r"[\(\[\.\s_-]((?:19|20)\d{2})[\)\]\.\s_-]|[\(\[]((?:19|20)\d{2})[\)\]]|[\.\s_-]((?:19|20)\d{2})$")
_NOISE = re.compile(
    r"\b(2160p|1080p|1080i|720p|480p|4k|uhd|hdr10?\+?|hdr|dv|dolby ?vision|bluray|blu-ray|bdrip|brrip|"
    r"web-?dl|webrip|web|hdtv|dvdrip|remux|x264|x265|h\.?264|h\.?265|hevc|avc|aac|ac3|dts(?:-hd)?|truehd|atmos|"
    r"ddp?5\.1|ddp?7\.1|10bit|proper|repack|extended|unrated|remastered|multi|dual|internal)\b.*$",
    re.I,
)


@dataclass
class MediaId:
    kind: str  # movie | episode
    title: str
    year: int | None = None
    series_title: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    episode_title: str | None = None


def clean_title(s: str) -> str:
    s = _NOISE.sub("", s)
    s = re.sub(r"[\._]+", " ", s)
    s = re.sub(r"\s+-\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -._[]()")
    return s


def _year_of(s: str) -> int | None:
    m = _YEAR.search(s)
    if m:
        y = int(next(g for g in m.groups() if g))
        if 1900 <= y <= 2100:
            return y
    return None


def parse_episode_tag(stem: str) -> tuple[int, int, int | None] | None:
    m = _SXXEXX.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
    m = _NXNN.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2)), None
    return None


def identify(path: Path, library_root: Path, library_type: str) -> MediaId:
    stem = path.stem
    rel_parts = path.relative_to(library_root).parts[:-1] if path.is_relative_to(library_root) else ()
    tag = parse_episode_tag(stem)
    if library_type == "series" or tag:
        season = episode = end = None
        if tag:
            season, episode, end = tag
        # série: pasta acima de "Season NN" (ou 1.ª pasta da biblioteca)
        series_title = None
        year = None
        for i in range(len(rel_parts) - 1, -1, -1):
            m = _SEASON_DIR.match(rel_parts[i].strip())
            if m:
                if season is None:
                    season = int(m.group(1))
                if i > 0:
                    series_title = rel_parts[i - 1]
                break
        if series_title is None and rel_parts:
            series_title = rel_parts[0] if not _SEASON_DIR.match(rel_parts[0]) else None
        if series_title is None:
            before = _SXXEXX.split(stem)[0] if tag else stem
            series_title = before
        year = _year_of(series_title)
        series_clean = clean_title(re.sub(r"[\(\[]?(?:19|20)\d{2}[\)\]]?\s*$", "", clean_title(series_title)))
        ep_title = None
        if tag:
            after = _SXXEXX.split(stem, maxsplit=1)[-1] if _SXXEXX.search(stem) else ""
            ep_title = clean_title(after) or None
        title = f"{series_clean} S{season:02d}E{episode:02d}" if season is not None and episode is not None else series_clean
        return MediaId("episode", title, year, series_clean or "Desconhecida", season, episode, end, ep_title)
    # filme: preferir nome da pasta quando contém ano, senão ficheiro
    year = _year_of(stem)
    base = stem
    if year is None and rel_parts:
        y2 = _year_of(rel_parts[-1])
        if y2:
            year = y2
            base = rel_parts[-1]
    title = clean_title(re.sub(r"[\(\[\.\s_-]*(?:19|20)\d{2}.*$", "", base)) if year else clean_title(base)
    return MediaId("movie", title or clean_title(stem), year)


# --- legendas ----------------------------------------------------------------

@dataclass
class SubName:
    lang: str | None
    forced: bool
    hi: bool
    stem_match: str  # parte do nome antes das etiquetas (para associar ao vídeo)


_HI_TOKENS = {"sdh", "hi", "cc", "hearing", "hearingimpaired"}
_FORCED_TOKENS = {"forced", "forçada", "forcada", "foreign"}


def parse_subtitle_name(sub_path: Path) -> SubName:
    """Extrai idioma/forced/SDH das etiquetas finais: `Nome.pt-PT.forced.srt`."""
    parts = sub_path.stem.split(".")
    lang = None
    forced = hi = False
    consumed = 0
    for tok in reversed(parts[1:]):
        t = tok.lower().strip("[]()")
        if t in _FORCED_TOKENS:
            forced = True
        elif t in _HI_TOKENS:
            hi = True
        elif (nl := normalize_lang(t)) and lang is None and len(t) <= 8 and (len(t) <= 3 or "-" in t or "_" in t or t in ("portuguese", "english", "spanish", "french", "german", "italian", "brazilian")):
            lang = nl
        elif t.isdigit() and len(t) <= 2:  # sufixo de duplicado: .2
            pass
        else:
            break
        consumed += 1
    keep = parts[: len(parts) - consumed] if consumed else parts
    return SubName(lang, forced, hi, ".".join(keep))
