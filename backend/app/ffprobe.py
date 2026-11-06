"""Wrapper de FFprobe: argumentos fixos em lista (sem shell), caminho absoluto, timeout."""
from __future__ import annotations

import json
import os
import re
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
            lang = normalize_lang(tags.get("language")) or _lang_from_title(title)
            if lang == "pt" and _lang_from_title(title) in ("pt-PT", "pt-BR"):
                lang = _lang_from_title(title)           # «por» + título «Brazilian» → pt-BR
            res.subs.append(EmbeddedSub(
                index=int(st.get("index", 0)), codec=codec,
                lang=lang,
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
    if re.search(r"brazil|brasil|pt[-_ ]?br|\bptbr\b|\(br\)|\bpob\b", t):
        return "pt-BR"
    if re.search(r"portugal|pt[-_ ]?pt|european|europeu|\(pt\)|\beuro", t):
        return "pt-PT"
    return normalize_lang(t.split()[0]) if t.split() else None


def extract_subtitle_text(path: str, index: int, ffmpeg_bin: str = "ffmpeg", timeout: int = 300) -> bytes | None:
    """Extrai uma faixa de legenda de texto como SRT (stdout). Só leitura; sem shell; índice validado."""
    if not os.path.isabs(path) or not isinstance(index, int) or index < 0:
        return None
    cmd = [ffmpeg_bin, "-v", "error", "-nostdin", "-i", path, "-map", f"0:{index}", "-f", "srt", "-"]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return cp.stdout if cp.returncode == 0 and cp.stdout else None


def extract_subtitle_windows(path: str, index: int, windows: list[tuple[float, float]], ffmpeg_bin: str = "ffmpeg", timeout: int = 90) -> bytes | None:
    """Extrai só alguns TRECHOS de uma faixa de legenda de texto (início, duração em s) como um SRT com os tempos ORIGINAIS do filme.
    Procura no ficheiro (-ss antes do -i, com -copyts) em vez de o ler todo: muito mais rápido em ficheiros grandes. Só leitura; sem shell."""
    if not os.path.isabs(path) or not isinstance(index, int) or index < 0 or not windows:
        return None
    from .subtitles.parser import parse_srt
    seen: set[tuple[int, int, str]] = set()
    cues: list[tuple[int, int, str]] = []
    for start, dur in windows:
        cmd = [ffmpeg_bin, "-v", "error", "-nostdin", "-ss", f"{max(0.0, start):.2f}", "-copyts", "-i", path, "-to", f"{start + dur:.2f}", "-map", f"0:{index}", "-f", "srt", "-"]
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if not cp.stdout:
            continue
        for c in parse_srt(cp.stdout.decode("utf-8", "ignore")).cues:
            if start * 1000 <= c.start_ms < (start + dur) * 1000 and (c.start_ms, c.end_ms, c.text) not in seen:
                seen.add((c.start_ms, c.end_ms, c.text))
                cues.append((c.start_ms, c.end_ms, c.text))
    if not cues:
        return None
    cues.sort()

    def ts(ms: int) -> str:
        s_, ms_ = divmod(ms, 1000)
        return f"{s_ // 3600:02d}:{(s_ % 3600) // 60:02d}:{s_ % 60:02d},{ms_:03d}"
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)).encode("utf-8")
