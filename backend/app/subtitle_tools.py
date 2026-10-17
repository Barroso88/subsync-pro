"""Ferramentas de sincronização de legendas SRT: atrasar/adiantar, frame rate, dois pontos, referência.

Princípios:
- Só se alteram os TEMPOS: o texto (itálicos, posições, quebras de linha) fica byte a byte igual.
- Nada é escrito sem pré-visualização + confirmação; o original vai sempre para backup (restaurável).
- Só `.srt`, só em bibliotecas com escrita ativa, ficheiro normal (não atalho), escrita atómica.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as _stat
import time
import uuid
from bisect import bisect_left
from pathlib import Path
from typing import Callable

from .safepaths import PathSecurityError, require_within, sanitize_filename
from .subtitles.parser import MAX_SUB_BYTES, analyze_duration, decode_bytes, estimate_alignment, fmt_ms, parse_srt, ParsedSub, Cue

TS_LINE = re.compile(r"^(?P<a>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})(?P<mid>\s*-->\s*)(?P<b>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})(?P<rest>[^\n]*)$", re.M)
MAX_SHIFT_MS = 30 * 60 * 1000


class ToolError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def parse_time_ms(s: str | float | int) -> int:
    """Aceita `HH:MM:SS,mmm`, `MM:SS.mmm`, `SS,mmm` ou segundos decimais."""
    if isinstance(s, (int, float)):
        return int(round(float(s) * 1000))
    t = str(s).strip().replace(",", ".")
    if not t:
        raise ToolError("Tempo vazio.")
    parts = t.split(":")
    try:
        sec = float(parts[-1])
        mins = int(parts[-2]) if len(parts) > 1 else 0
        hrs = int(parts[-3]) if len(parts) > 2 else 0
    except ValueError:
        raise ToolError(f"Tempo inválido: {s}")
    return int(round(((hrs * 60 + mins) * 60 + sec) * 1000))


def _fmt(ms: int) -> str:
    ms = max(0, ms)
    s, m = divmod(ms, 1000)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{m:03d}"


def _to_ms(ts: str) -> int:
    h, m, rest = ts.split(":")
    sec, _, frac = rest.replace(",", ".").partition(".")
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + int(frac.ljust(3, "0")[:3])


def retime_srt(text: str, fn: Callable[[int], int]) -> tuple[str, dict]:
    """Aplica fn a cada tempo, preservando tudo o resto. Eventos que ficam totalmente antes de 0 são removidos."""
    parts = re.split(r"(\r?\n[ \t]*\r?\n)", text)
    out: list[str] = []
    stats = {"cues": 0, "dropped": 0, "clamped": 0}
    for i in range(0, len(parts), 2):
        block = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        m = TS_LINE.search(block)
        if not m:
            out.append(block + sep)
            continue
        stats["cues"] += 1
        a, b = fn(_to_ms(m["a"])), fn(_to_ms(m["b"]))
        if b <= 0:
            stats["dropped"] += 1
            continue
        if a < 0:
            stats["clamped"] += 1
        b = max(b, a + 1)
        line = f"{_fmt(a)}{m['mid']}{_fmt(b)}{m['rest']}"
        out.append(block[:m.start()] + line + block[m.end():] + sep)
    res = "".join(out)
    if stats["dropped"]:
        res = _renumber(res)
    return res, stats


def _renumber(text: str) -> str:
    parts = re.split(r"(\r?\n[ \t]*\r?\n)", text)
    n = 0
    for i in range(0, len(parts), 2):
        if TS_LINE.search(parts[i]):
            n += 1
            lines = parts[i].split("\n", 1)
            if re.fullmatch(r"\s*\d+\s*\r?", lines[0]) and len(lines) > 1:
                nl = "\r" if lines[0].endswith("\r") else ""
                parts[i] = f"{n}{nl}\n{lines[1]}"
    return "".join(parts)


# ---- cálculo das transformações ------------------------------------------------

def make_mapper(action: str, params: dict, cues: list[Cue], reference: ParsedSub | None = None) -> tuple[Callable[[int], int], dict]:
    """Devolve (função de transformação de tempos, descrição legível do que será feito)."""
    if action == "shift":
        delta = parse_time_ms(params.get("seconds", 0)) if isinstance(params.get("seconds"), str) else int(round(float(params.get("seconds", 0)) * 1000))
        if delta == 0:
            raise ToolError("O deslocamento é zero.")
        if abs(delta) > MAX_SHIFT_MS:
            raise ToolError("Deslocamento excessivo (máx. 30 minutos).")
        return (lambda t: t + delta), {"summary": f"Todas as falas {'atrasam' if delta > 0 else 'adiantam'} {abs(delta) / 1000:g} s.", "delta_ms": delta}
    if action == "fps":
        try:
            src, dst = float(params["from_fps"]), float(params["to_fps"])
        except (KeyError, ValueError, TypeError):
            raise ToolError("Indique o frame rate de origem e de destino.")
        if not (5 <= src <= 120 and 5 <= dst <= 120) or src == dst:
            raise ToolError("Frame rates inválidos ou iguais.")
        ratio = src / dst
        if not 0.8 <= ratio <= 1.25:
            raise ToolError("Conversão de frame rate fora do intervalo razoável.")
        return (lambda t: int(round(t * ratio))), {"summary": f"Converte de {src:g} para {dst:g} fps (tempos × {ratio:.6f}).", "ratio": ratio}
    if action == "twopoint":
        try:
            i1, i2 = int(params["cue1"]), int(params["cue2"])
            t1, t2 = parse_time_ms(params["time1"]), parse_time_ms(params["time2"])
        except (KeyError, ValueError, TypeError):
            raise ToolError("Indique as duas falas e quando devem aparecer.")
        if i1 == i2 or not (1 <= i1 <= len(cues)) or not (1 <= i2 <= len(cues)):
            raise ToolError("Escolha duas falas diferentes da legenda.")
        o1, o2 = cues[i1 - 1].start_ms, cues[i2 - 1].start_ms
        if o1 == o2:
            raise ToolError("As duas falas têm o mesmo tempo atual.")
        a = (t2 - t1) / (o2 - o1)
        b = t1 - a * o1
        if not 0.9 <= a <= 1.1:
            raise ToolError(f"Escala resultante ({a:.4f}) fora do intervalo razoável (0,9–1,1): confirme as falas e os tempos.")
        return (lambda t: int(round(a * t + b))), {"summary": f"Ajuste linear: tempos × {a:.6f} {b / 1000:+.3f} s (fala {i1} → {fmt_ms(t1)}, fala {i2} → {fmt_ms(t2)}).", "scale": a, "offset_ms": b}
    if action == "reference":
        if reference is None:
            raise ToolError("Escolha uma legenda de referência.")
        est = estimate_sync(cues, reference.cues)
        if not est["ok"]:
            raise ToolError("Não foi possível estimar o alinhamento: " + est["reason"])
        mode = params.get("mode", "offset")
        use_linear = mode == "linear" and bool(est.get("linear"))
        ratio = est["linear_ratio"] if use_linear else est["match_ratio"]
        if ratio < 0.4:
            raise ToolError(f"Correspondência com a referência demasiado fraca ({round(ratio * 100)} %): os cortes das falas são muito diferentes.")
        if use_linear:
            a, b = est["linear"]
            return (lambda t: int(round(a * t + b))), {"summary": f"Alinhamento linear com a referência (offset inicial {est['o1'] / 1000:+.2f} s, final {est['o3'] / 1000:+.2f} s).", "estimate": est}
        d = est["offset_ms"]
        if d == 0:
            raise ToolError("A legenda já está alinhada com a referência (offset ≈ 0).")
        return (lambda t: t + d), {"summary": f"Offset estimado de {d / 1000:+.2f} s face à referência.", "estimate": est}
    raise ToolError("Ação desconhecida.")


def estimate_sync(candidate: list[Cue], reference: list[Cue]) -> dict:
    return estimate_alignment(candidate, reference)


# ---- acesso seguro ao ficheiro --------------------------------------------------

def check_editable(path: str, writable_roots: list[str], *, allow_any_sub_ext: bool = False) -> str:
    try:
        real = require_within(path, writable_roots)
    except PathSecurityError:
        raise ToolError("O ficheiro não está numa biblioteca com escrita ativa.", 403)
    ext = os.path.splitext(path)[1].lower()
    if not allow_any_sub_ext and ext != ".srt":
        raise ToolError("Por agora só se editam legendas SRT.", 422)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ToolError("O ficheiro já não existe.", 404)
    if not _stat.S_ISREG(st.st_mode) or os.path.islink(path) or real != os.path.realpath(path):
        raise ToolError("Não é um ficheiro normal (atalho/pasta).", 422)
    if st.st_size == 0 or st.st_size > MAX_SUB_BYTES:
        raise ToolError("Ficheiro vazio ou demasiado grande.", 422)
    if not os.access(os.path.dirname(path), os.W_OK):
        raise ToolError("Sem permissão de escrita na pasta (UID/GID/montagem).", 403)
    return real


def encode_like(text: str, orig_bytes: bytes, enc: str) -> bytes:
    if enc == "utf-8":
        return text.encode("utf-8-sig" if orig_bytes.startswith(b"\xef\xbb\xbf") else "utf-8")
    if enc in ("cp1252", "latin-1"):
        try:
            return text.encode(enc)
        except UnicodeEncodeError:
            pass
    return text.encode("utf-8")


def atomic_replace(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{uuid.uuid4().hex[:8]}.subsync-tmp")
    try:
        mode = _stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o664
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode or 0o664)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def build_preview(orig_cues: list[Cue], fn: Callable[[int], int], video_duration: float | None, n: int = 6) -> dict:
    def row(i: int, c: Cue) -> dict:
        s, e = fn(c.start_ms), fn(c.end_ms)
        return {"n": i + 1, "old_start": c.start_ms, "old_end": c.end_ms, "new_start": s, "new_end": e, "text": c.text[:90]}
    rows = [row(i, c) for i, c in enumerate(orig_cues)]
    head, tail = rows[:n], rows[-n:] if len(rows) > n else []
    new_last = max((r["new_end"] for r in rows), default=0)
    warn = []
    neg = sum(1 for r in rows if r["new_end"] <= 0)
    clamp = sum(1 for r in rows if r["new_start"] < 0 < r["new_end"])
    if neg:
        warn.append(f"{neg} falas ficariam antes do início do vídeo e seriam removidas.")
    if clamp:
        warn.append(f"{clamp} falas ficariam parcialmente antes de 0 e começariam em 0.")
    after = None
    if video_duration:
        dur = video_duration * 1000
        if new_last > dur * 1.03 + 5000:
            warn.append(f"A última fala terminaria às {fmt_ms(new_last)}, depois do fim do vídeo ({fmt_ms(int(dur))}).")
        elif new_last < dur * 0.55 and dur > 20 * 60 * 1000:
            warn.append("A legenda terminaria muito antes do fim do vídeo (pode estar truncada).")
        after = "limites compatíveis com a duração do vídeo (não prova sincronização)" if not warn else "atenção aos avisos"
    return {"head": head, "tail": tail, "total": len(rows), "dropped": neg, "clamped": clamp, "warnings": warn,
            "first_start_before": rows[0]["old_start"] if rows else None, "first_start_after": rows[0]["new_start"] if rows else None,
            "last_end_before": max((r["old_end"] for r in rows), default=0), "last_end_after": new_last, "duration_check": after}
