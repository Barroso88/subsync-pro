"""Tradução automática de legendas com a API do DeepL.

Só o texto é traduzido; os tempos de cada evento são mantidos exatamente.
O resultado é SEMPRE assinalado como tradução automática. O texto das legendas
é enviado ao DeepL (terceiro): isto está documentado no README.
"""
from __future__ import annotations

import json
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


# ---- Gemini (Google AI Studio) -------------------------------------------------------

LANG_NAMES = {"pt-PT": "European Portuguese (Portugal, NOT Brazilian: use vocabulary and grammar of Portugal, e.g. «ecrã», «autocarro», «telemóvel», «a fazer»)",
              "pt-BR": "Brazilian Portuguese", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "nl": "Dutch"}
SRC_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "nl": "Dutch", "pt": "Portuguese", "pt-PT": "Portuguese", "pt-BR": "Portuguese"}
GEMINI_CHUNK = 40        # falas por pedido (contexto suficiente sem respostas demasiado longas)


class Gemini:
    """Tradução por modelo de IA. Só texto; devolve SEMPRE o mesmo número de itens (valida e subdivide)."""
    SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in
              ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]

    _AUTO: dict[str, tuple[float, str]] = {}     # chave -> (quando, modelo escolhido automaticamente)

    def __init__(self, key: str, model: str = "", transport: httpx.BaseTransport | None = None):
        self.key, self.transport = key, transport
        self.model = re.sub(r"[^A-Za-z0-9._-]", "", model or "")      # vazio = automático (o Flash mais recente que a chave pode usar)
        self.base = "https://generativelanguage.googleapis.com/v1beta"
        self.untranslated = 0           # itens que o modelo recusou/falhou e ficaram no original
        self._sleep = time.sleep
        self._thinking = True           # tenta desligar o «pensar» do modelo (mais rápido); recua se o modelo o rejeitar

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=120, follow_redirects=False, transport=self.transport,
                            headers={"x-goog-api-key": self.key, "User-Agent": "SubSyncPro/1.0", "content-type": "application/json"})

    def usage(self) -> Usage:            # o Google não expõe consumo: a quota vê-se no AI Studio
        return Usage(0, 0)

    def list_models(self, c: httpx.Client | None = None) -> list[str]:
        """Modelos que esta chave pode usar para gerar texto (nomes sem o prefixo «models/»)."""
        own = c is None
        c = c or self._client()
        try:
            r = c.get(f"{self.base}/models", params={"pageSize": 200})
        except httpx.HTTPError as e:
            raise DeepLError(f"Sem ligação ao Gemini: {type(e).__name__}") from e
        finally:
            if own:
                c.close()
        if r.status_code in (401, 403):
            raise DeepLError("O Gemini recusou a chave.", auth=True)
        if r.status_code >= 400:
            raise DeepLError(f"Gemini respondeu HTTP {r.status_code} ao listar modelos.")
        names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", []) if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        return sorted(names)

    @staticmethod
    def pick_flash(names: list[str]) -> str | None:
        """O «gemini-X[.Y]-flash» estável mais recente (sem lite/preview/imagem/áudio)."""
        best, best_v = None, (-1.0,)
        for n in names:
            m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash", n)
            if m and (float(m.group(1)),) > best_v:
                best, best_v = n, (float(m.group(1)),)
        return best

    def _ensure_model(self, c: httpx.Client) -> None:
        if self.model:
            return
        hit = Gemini._AUTO.get(self.key)
        if hit and time.time() - hit[0] < 3600:
            self.model = hit[1]
            return
        try:
            self.model = self.pick_flash(self.list_models(c)) or "gemini-flash-latest"
        except DeepLError as e:
            if e.auth:
                raise
            self.model = "gemini-flash-latest"       # alias oficial do Flash mais recente
        Gemini._AUTO[self.key] = (time.time(), self.model)

    def _call(self, c: httpx.Client, items: list[str], target: str, source: str | None, ctx: list[str]) -> list[str] | None:
        """Um pedido. Devolve a lista traduzida, ou None se recusada/inválida (o chamador subdivide)."""
        system = (f"You are a professional subtitle translator. Translate each item from {SRC_NAMES.get(source or '', 'the source language')} into {LANG_NAMES[target]}. "
                  "The items are consecutive subtitle lines of one film, in order; use neighbouring lines for context, pronouns and tone, but translate EACH item separately. "
                  "Rules: return a JSON array of strings with EXACTLY the same number of items, same order; never merge, split, drop or add items; keep HTML tags such as <i></i> and "
                  "line breaks («\n») in place; keep names, numbers and sound-effect markers; keep it concise and natural for on-screen reading; do not add notes or explanations.")
        user = {"previous_lines_for_context_only": ctx[-3:], "items_to_translate": items}
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": json.dumps(user, ensure_ascii=False)}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                     "responseSchema": {"type": "OBJECT", "properties": {"translations": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["translations"]}},
                "safetySettings": self.SAFETY}
        if self._thinking:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        for attempt in range(4):
            try:
                r = c.post(f"{self.base}/models/{self.model}:generateContent", json=body)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise DeepLError(f"Sem ligação ao Gemini: {type(e).__name__}") from e
                self._sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 503) and attempt < 3:
                self._sleep(4 * (attempt + 1))
                continue
            break
        if r.status_code == 400 and self._thinking and "think" in r.text.lower():
            self._thinking = False       # este modelo não aceita desligar o raciocínio: repete sem isso (e lembra-se)
            return self._call(c, items, target, source, ctx)
        if r.status_code in (400, 401, 403):
            msg = ""
            try:
                msg = r.json().get("error", {}).get("message", "")[:160]
            except ValueError:
                pass
            raise DeepLError(f"O Gemini recusou o pedido ({r.status_code}): {msg or 'verifique a chave e o modelo'}", auth=r.status_code in (401, 403))
        if r.status_code == 404:
            Gemini._AUTO.pop(self.key, None)
            hint = ""
            try:
                av = [n for n in self.list_models(c) if "flash" in n][:6]
                hint = f" Modelos disponíveis para a sua chave: {', '.join(av)}." if av else ""
            except DeepLError:
                pass
            raise DeepLError(f"Modelo «{self.model}» não encontrado.{hint} Deixe o campo do modelo vazio (automático) ou escreva um dos nomes acima.")
        if r.status_code == 429:
            raise DeepLError("Quota/limite de pedidos do Gemini atingido. Tente mais tarde.", quota=True)
        if r.status_code >= 400:
            raise DeepLError(f"Gemini respondeu HTTP {r.status_code}.")
        try:
            j = r.json()
            cand = (j.get("candidates") or [None])[0]
            if not cand or (j.get("promptFeedback") or {}).get("blockReason") or cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
                return None
            data = json.loads("".join(p.get("text", "") for p in cand["content"]["parts"]))
            out = data["translations"] if isinstance(data, dict) else data
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(out, list) or len(out) != len(items) or not all(isinstance(x, str) for x in out):
            return None
        return [x.strip() for x in out]

    def _chunk(self, c: httpx.Client, items: list[str], target: str, source: str | None, ctx: list[str], depth: int = 0) -> list[str]:
        res = self._call(c, items, target, source, ctx)
        if res is not None and all(t.strip() or not o.strip() for t, o in zip(res, items)):
            return res
        if len(items) == 1:
            self.untranslated += 1       # recusada/inválida mesmo sozinha: fica no original (e é contado)
            return [items[0]]
        mid = len(items) // 2
        return self._chunk(c, items[:mid], target, source, ctx, depth + 1) + self._chunk(c, items[mid:], target, source, ctx + items[:mid], depth + 1)

    def translate(self, texts: list[str], target: str, source: str | None = None, progress: Callable[[int, int], None] | None = None) -> list[str]:
        if target not in LANG_NAMES:
            raise DeepLError(f"Idioma de destino não suportado: {target}")
        self.untranslated = 0
        out: list[str] = []
        chunks = [texts[i:i + GEMINI_CHUNK] for i in range(0, len(texts), GEMINI_CHUNK)]
        with self._client() as c:
            self._ensure_model(c)
            for n, ch in enumerate(chunks):
                out += self._chunk(c, ch, target, source, texts[max(0, len(out) - 3):len(out)])
                if progress:
                    progress(n + 1, len(chunks))
        if texts and self.untranslated > max(3, len(texts) * 0.03):
            raise DeepLError(f"O Gemini recusou ou falhou {self.untranslated} de {len(texts)} falas (>3 %): tradução rejeitada para não instalar uma legenda incompleta.")
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


def translate_cues(cues: list[Cue], target: str, source: str | None, deepl: "DeepL | Gemini",
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
