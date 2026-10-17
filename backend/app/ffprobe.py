"""Wrapper de FFprobe: argumentos fixos em lista (sem shell), caminho absoluto, timeout."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction

from .languages import normalize_lang

IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "pgssub", "dvbsub"}


@dataclass
class EmbeddedSub:
    index: int
    codec: str
    lang: str | None
    raw_lang: str | None
    title: str | None
    forced: bool
    hi: bool
    text_based: bool


@dataclass
class ProbeResult:
    ok: bool
    duration: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    subs: list[EmbeddedSub] = field(default_factory=list)
    error: str | None = None


def _fps(s: str | None) -> float | None:
    try:
        f = float(Fraction(s)) if s and s != "0/0" else None
        return round(f, 3) if f else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: str, ffprobe_bin: str = "ffprobe", timeout: int = 60) -> ProbeResult:
    if not os.path.isabs(path):
        return ProbeResult(False, error="Caminho não absoluto")
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", path]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return ProbeResult(False, error="ffprobe não encontrado")
    except subprocess.TimeoutExpired:
        return ProbeResult(False, error="ffprobe excedeu o tempo limite")
    if cp.returncode != 0:
        return ProbeResult(False, error=cp.stderr.decode("utf-8", "replace")[:300] or "ffprobe falhou")
    return parse_probe_json(cp.stdout)


def parse_probe_json(raw: bytes | str) -> ProbeResult:
    try:
        j = json.loads(raw)
    except ValueError:
        return ProbeResult(False, error="Saída do ffprobe inválida")
    res = ProbeResult(True)
    try:
        res.duration = float(j.get("format", {}).get("duration")) if j.get("format", {}).get("duration") else None
    except ValueError:
        res.duration = None
    for st in j.get("streams", []):
        ct = st.get("codec_type")
        if ct == "video" and res.video_codec is None and st.get("disposition", {}).get("attached_pic") != 1:
            res.video_codec = st.get("codec_name")
            res.width, res.height = st.get("width"), st.get("height")
            res.fps = _fps(st.get("avg_frame_rate")) or _fps(st.get("r_frame_rate"))
            if res.duration is None and st.get("duration"):
                try:
                    res.duration = float(st["duration"])
                except ValueError:
                    pass
        elif ct == "subtitle":
            tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
            disp = st.get("disposition") or {}
            title = tags.get("title")
            tl = (title or "").lower()
            codec = st.get("codec_name") or "?"
            res.subs.append(EmbeddedSub(
                index=int(st.get("index", 0)), codec=codec,
                lang=normalize_lang(tags.get("language")) or _lang_from_title(title),
                raw_lang=tags.get("language"), title=title,
                forced=bool(disp.get("forced")) or "forced" in tl or "forçada" in tl,
                hi=bool(disp.get("hearing_impaired")) or "sdh" in tl or "hearing" in tl,
                text_based=codec not in IMAGE_SUB_CODECS,
            ))
    return res


def _lang_from_title(title: str | None) -> str | None:
    if not title:
        return None
    t = title.lower()
    if "brazil" in t or "pt-br" in t:
        return "pt-BR"
    if "portugal" in t or "pt-pt" in t or "european" in t:
        return "pt-PT"
    return normalize_lang(t.split()[0]) if t.split() else None
