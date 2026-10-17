"""Parsing e análise de legendas de texto: SRT, VTT, ASS/SSA."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_SUB_BYTES = 8 * 1024 * 1024


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class ParsedSub:
    format: str
    cues: list[Cue] = field(default_factory=list)
    errors: int = 0          # blocos mal formados
    encoding: str = "utf-8"

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.cues)


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Descodifica tentando BOMs e codificações comuns. Devolve (texto, encoding)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Latin-1 cobre tudo; cp1252 é o mais provável para legendas ocidentais antigas.
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


_TIME = re.compile(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")
_SRT_ARROW = re.compile(
    r"^\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})", re.M
)
_VTT_ARROW = re.compile(
    r"^\s*((?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})", re.M
)
_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _t(s: str) -> int:
    m = _TIME.match(s.strip())
    if not m:
        raise ValueError(s)
    h = int(m.group(1) or 0)
    ms = int(m.group(4).ljust(3, "0")[:3])
    return ((h * 60 + int(m.group(2))) * 60 + int(m.group(3))) * 1000 + ms


def sniff_format(text: str, filename_ext: str = "") -> str | None:
    head = text.lstrip("﻿ \r\n\t")[:2000]
    if head.upper().startswith("WEBVTT"):
        return "vtt"
    if re.search(r"^\[Script Info\]", head, re.I | re.M) or re.search(r"^\[V4\+? Styles\]", text[:5000], re.I | re.M):
        return "ass" if filename_ext != ".ssa" else "ssa"
    if _SRT_ARROW.search(text[:20000]):
        return "srt"
    return None


def looks_like_html_error(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<body")) or b"<html" in head[:512]


def parse_srt(text: str) -> ParsedSub:
    out = ParsedSub("srt")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip("\n"))
    for b in blocks:
        m = _SRT_ARROW.search(b)
        if not m:
            if b.strip():
                out.errors += 1
            continue
        try:
            s, e = _t(m.group(1)), _t(m.group(2))
        except ValueError:
            out.errors += 1
            continue
        body = b[m.end():].split("\n", 1)
        # o resto da linha da seta pode ter coordenadas; o texto está nas linhas seguintes
        content = body[1] if len(body) > 1 else ""
        t = _TAGS.sub("", content).strip()
        if e < s:
            out.errors += 1
            continue
        out.cues.append(Cue(s, e, t))
    return out


def parse_vtt(text: str) -> ParsedSub:
    out = ParsedSub("vtt")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for b in re.split(r"\n\s*\n", text.strip("\n")):
        if b.strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        m = _VTT_ARROW.search(b)
        if not m:
            if b.strip():
                out.errors += 1
            continue
        try:
            s, e = _t(m.group(1)), _t(m.group(2))
        except ValueError:
            out.errors += 1
            continue
        lines = b[m.end():].split("\n", 1)
        content = lines[1] if len(lines) > 1 else ""
        out.cues.append(Cue(s, e, _TAGS.sub("", content).strip()))
    return out


_ASS_T = re.compile(r"(\d+):(\d{2}):(\d{2})[.](\d{1,2})")


def _ass_t(s: str) -> int:
    m = _ASS_T.match(s.strip())
    if not m:
        raise ValueError(s)
    return ((int(m.group(1)) * 60 + int(m.group(2))) * 60 + int(m.group(3))) * 1000 + int(m.group(4).ljust(2, "0")[:2]) * 10


def parse_ass(text: str, fmt: str = "ass") -> ParsedSub:
    out = ParsedSub(fmt)
    fields: list[str] = []
    in_events = False
    for line in text.replace("\r\n", "\n").split("\n"):
        ls = line.strip()
        if ls.startswith("["):
            in_events = ls.lower() == "[events]"
            continue
        if not in_events:
            continue
        if ls.lower().startswith("format:"):
            fields = [f.strip().lower() for f in ls[7:].split(",")]
        elif ls.lower().startswith("dialogue:") and fields:
            parts = ls[9:].lstrip().split(",", len(fields) - 1)
            if len(parts) < len(fields):
                out.errors += 1
                continue
            d = dict(zip(fields, parts))
            try:
                s, e = _ass_t(d["start"]), _ass_t(d["end"])
            except (KeyError, ValueError):
                out.errors += 1
                continue
            txt = re.sub(r"\{[^}]*\}", "", d.get("text", "")).replace("\\N", "\n").replace("\\n", "\n").strip()
            out.cues.append(Cue(s, e, txt))
    out.cues.sort(key=lambda c: c.start_ms)
    return out


def parse_text(text: str, ext: str = "") -> ParsedSub | None:
    fmt = sniff_format(text, ext)
    if fmt == "srt":
        return parse_srt(text)
    if fmt == "vtt":
        return parse_vtt(text)
    if fmt in ("ass", "ssa"):
        return parse_ass(text, fmt)
    return None


def parse_bytes(data: bytes, ext: str = "") -> ParsedSub | None:
    if len(data) > MAX_SUB_BYTES:
        return None
    text, enc = decode_bytes(data)
    p = parse_text(text, ext)
    if p:
        p.encoding = enc
    return p


# --- análise -----------------------------------------------------------------

@dataclass
class Issue:
    code: str
    severity: str  # info | warning | error
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class Analysis:
    status: str  # ok | empty | invalid
    cue_count: int
    first_start_ms: int | None
    last_end_ms: int | None
    issues: list[Issue]
    sync_status: str  # unchecked | plausible | suspect
    sync_detail: str | None


def analyze_duration(parsed: ParsedSub, video_duration: float | None) -> tuple[list[Issue], str, str | None]:
    """Compara limites temporais da legenda com a duração do vídeo.

    Isto NÃO prova sincronização: só deteta incompatibilidades grosseiras.
    `plausible` significa "nada de errado nos limites", não "sincronizada".
    """
    issues: list[Issue] = []
    if not parsed.cues:
        return issues, "unchecked", None
    last = max(c.end_ms for c in parsed.cues)
    first = min(c.start_ms for c in parsed.cues)
    if video_duration is None or video_duration <= 0:
        return issues, "unchecked", "Duração do vídeo desconhecida (sem FFprobe?)"
    dur_ms = video_duration * 1000
    if last > dur_ms * 1.03 + 5000:
        issues.append(Issue("beyond_video", "warning",
                            f"A legenda termina {fmt_ms(last)}, depois do fim do vídeo ({fmt_ms(int(dur_ms))})."))
        return issues, "suspect", "Eventos para lá da duração do vídeo"
    if last < dur_ms * 0.55 and dur_ms > 20 * 60 * 1000:
        issues.append(Issue("truncated", "warning",
                            f"A legenda termina aos {fmt_ms(last)} mas o vídeo dura {fmt_ms(int(dur_ms))}: pode estar truncada."))
        return issues, "suspect", "Legenda aparentemente truncada"
    if first > dur_ms * 0.25:
        issues.append(Issue("late_start", "info", f"A primeira fala só aparece aos {fmt_ms(first)}."))
    return issues, "plausible", "Limites temporais compatíveis (não prova sincronização)"


def fmt_ms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def analyze(parsed: ParsedSub | None, video_duration: float | None, *, size: int | None = None) -> Analysis:
    if size == 0:
        return Analysis("empty", 0, None, None, [Issue("empty", "error", "Ficheiro vazio.")], "unchecked", None)
    if parsed is None:
        return Analysis("invalid", 0, None, None,
                        [Issue("invalid_format", "error", "Formato não reconhecido ou ilegível.")], "unchecked", None)
    issues: list[Issue] = []
    if not parsed.cues:
        return Analysis("empty", 0, None, None, [Issue("no_cues", "error", "Sem eventos de legenda.")], "unchecked", None)
    if parsed.errors and parsed.errors > max(3, len(parsed.cues) * 0.05):
        issues.append(Issue("malformed_blocks", "warning", f"{parsed.errors} blocos mal formados."))
    if any(c.end_ms - c.start_ms <= 0 for c in parsed.cues[:2000]):
        issues.append(Issue("zero_duration", "info", "Existem eventos com duração zero."))
    # ordem temporal
    prev = -1
    disorder = 0
    for c in parsed.cues:
        if c.start_ms < prev - 1000:
            disorder += 1
        prev = max(prev, c.start_ms)
    if disorder > max(2, len(parsed.cues) * 0.02):
        issues.append(Issue("out_of_order", "warning", "Eventos fora de ordem temporal."))
    d_issues, sync, detail = analyze_duration(parsed, video_duration)
    issues += d_issues
    return Analysis("ok", len(parsed.cues), min(c.start_ms for c in parsed.cues),
                    max(c.end_ms for c in parsed.cues), issues, sync, detail)


# --- alinhamento com legenda de referência -------------------------------------

@dataclass
class AlignResult:
    ok: bool
    offset_ms: int = 0
    match_ratio: float = 0.0     # fração de eventos com par a ≤1,5 s após corrigir offset
    drift_ms: int = 0            # diferença de offset entre início e fim
    compared: int = 0
    reason: str = ""


def align_with_reference(candidate: list[Cue], reference: list[Cue]) -> AlignResult:
    """Compara os instantes de início das falas com os de uma legenda de referência
    (idioma diferente). Estima offset global e deriva.

    Limitação: assume que a referência está sincronizada com o vídeo; não usa áudio.
    Em legendas de idiomas diferentes os cortes de fala nem sempre coincidem, por isso
    o resultado é indicativo e só é "forte" quando a fração de pares é alta.
    """
    from bisect import bisect_left

    a = sorted(c.start_ms for c in candidate if c.text)
    b = sorted(c.start_ms for c in reference if c.text)
    if len(a) < 30 or len(b) < 30:
        return AlignResult(False, reason="Poucos eventos para comparar")

    def nearest(sorted_list: list[int], x: int) -> int:
        i = bisect_left(sorted_list, x)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(sorted_list):
                d = sorted_list[j] - x
                if best is None or abs(d) < abs(best):
                    best = d
        return best if best is not None else 10**9

    diffs = [nearest(b, x) for x in a]
    # moda dos desvios (bins de 250 ms) numa janela de ±120 s
    bins: dict[int, int] = {}
    for d in diffs:
        if abs(d) <= 120_000:
            bins[round(d / 250)] = bins.get(round(d / 250), 0) + 1
    if not bins:
        return AlignResult(False, reason="Sem sobreposição temporal com a referência")
    mode_bin = max(bins, key=lambda k: bins[k] + bins.get(k - 1, 0) * 0.5 + bins.get(k + 1, 0) * 0.5)
    offset = mode_bin * 250
    # desvio residual depois de aplicar o offset estimado (referência ≈ candidata + offset)
    resid = [nearest(b, x + offset) for x in a]
    good = [i for i, r in enumerate(resid) if abs(r) <= 1500]
    ratio = len(good) / len(a)

    def seg_offset(lo: int, hi: int) -> int | None:
        seg = [nearest(b, x) for x in a[lo:hi]]
        seg = [d for d in seg if abs(d) <= 120_000]
        if len(seg) < 10:
            return None
        bs: dict[int, int] = {}
        for d in seg:
            bs[round(d / 250)] = bs.get(round(d / 250), 0) + 1
        return max(bs, key=bs.get) * 250

    n = len(a)
    o1, o3 = seg_offset(0, n // 3), seg_offset(2 * n // 3, n)
    drift = (o3 - o1) if (o1 is not None and o3 is not None) else 0
    return AlignResult(True, offset, round(ratio, 3), drift, len(a))
