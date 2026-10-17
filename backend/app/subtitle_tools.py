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


# =====================================================================================
# Fase 2 — limpeza de TEXTO (tempos e numeração intactos; só mudam as falas indicadas)
# =====================================================================================

STYLE_RE = re.compile(r"</?(?:i|b|u|s|em|strong|font|c|v|ruby|rt|span|lang)\b[^>]*>|\{\\[^}]*\}", re.I)
_BRACKETS = re.compile(r"\[[^\]\n]{0,80}\]")
_PAREN_START = re.compile(r"^\s*\([^)\n]{1,60}\)\s*")
_PAREN_END = re.compile(r"\s*\([^)\n]{1,60}\)\s*$")
_SPEAKER = re.compile(r"^[A-ZÀ-ÝÇ][A-ZÀ-ÝÇ0-9 .'’\-]{1,28}:\s+")           # «JOÃO: » só em maiúsculas
_MUSIC_ONLY = re.compile(r"^[\s♪♫♬#*]*$")
_WATERMARK = re.compile(r"opensubtitles|subscene|addic7ed|yifysubtitles|legendas?\s*(por|by|:)|subtitles?\s*(by|:)|sync(ed|hronized)?\s*(by|:)|"
                        r"resync|tradu[cç][aã]o\s*(por|:)|revis[aã]o\s*(por|:)|https?://|www\.|\.(com|org|net)\b", re.I)
_ACRONYM_OK = {"FBI", "CIA", "USA", "UK", "DVD", "TV", "NASA", "DNA", "NYPD", "LAPD", "UFO", "SOS", "VIP", "PC", "NSA", "DEA", "SWAT", "AIDS", "HIV", "GPS", "CEO", "SMS", "BBC", "CNN", "NYC"}
_NO_VOWEL = re.compile(r"^[B-DF-HJ-NP-TV-Z]{2,5}$")
OPTIONS = ("style", "hi", "common", "uppercase")


def strip_style(lines: list[str]) -> list[str]:
    return [STYLE_RE.sub("", l) for l in lines]


def remove_hi(lines: list[str]) -> list[str]:
    """Remove descrições para surdos: [ruído], (música) no início/fim, «NOME:» em maiúsculas e linhas só com ♪."""
    out: list[str] = []
    for l in lines:
        m = re.match(r"^(\s*[-–—]\s*)?(.*)$", l)
        dash, body = (m.group(1) or ""), m.group(2)
        body = _BRACKETS.sub("", body)
        for _ in range(3):
            body = _PAREN_START.sub("", body)
        body = _PAREN_END.sub("", body)
        body = _SPEAKER.sub("", body.strip())
        body = re.sub(r"[ \t]{2,}", " ", body).strip()
        if not body or _MUSIC_ONLY.match(body):
            continue
        out.append((dash.strip() + " " if dash else "") + body)
    if len(out) == 1 and len(lines) > 1 and re.match(r"^[-–—]\s+", out[0]):
        out[0] = re.sub(r"^[-–—]\s+", "", out[0])               # sobrou uma só fala: sem travessão
    return out


def common_fixes(lines: list[str], lang: str | None = None) -> list[str]:
    out = []
    for l in lines:
        l = l.replace(" ", " ")
        l = re.sub(r"[ \t]{2,}", " ", l).strip()
        l = re.sub(r"^[–—]\s*", "- ", l)
        l = re.sub(r"^-(?=[A-Za-zÀ-ÿ¿¡\"'♪])", "- ", l)
        if (lang or "")[:2] != "fr":
            l = re.sub(r"\s+([,.;:!?])", r"\1", l)
        l = re.sub(r"([,;!?])(?=[A-Za-zÀ-ÿ])", r"\1 ", l)
        l = re.sub(r"(?<=[a-zà-ÿ]{3})\.(?=[A-ZÀ-Ý][a-zà-ÿ])", ". ", l)
        l = l.replace("…", "...")
        l = re.sub(r"(?<![.])\.\.(?![.])", "...", l)
        if l.strip():
            out.append(l)
    return out


