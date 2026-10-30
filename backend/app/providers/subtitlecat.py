"""Subtitlecat (https://www.subtitlecat.com): site público de legendas, SEM API oficial.

LEIA ANTES DE ATIVAR
- Isto lê as páginas HTML do site (não há API). Está DESLIGADO por omissão e o utilizador é responsável por cumprir os termos do site.
- Comportamento educado e fixo: respeita o `robots.txt`, identifica-se (User-Agent com o nome da app), no máximo 1 pedido a cada ~4 s (e o limite por minuto
  das Definições), poucas páginas por pesquisa, sem login, SEM contornar bloqueios, captchas ou limites: se o site recusar (403/429/503/desafio anti-robô),
  a app pára e diz-lho.
- Só descarrega ficheiros .srt que o site já tem prontos. Os botões «Translate» (tradução feita no servidor deles) NÃO são usados.
- Muitas legendas do Subtitlecat são TRADUÇÕES AUTOMÁTICAS de outra língua: ficam marcadas como tal (e penalizadas na avaliação).
- É frágil por natureza: se o site mudar o HTML, a pesquisa deixa de devolver resultados até se atualizar o adaptador.
"""
from __future__ import annotations

import html as htmllib
import re
import threading
import time
from typing import Any
from urllib.parse import quote
from urllib.robotparser import RobotFileParser

import httpx

from ..languages import LANGS, normalize_lang
from .base import DownloadedFile, Provider, ProviderError, SearchQuery, SubtitleResult, fetch_limited

BASE = "https://www.subtitlecat.com"
HOSTS = ("subtitlecat.com",)
UA = "SubSyncPro/1.0 (+https://github.com/Barroso88/subsync-pro; uso pessoal, ritmo baixo)"
MIN_INTERVAL = 4.0            # segundos entre pedidos ao site
MAX_HTML = 2_000_000
_PATH_OK = re.compile(r"^/subs/\d+/[^/]+\.srt$")
_ROW = re.compile(r'<tr>\s*<td><a href="(subs/\d+/[^"]+\.html)">(.*?)</a>(?:\s*\(translated from ([^)]*)\))?</td>(.*?)</tr>', re.S)
_DL = re.compile(r'<a id="download_([\w-]+)"[^>]*?href="(/subs/\d+/[^"]+\.srt)"', re.S)
_METRIC = re.compile(r'sub-table__metric-label">\s*(?:<img[^>]*>)?\s*(Size|Downloads|Languages)</span><span class="sub-table__metric-value">([^<]*)')
_TOK = re.compile(r"[a-z0-9]+")
_NAME2CODE = {v[0].lower(): k for k, v in LANGS.items()}


def _tokens(s: str) -> set[str]:
    return set(_TOK.findall(re.sub(r"[^\w]+", " ", s.lower())))


