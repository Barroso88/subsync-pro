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

    BASE_GAP = 4.2        # s entre pedidos (≈14/min: dentro dos 15/min do escalão gratuito dos Flash-Lite); sobe sozinho se houver 429 por minuto
    GEMMA_GAP = 8.0       # ≈7/min ≈ 14K tokens/min (limite gratuito do Gemma: 16K)
    _AUTO: dict[str, tuple[float, str]] = {}
    _SHARED: dict[str, dict] = {}        # por chave: instante do último pedido e espaçamento aprendido (partilhados entre traduções consecutivas)     # chave -> (quando, modelo escolhido automaticamente)

    def __init__(self, key: str, model: str = "", transport: httpx.BaseTransport | None = None):
        self.key, self.transport = key, transport
        self.model = re.sub(r"[^A-Za-z0-9._-]", "", model or "")      # vazio = automático (o Flash mais recente que a chave pode usar)
        self.base = "https://generativelanguage.googleapis.com/v1beta"
        self.untranslated = 0           # itens que o modelo recusou/falhou e ficaram no original
        self._sleep = time.sleep
        # escada de compatibilidade: 0 = completo (instrução de sistema + JSON + sem raciocínio + sem filtros); 1 = sem «thinkingConfig»;
        # 2 = sem «safetySettings»; 3 = instruções na mensagem e JSON lido do texto. Sobe quando o modelo responde 400 e lembra-se.
        self._level = 0
        self._sh = Gemini._SHARED.setdefault(key, {"last": 0.0, "gap": 0.0})
        self.timeout = 120.0                 # por pedido
        self.deadline: float | None = None   # instante limite (time.monotonic) para o conjunto, usado no «Testar»
        self.stats = {"requests": 0, "rate_limited": 0, "invalid": 0, "slowest": 0.0}

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(self.timeout, connect=15.0), follow_redirects=False, transport=self.transport,
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
        """O melhor modelo para o escalão gratuito: o «flash-lite» estável mais recente (limites muito mais altos: no AI Studio, 15 pedidos/min e 500/dia
        contra 5/min e 20/dia dos Flash normais); se não houver, o «flash» estável mais recente. Sem preview/imagem/áudio."""
        def newest(pattern: str) -> str | None:
            best, best_v = None, -1.0
            for n in names:
                m = re.fullmatch(pattern, n)
                if m and float(m.group(1)) > best_v:
                    best, best_v = n, float(m.group(1))
            return best
        return newest(r"gemini-(\d+(?:\.\d+)?)-flash-lite") or newest(r"gemini-(\d+(?:\.\d+)?)-flash")

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
        inline = self._is_inline()
        safety = [] if self._level >= 2 else self.SAFETY
        if inline:
            # modelos sem «instruções de sistema» nem modo JSON (ex.: Gemma): tudo na mensagem, resposta lida do texto
            prompt = (system + ' Respond with ONLY a JSON object of the form {"translations": ["...", "..."]} and nothing else (no markdown, no commentary).\n\nINPUT:\n'
                      + json.dumps(user, ensure_ascii=False))
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
            if safety:
                body["safetySettings"] = safety
        else:
            body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": json.dumps(user, ensure_ascii=False)}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                         "responseSchema": {"type": "OBJECT", "properties": {"translations": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["translations"]}}}
            if safety:
                body["safetySettings"] = safety
            if self._level == 0:
                body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        for attempt in range(5):
            gap = self._min_interval() - (time.monotonic() - self._sh["last"])
            if gap > 0:
                self._time_check(gap)
                self._sleep(gap)            # respeita o limite de tokens por minuto (Gemma: 16K/min no escalão gratuito)
            self._time_check()
            self._sh["last"] = t0 = time.monotonic()
            try:
                r = c.post(f"{self.base}/models/{self.model}:generateContent", json=body)
                self.stats["requests"] += 1
                self.stats["slowest"] = max(self.stats["slowest"], time.monotonic() - t0)
            except httpx.TimeoutException:
                self.stats["requests"] += 1
                self.stats["slowest"] = max(self.stats["slowest"], time.monotonic() - t0)
                self._time_check(self.timeout)
                if attempt >= 1:
                    raise DeepLError(f"O Gemini não respondeu a tempo ({self._stats_text()}).")
                continue
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise DeepLError(f"Sem ligação ao Gemini: {type(e).__name__}") from e
                self._sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                self.stats["rate_limited"] += 1
                kind, delay = self._rate_info(r)
                if kind == "day":
                    raise DeepLError(f"Limite diário de pedidos do Gemini esgotado para o modelo «{self.model}» (escalão gratuito). Tente amanhã, ative o faturamento "
                                     "ou escolha um modelo com limite diário maior (ex.: um «flash-lite»; veja Fornecedores → Listar modelos).", quota=True)
                self._sh["gap"] = min(max(self._sh["gap"] * 2, 8.0), 15.0)      # limite por minuto: abranda o ritmo dos pedidos seguintes (5/min → 12 s)
                if attempt < 4:
                    self._time_check(delay)
                    self._sleep(delay)          # limite por minuto: espera o que o Google indica (ou 15 s) e repete
                    continue
            elif r.status_code in (500, 503) and attempt < 3:
                self._sleep(4 * (attempt + 1))
                continue
            break
        if r.status_code == 400 and self._level < 3:
            low = r.text.lower()
            if re.search(r"developer instruction|system instruction|mime|json mode|response_?schema|schema", low):
                self._level = 3                          # sem instruções de sistema / modo JSON: tudo na mensagem
            elif "think" in low:
                self._level = max(self._level, 1)        # não aceita desligar o raciocínio
            elif "safety" in low:
                self._level = max(self._level, 2)
            elif "invalid argument" in low or "invalid_argument" in low:
                self._level += 1                         # 400 genérico: simplifica o pedido passo a passo
            else:
                self._level = 99
            if self._level < 99:
                return self._call(c, items, target, source, ctx)
            self._level = 3
        if r.status_code in (400, 401, 403):
            msg = ""
            try:
                msg = r.json().get("error", {}).get("message", "")[:160]
            except ValueError:
                pass
            raise DeepLError(f"O Gemini recusou o pedido ({r.status_code}) com o modelo «{self.model}»: {msg or 'verifique a chave e o modelo'}"
                             f"{' (já tentado sem filtros, sem «thinking» e com instruções na mensagem)' if r.status_code == 400 and self._level >= 3 else ''}",
                             auth=r.status_code in (401, 403))
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
            raw = "".join(p.get("text", "") for p in cand["content"]["parts"])
            data = json.loads(self._extract_json(raw) if inline else raw)
            out = data["translations"] if isinstance(data, dict) else data
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(out, list) or len(out) != len(items) or not all(isinstance(x, str) for x in out):
            return None
        return [x.strip() for x in out]

    def _stats_text(self) -> str:
        s = self.stats
        return f"{s['requests']} pedidos, {s['rate_limited']} com limite de taxa (429), {s['invalid']} respostas inválidas, o mais lento {s['slowest']:.0f} s"

    def _time_check(self, wait: float = 0.0) -> None:
        if self.deadline is not None and time.monotonic() + wait > self.deadline:
            raise DeepLError(f"Tempo de teste excedido ({self._stats_text()}). O modelo «{self.model}» está demasiado lento ou a ser limitado; "
                             "experimente outro (ex.: um «flash-lite») em Fornecedores → Listar modelos.")

    def _is_inline(self) -> bool:
        return self._level >= 3 or self.model.lower().startswith("gemma")

    def _min_interval(self) -> float:
        base = self.GEMMA_GAP if self.model.lower().startswith("gemma") else self.BASE_GAP
        return max(base, self._sh["gap"])

    @staticmethod
    def _extract_json(raw: str) -> str:
        """Tira cercas de markdown e texto à volta: devolve o primeiro objeto/array JSON da resposta."""
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
        if not starts:
            return t
        i = min(starts)
        j = max(t.rfind("}"), t.rfind("]"))
        return t[i:j + 1] if j > i else t

    @staticmethod
    def _rate_info(r: httpx.Response) -> tuple[str, float]:
        """429 do Gemini: («day»|«minute», segundos a esperar). O Google indica o quotaId violado e o retryDelay."""
        text, delay = r.text, 15.0
        try:
            for d in r.json().get("error", {}).get("details", []):
                if str(d.get("@type", "")).endswith("RetryInfo"):
                    m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
                    if m:
                        delay = float(m.group(1))
        except (ValueError, AttributeError):
            pass
        kind = "day" if re.search(r"PerDay|per day|RequestsPerDay", text, re.I) else "minute"
        return kind, min(max(delay, 2.0) + 1.0, 90.0)

    def _chunk(self, c: httpx.Client, items: list[str], target: str, source: str | None, ctx: list[str], depth: int = 0) -> list[str]:
        res = self._call(c, items, target, source, ctx)
        if res is None:
            self.stats["invalid"] += 1
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