def fix_uppercase(lines: list[str], lang: str | None = None) -> list[str]:
    text = " ".join(lines)
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8 or len(text.split()) < 2 or sum(c.isupper() for c in letters) / len(letters) < 0.9:
        return lines                                              # não é uma fala toda em maiúsculas
    out = []
    for l in lines:
        words = re.split(r"(\s+)", l)
        conv = []
        for w in words:
            core = re.sub(r"[^A-Za-zÀ-ÿ]", "", w)
            if core and (core in _ACRONYM_OK or _NO_VOWEL.match(core)):
                conv.append(w)                                     # siglas (FBI, DVD...) ficam
            else:
                conv.append(w.lower())
        s = "".join(conv)
        s = re.sub(r"(^|[.!?…]\s+|^[-–—]\s+)([a-zà-ÿ])", lambda m: m.group(1) + m.group(2).upper(), s)
        s = re.sub(r"^(\W*)([a-zà-ÿ])", lambda m: m.group(1) + m.group(2).upper(), s)
        if (lang or "")[:2] == "en":
            s = re.sub(r"\bi\b(?=$|[\s'’,.!?])", "I", s)
        out.append(s)
    return out


def transform_srt_text(text: str, opts: dict, lang: str | None = None, preview_limit: int = 40) -> tuple[str, dict, list[dict]]:
    """Aplica as opções ativas a cada fala. Devolve (texto novo, estatísticas, alterações para pré-visualizar)."""
    active = [o for o in OPTIONS if opts.get(o)]
    if not active:
        raise ToolError("Escolha pelo menos uma opção.")
    crlf = "\r\n" in text
    src = text.replace("\r\n", "\n")
    parts = re.split(r"(\n[ \t]*\n)", src)
    blocks: list[dict] = []
    for i in range(0, len(parts), 2):
        block, sep = parts[i], (parts[i + 1] if i + 1 < len(parts) else "")
        m = TS_LINE.search(block)
        if not m:
            blocks.append({"raw": block + sep, "cue": False})
            continue
        head = block[:m.end()]
        body = block[m.end():].lstrip("\n")
        core = body.rstrip("\n")
        tail = body[len(core):]                                   # \n final do ficheiro (só na última fala)
        blocks.append({"cue": True, "head": head, "sep": sep, "tail": tail, "lines": core.split("\n") if core else [], "orig": core.split("\n") if core else []})
    cue_blocks = [b for b in blocks if b["cue"]]
    stats = {"cues": len(cue_blocks), "changed": 0, "dropped": 0, "style": 0, "hi": 0, "common": 0, "uppercase": 0, "watermark": 0}
    changes: list[dict] = []
    dropped_pos: list[int] = []
    for pos, b in enumerate(cue_blocks, 1):
        lines = list(b["orig"])
        marks = []
        for name, fn in (("style", strip_style), ("hi", remove_hi), ("common", lambda x: common_fixes(x, lang)), ("uppercase", lambda x: fix_uppercase(x, lang))):
            if name in active:
                new = fn(lines)
                if new != lines:
                    stats[name] += 1
                    marks.append(name)
                lines = new
        if "common" in active and (pos <= 3 or pos > len(cue_blocks) - 3) and lines and len(" ".join(lines)) < 140 and _WATERMARK.search(" ".join(lines)):
            stats["watermark"] += 1
            marks.append("watermark")
            lines = []
        b["lines"] = lines
        if lines != b["orig"]:
            stats["changed"] += 1
            if not lines:
                stats["dropped"] += 1
                dropped_pos.append(pos)
            if len(changes) < preview_limit:
                changes.append({"n": pos, "before": " ⏎ ".join(b["orig"])[:160], "after": " ⏎ ".join(lines)[:160] if lines else "(fala removida)", "tags": marks})
    out = []
    for b in blocks:
        if not b["cue"]:
            out.append(b["raw"])
        elif b["lines"]:
            out.append(b["head"] + "\n" + "\n".join(b["lines"]) + b["tail"] + b["sep"])
        # falas ficam vazias → removidas (inclui o separador)
    res = "".join(out)
    if stats["dropped"]:
        res = _renumber(res)
    stats["dropped_positions"] = dropped_pos
    if crlf:
        res = res.replace("\n", "\r\n")
    return res, stats, changes