class Subtitlecat(Provider):
    name = "subtitlecat"
    title = "Subtitlecat"
    supports_hash = False
    homepage = "https://www.subtitlecat.com"
    secrets: tuple[str, ...] = ()
    notice = ("Sem API: lê as páginas do site, de forma educada (robots.txt, 1 pedido a cada ~4 s, sem contornar bloqueios). Muitas legendas são traduções "
              "automáticas. Ative só se aceitar os termos do site.")

    _lock = threading.Lock()
    _last = 0.0
    _robots: tuple[float, RobotFileParser] | None = None
    _sleep = staticmethod(time.sleep)

    def configured(self) -> bool:
        return True

    # ---- acesso ao site (educado) ---------------------------------------------
    def _pace(self) -> None:
        if self.ctx.limiter:
            self.ctx.limiter.acquire()
        with Subtitlecat._lock:
            gap = MIN_INTERVAL - (time.monotonic() - Subtitlecat._last)
            if gap > 0:
                self._sleep(gap)
            Subtitlecat._last = time.monotonic()

    def _robots_ok(self, c: httpx.Client, path: str) -> bool:
        cached = Subtitlecat._robots
        if not cached or time.time() - cached[0] > 6 * 3600:
            rp = RobotFileParser()
            r = c.get(BASE + "/robots.txt", headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif r.status_code in (401, 403):
                rp.parse(["User-agent: *", "Disallow: /"])
            else:
                rp.parse([])                         # sem robots.txt: sem restrições declaradas
            cached = Subtitlecat._robots = (time.time(), rp)
        return cached[1].can_fetch(UA, BASE + path)

    @staticmethod
    def _blocked(r: httpx.Response) -> bool:
        if r.status_code in (403, 429, 503):
            return True
        head = r.text[:6000].lower() if "text/html" in r.headers.get("content-type", "") else ""
        return any(k in head for k in ("captcha", "cf-chl", "just a moment", "attention required", "access denied"))

    def _get(self, path: str, **params) -> str:
        try:
            with self.ctx.http() as c:
                if not self._robots_ok(c, path):
                    raise ProviderError("O robots.txt do Subtitlecat não permite este acesso: a app não o faz.")
                self._pace()
                r = c.get(BASE + path, params=params or None, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=20, follow_redirects=False)
        except httpx.HTTPError as e:
            msg = f"Erro de rede: {type(e).__name__}"
            self.ctx.record(ok=False, error=msg)
            raise ProviderError(msg) from e
        if self._blocked(r):
            msg = f"O Subtitlecat recusou ou bloqueou o pedido (HTTP {r.status_code}). A app não contorna bloqueios: tente mais tarde ou desative o fornecedor."
            self.ctx.record(ok=False, error=msg)
            raise ProviderError(msg, rate_limited=True, retry_after=600)
        if r.status_code >= 400:
            raise ProviderError(f"Subtitlecat respondeu HTTP {r.status_code}.")
        if len(r.content) > MAX_HTML:
            raise ProviderError("Página do Subtitlecat demasiado grande.")
        return r.text

    # ---- pesquisa ------------------------------------------------------------------
    @staticmethod
    def parse_results(page: str) -> list[dict]:
        out = []
        for href, name, orig, rest in _ROW.findall(page):
            m = {k.lower(): v for k, v in _METRIC.findall(rest)}
            dl = re.sub(r"\D", "", m.get("downloads", ""))
            out.append({"href": "/" + href.lstrip("/"), "name": htmllib.unescape(re.sub(r"<[^>]+>", "", name)).strip(), "orig": (orig or "").strip() or None,
                        "downloads": int(dl) if dl else None, "size": m.get("size")})
        return out

    @staticmethod
    def parse_downloads(page: str) -> list[tuple[str, str]]:
        """[(código de idioma do site, caminho do .srt)] só dos ficheiros já prontos (links «Download»)."""
        return [(code, path) for code, path in _DL.findall(page) if _PATH_OK.match(path)]

    @staticmethod
    def _lang(code: str) -> str | None:
        c = code.strip()
        low = c.lower()
        if low in ("pt-br", "pt_br"):
            return "pt-BR"
        if low in ("pt-pt", "pt_pt"):
            return "pt-PT"
        return normalize_lang(low)

    def search(self, q: SearchQuery) -> list[SubtitleResult]:
        title = (q.series_title or q.title) if q.kind == "episode" else q.title
        if not title:
            return []
        term = f"{title} S{q.season:02d}E{q.episode:02d}" if q.kind == "episode" and q.season is not None and q.episode is not None else \
               (f"{title} {q.year}" if q.year and q.mode == "full" else title)
        rows = self.parse_results(self._get("/index.php", search=term))
        self.ctx.record(ok=True)
        want_t = _tokens(title)
        ref = _tokens(q.file_name or "")
        cand = []
        for r in rows:
            toks = _tokens(r["name"])
            if q.kind == "episode":
                tag = f"s{q.season:02d}e{q.episode:02d}" if q.season is not None and q.episode is not None else ""
                if tag and tag not in toks:
                    continue
            if want_t and not want_t <= toks:
                continue                                          # o título tem de aparecer no nome
            if q.kind == "movie" and re.search(r"\bs\d{1,2}e\d{1,3}\b", r["name"].lower()):
                continue                                          # episódios de séries com o mesmo nome
            if q.kind == "movie" and q.year and not (str(q.year) in toks or not re.search(r"\b(19|20)\d{2}\b", r["name"])):
                continue                                          # nomes com outro ano ficam de fora
            cand.append((len(toks & ref) / max(1, len(toks | ref)), r))
        cand.sort(key=lambda x: -x[0])
        max_pages = int((self.ctx.get_config() or {}).get("max_pages", 3))
        out: list[SubtitleResult] = []
        for _score, r in cand[:max_pages]:
            if len(out) >= q.max_results:
                break
            page = self._get("/" + r["href"].lstrip("/"))
            orig_code = _NAME2CODE.get((r["orig"] or "").lower())
            for code, path in self.parse_downloads(page):
                lang = self._lang(code)
                if not lang or not (not q.languages or lang in q.languages or (lang == "pt" and any(l.startswith("pt") for l in q.languages))):
                    continue
                machine = bool(r["orig"]) and not (orig_code and lang.split("-")[0] == orig_code.split("-")[0])
                fname = path.rsplit("/", 1)[-1]
                out.append(SubtitleResult(provider=self.name, file_id=path, language=lang, release=r["name"], file_name=fname, download_count=r["downloads"],
                                          machine_translated=machine or not r["orig"], page_url=BASE + "/" + r["href"].lstrip("/"), format="srt"))
        return out[: q.max_results]

    def download(self, r: SubtitleResult) -> DownloadedFile:
        if not _PATH_OK.match(r.file_id):
            raise ProviderError("Identificador de download do Subtitlecat inválido.")
        with self.ctx.http() as c:
            if not self._robots_ok(c, r.file_id):
                raise ProviderError("O robots.txt do Subtitlecat não permite este acesso: a app não o faz.")
            self._pace()
            data = fetch_limited(c, BASE + quote(r.file_id, safe="/"), headers={"User-Agent": UA}, allowed=HOSTS)
        if data[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            raise ProviderError("O Subtitlecat devolveu uma página em vez do ficheiro (possível bloqueio). A app não o contorna.", rate_limited=True)
        self.ctx.record(ok=True)
        return DownloadedFile(r.file_id.rsplit("/", 1)[-1], data)

    def test_connection(self) -> dict[str, Any]:
        try:
            self._get("/")
        except ProviderError as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": "Site acessível e o robots.txt permite a pesquisa (isto não testa o download)."}
