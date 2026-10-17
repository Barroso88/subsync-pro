"""Tradução automática de legendas com a API do DeepL.

Só o texto é traduzido; os tempos de cada evento são mantidos exatamente.
O resultado é SEMPRE assinalado como tradução automática. O texto das legendas
é enviado ao DeepL (terceiro): isto está documentado no README.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .subtitles.parser import Cue, ParsedSub

# código interno -> código de destino do DeepL
TARGETS = {"pt-PT": "PT-PT", "pt-BR": "PT-BR", "es": "ES", "fr": "FR", "de": "DE", "it": "IT", "nl": "NL"}
SOURCES = {"en": "EN", "es": "ES", "fr": "FR", "de": "DE", "it": "IT", "nl": "NL", "pt": "PT", "pt-PT": "PT", "pt-BR": "PT"}
MAX_ITEMS = 50           # limite do DeepL por pedido
MAX_BODY_CHARS = 60_000  # margem abaixo de 128 KiB
LINE_WIDTH = 42


class DeepLError(Exception):
    def __init__(self, message: str, *, auth: bool = False, quota: bool = False):
        super().__init__(message)
        self.auth, self.quota = auth, quota


@dataclass
class Usage:
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class DeepL:
    def __init__(self, key: str, transport: httpx.BaseTransport | None = None):
        self.key = key
        self.base = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=60, follow_redirects=False, transport=self.transport,
                            headers={"Authorization": f"DeepL-Auth-Key {self.key}", "User-Agent": "SubSyncPro/1.0"})

    def _check(self, r: httpx.Response) -> None:
        if r.status_code in (401, 403):
            raise DeepLError("O DeepL recusou a API key.", auth=True)
        if r.status_code == 456:
            raise DeepLError("Quota de caracteres do DeepL esgotada.", quota=True)
        if r.status_code == 429:
            raise DeepLError("DeepL: demasiados pedidos.")
        if r.status_code >= 400:
            raise DeepLError(f"DeepL respondeu HTTP {r.status_code}.")

    def usage(self) -> Usage:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/v2/usage")
        except httpx.HTTPError as e:
            raise DeepLError(f"Sem ligação ao DeepL: {type(e).__name__}") from e
        self._check(r)
        j = r.json()
        return Usage(int(j.get("character_count", 0)), int(j.get("character_limit", 0)))

    def translate(self, texts: list[str], target: str, source: str | None = None,
                  progress: Callable[[int, int], None] | None = None) -> list[str]:
        if target not in TARGETS:
            raise DeepLError(f"Idioma de destino não suportado: {target}")
        out: list[str] = []
        batches: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for t in texts:
            if cur and (len(cur) >= MAX_ITEMS or size + len(t) > MAX_BODY_CHARS):
                batches.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += len(t)
        if cur:
            batches.append(cur)
        with self._client() as c:
            for i, b in enumerate(batches):
                body: dict = {"text": b, "target_lang": TARGETS[target], "split_sentences": "nonewlines",
                              "preserve_formatting": True}
                if source and source in SOURCES and SOURCES[source] != "PT":
                    body["source_lang"] = SOURCES[source]
                for attempt in range(4):
                    try:
                        r = c.post(f"{self.base}/v2/translate", json=body)
                    except httpx.HTTPError as e:
                        if attempt == 3:
                            raise DeepLError(f"Sem ligação ao DeepL: {type(e).__name__}") from e
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    if r.status_code in (429, 503) and attempt < 3:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    break
                self._check(r)
                tr = r.json().get("translations", [])
                if len(tr) != len(b):
                    raise DeepLError("Resposta do DeepL com número de textos inesperado.")
                out += [t.get("text", "") for t in tr]
                if progress:
                    progress(i + 1, len(batches))
        return out


# ---- segmentação das falas ------------------------------------------------------

_DASH = re.compile(r"^\s*[-–—]\s*")
_NOLETTER = re.compile(r"^[\W\d_]*$", re.UNICODE)


def split_segments(text: str) -> tuple[list[str], bool]:
    """Divide a fala em segmentos a traduzir. Diálogo com travessões = um segmento por linha."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) > 1 and all(_DASH.match(l) for l in lines):
        return [_DASH.sub("", l) for l in lines], True
    return [" ".join(lines)], False


def rewrap(text: str, width: int = LINE_WIDTH) -> str:
    """Quebra em 2 linhas equilibradas quando é demasiado longa (convenção de legendas)."""
    if len(text) <= width or " " not in text:
        return text
    words = text.split(" ")
    best, best_diff = None, 10**9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(a) - len(b))
        if diff < best_diff:
            best, best_diff = (a, b), diff
    return f"{best[0]}\n{best[1]}" if best else text


def translatable(text: str) -> bool:
    return bool(text.strip()) and not _NOLETTER.match(text)


def translate_cues(cues: list[Cue], target: str, source: str | None, deepl: DeepL,
                   progress: Callable[[int, int], None] | None = None) -> tuple[list[Cue], int]:
    """Devolve (eventos traduzidos com os MESMOS tempos, nº de caracteres enviados)."""
    plan: list[tuple[int, list[int] | None, bool]] = []   # por cue: índices dos segmentos no lote
    items: list[str] = []
    for cue in cues:
        if not translatable(cue.text):
            plan.append((0, None, False))
            continue
        segs, dialog = split_segments(cue.text)
        idx = []
        for s in segs:
            idx.append(len(items))
            items.append(s)
        plan.append((0, idx, dialog))
    chars = sum(len(t) for t in items)
    translated = deepl.translate(items, target, source, progress) if items else []
    out: list[Cue] = []
    for cue, (_, idx, dialog) in zip(cues, plan):
        if idx is None:
            out.append(Cue(cue.start_ms, cue.end_ms, cue.text))
            continue
        parts = [translated[i].strip() for i in idx]
        text = "\n".join(f"- {p}" for p in parts) if dialog else rewrap(parts[0])
        out.append(Cue(cue.start_ms, cue.end_ms, text))
    return out, chars


def count_chars(cues: list[Cue]) -> int:
    n = 0
    for cue in cues:
        if translatable(cue.text):
            n += sum(len(s) for s in split_segments(cue.text)[0])
    return n


def _ts(ms: int) -> str:
    s, ms = divmod(max(0, ms), 1000)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    return "\n".join(f"{i}\n{_ts(c.start_ms)} --> {_ts(c.end_ms)}\n{c.text}\n" for i, c in enumerate(cues, 1))