# ---- Fase 3: codificação (UTF-8) e conversão ASS/SSA/VTT → SRT ----------------------------

TEXT_EXTS = (".srt", ".ass", ".ssa", ".vtt")
_MOJI = re.compile("[ÃÂ][\u0080-¿ŒœŠšŸŽžƒˆ˜–—‘-„†-•…‰‹›€™]")


def _cp1252_bytes(line: str) -> bytes | None:
    out = bytearray()
    for ch in line:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) < 256:
                out.append(ord(ch))
            else:
                return None
    return bytes(out)


def fix_mojibake(text: str) -> tuple[str, int]:
    """Repara «Ã£», «Ã©» etc. (UTF-8 lido como cp1252), linha a linha e só quando o resultado é UTF-8 válido."""
    fixed = 0
    out = []
    for line in text.split("\n"):
        if _MOJI.search(line):
            b = _cp1252_bytes(line)
            if b is not None:
                try:
                    new = b.decode("utf-8")
                    if not _MOJI.search(new):
                        out.append(new)
                        fixed += 1
                        continue
                except UnicodeDecodeError:
                    pass
        out.append(line)
    return "\n".join(out), fixed


def _ass_text_to_srt(t: str) -> str:
    t = t.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    def repl(m: re.Match) -> str:
        inner, out = m.group(1), ""
        for tag, val in re.findall(r"\\([ibu])([01])", inner):
            out += f"<{tag}>" if val == "1" else f"</{tag}>"
        return out
    t = re.sub(r"\{([^}]*)\}", repl, t)
    return t


def _ass_cues(text: str) -> list[tuple[int, int, str]]:
    fields: list[str] = []
    in_events = False
    out = []
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
                continue
            d = dict(zip(fields, parts))
            try:
                s, e = _ass_ms(d["start"]), _ass_ms(d["end"])
            except (KeyError, ValueError):
                continue
            raw = d.get("text", "")
            if re.search(r"\{[^}]*\\p[1-9]", raw):        # desenhos vetoriais (karaoke/efeitos), não são texto
                out.append((s, e, ""))
                continue
            out.append((s, e, _ass_text_to_srt(raw).strip()))
    return out


def _ass_ms(s: str) -> int:
    m = re.match(r"(\d+):(\d{2}):(\d{2})[.](\d{1,2})", s.strip())
    if not m:
        raise ValueError(s)
    return ((int(m.group(1)) * 60 + int(m.group(2))) * 60 + int(m.group(3))) * 1000 + int(m.group(4).ljust(2, "0")[:2]) * 10


_VTT_ARROW = re.compile(r"^\s*((?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})")


def _vtt_ms(s: str) -> int:
    parts = s.strip().replace(",", ".").split(":")
    sec = float(parts[-1])
    mins = int(parts[-2])
    hrs = int(parts[-3]) if len(parts) > 2 else 0
    return int(round(((hrs * 60 + mins) * 60 + sec) * 1000))


def _vtt_clean(t: str) -> str:
    t = re.sub(r"<\d{1,3}:\d{2}[:.]\d{2,3}(?:[.]\d{1,3})?>", "", t)            # timestamps inline (karaoke)
    t = re.sub(r"<(/?)([ibu])>", r"<\1\2>", t)
    t = re.sub(r"</?(?:c|v|lang|ruby|rt|rp)(?:[.\s][^>]*)?>", "", t)
    t = t.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return t.strip()


def _vtt_cues(text: str) -> list[tuple[int, int, str]]:
    out = []
    for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")):
        lines = b.split("\n")
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        for i, ln in enumerate(lines[:2]):                                   # linha 0 pode ser o identificador do cue
            m = _VTT_ARROW.match(ln)
            if m:
                try:
                    out.append((_vtt_ms(m.group(1)), _vtt_ms(m.group(2)), _vtt_clean("\n".join(lines[i + 1:]))))
                except ValueError:
                    pass
                break
    return out


def _srt_cues_raw(text: str) -> list[tuple[int, int, str]]:
    out = []
    for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")):
        m = TS_LINE.search(b)
        if m:
            out.append((_to_ms(m.group("a")), _to_ms(m.group("b")), b[m.end():].strip("\n")))
    return out


def cues_to_srt(cues: list[tuple[int, int, str]], crlf: bool = False) -> str:
    rows = []
    for s, e, t in cues:
        if t.strip() and e >= s:
            rows.append((s, e, t))
    rows.sort(key=lambda c: c[0])
    blocks = [f"{i}\n{_fmt(s)} --> {_fmt(e)}\n{t}\n" for i, (s, e, t) in enumerate(rows, 1)]
    res = "\n".join(blocks)
    return res.replace("\n", "\r\n") if crlf else res


def plan_convert(data: bytes, ext: str, opts: dict | None = None) -> dict:
    """Prepara a conversão para SRT em UTF-8 (sem BOM). Não escreve nada.
    Devolve texto novo, descrição das alterações e a lista de (início, fim) esperada, para verificação."""
    opts = opts or {}
    ext = ext.lower()
    if ext not in TEXT_EXTS:
        raise ToolError("Só legendas de texto (SRT, ASS, SSA, VTT) podem ser convertidas.", 422)
    from .subtitles.parser import sniff_format
    text, enc = decode_bytes(data)
    bom = data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
    replacement = text.count("�")
    fmt = sniff_format(text, ext) or ext.lstrip(".")
    if fmt not in ("srt", "vtt", "ass", "ssa"):
        raise ToolError("Formato não reconhecido.", 422)
    moji = 0
    if opts.get("fix_mojibake", True):
        text, moji = fix_mojibake(text.replace("\r\n", "\n"))
    crlf = b"\r\n" in data
    if fmt in ("ass", "ssa"):
        cues = _ass_cues(text)
    elif fmt == "vtt":
        cues = _vtt_cues(text)
    else:
        cues = _srt_cues_raw(text)
    kept = [(s, e, t) for s, e, t in cues if t.strip() and e >= s]
    if not kept:
        raise ToolError("Não foi possível extrair falas desta legenda.", 422)
    if fmt == "srt":
        new_text = text.replace("\n", "\r\n") if crlf else text
        if not new_text.endswith("\n"):
            new_text += "\r\n" if crlf else "\n"
    else:
        new_text = cues_to_srt(kept, crlf)
    changes = []
    if fmt != "srt":
        changes.append(f"{fmt.upper()} → SRT")
    if enc != "utf-8" or bom:
        changes.append(f"codificação {enc}{' com BOM' if bom else ''} → UTF-8 sem BOM")
    if moji:
        changes.append(f"{moji} linhas com acentos partidos reparadas")
    if fmt == "srt" and not changes:
        raise ToolError("Nada a alterar: já é SRT em UTF-8 sem BOM e sem acentos partidos.", 422)
    warnings = []
    if replacement:
        warnings.append(f"Contém {replacement} caracteres inválidos (�) que não se podem recuperar; é preciso uma legenda nova.")
    if fmt != "srt":
        if len(cues) != len(kept):
            warnings.append(f"{len(cues) - len(kept)} eventos sem texto (efeitos/desenhos) não passam para o SRT.")
        warnings.append("Posições e estilos (cores, fontes, \\pos) perdem-se; ficam só itálico, negrito e sublinhado.")
    return {"fmt": fmt, "enc": enc, "bom": bom, "new_text": new_text, "expected": [(s, e) for s, e, _ in sorted(kept, key=lambda c: c[0])] if fmt != "srt" else [(s, e) for s, e, _ in cues],
            "cues": len(kept), "dropped": len(cues) - len(kept), "moji": moji, "changes": changes, "warnings": warnings,
            "samples": [{"n": i + 1, "start": s, "end": e, "text": t[:90]} for i, (s, e, t) in enumerate(sorted(kept, key=lambda c: c[0])[:8])]}


def convert_target(path: str, fmt: str) -> str:
    """Caminho de saída: mesmo nome com extensão .srt (mantém o código de idioma do nome)."""
    return os.path.splitext(path)[0] + ".srt"
