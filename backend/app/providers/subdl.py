"""SubDL (https://subdl.com) — API pública com chave gratuita.

ATENÇÃO: escrito a partir do conhecimento da API (documentação oficial inacessível
ao autor no momento da implementação) e testado apenas com respostas simuladas.
Valide com a sua chave em Fornecedores → Testar ligação.
Não suporta pesquisa por hash: a compatibilidade nunca será «confirmada por hash».
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..languages import normalize_lang
from .base import (USER_AGENT, DownloadedFile, Provider, ProviderError, SearchQuery, SubtitleResult, fetch_limited)

API = "https://api.subdl.com/api/v1/subtitles"
DL_BASE = "https://dl.subdl.com"
DL_HOSTS = (".subdl.com",)
_PATH_OK = re.compile(r"^/subtitle/[\w\-.]+\.zip$")

# códigos do SubDL <-> códigos internos
_TO_SUBDL = {"pt-PT": "PT", "pt-BR": "BR_PT"}
_FROM_SUBDL = {"BR_PT": "pt-BR", "PT": "pt", "PT_BR": "pt-BR"}   # «PT» sem variante conhecida: ver nota abaixo


def to_subdl_lang(code: str) -> str:
    return _TO_SUBDL.get(code, code.split("-")[0].upper())


def from_subdl_lang(tag: str | None) -> str | None:
    if not tag:
        return None
    t = tag.strip()
    if t.upper() in _FROM_SUBDL:
        return _FROM_SUBDL[t.upper()]
    if "brazil" in t.lower():
        return "pt-BR"
    return normalize_lang(t.lower())


def _i(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class SubDL(Provider):
    name = "subdl"
    title = "SubDL"
    supports_hash = False
    homepage = "https://subdl.com"
    secrets = ("subdl_api_key",)

    def configured(self) -> bool:
        return bool(self.ctx.get_secret("subdl_api_key"))

    @staticmethod
    def build_params(q: SearchQuery, api_key: str) -> dict[str, Any]:
        p: dict[str, Any] = {"api_key": api_key, "type": "tv" if q.kind == "episode" else "movie",
                             "subs_per_page": min(30, q.max_results)}
        if q.languages:
            p["languages"] = ",".join(dict.fromkeys(to_subdl_lang(l) for l in q.languages))
        if q.imdb_id:
            p["imdb_id"] = q.imdb_id if q.imdb_id.startswith("tt") else f"tt{q.imdb_id}"
        elif q.tmdb_id:
            p["tmdb_id"] = q.tmdb_id
        else:
            p["film_name"] = (q.series_title or q.title) if q.kind == "episode" else q.title
        if q.kind == "episode":
            if q.season is not None:
                p["season_number"] = q.season
            if q.episode is not None:
                p["episode_number"] = q.episode
        elif q.year:
            p["year"] = q.year
        return p

    def _get(self, params: dict) -> dict:
        if not self.configured():
            raise ProviderError("API key do SubDL não configurada.", auth=True)
        if self.ctx.limiter:
            self.ctx.limiter.acquire()
        try:
            with self.ctx.http() as c:
                r = c.get(API, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
        except httpx.HTTPError as e:
            msg = f"Erro de rede: {type(e).__name__}"
            self.ctx.record(ok=False, error=msg)
            raise ProviderError(msg) from e
        if r.status_code in (401, 403):
            msg = "Autenticação recusada pelo SubDL (API key)."
            self.ctx.record(ok=False, error=msg, auth_ok=False)
            raise ProviderError(msg, auth=True)
        if r.status_code == 429:
            self.ctx.record(ok=False, error="Limite de pedidos atingido (429)")
            raise ProviderError("SubDL: limite de pedidos atingido.", rate_limited=True)
        if r.status_code >= 400:
            msg = f"SubDL respondeu HTTP {r.status_code}"
            self.ctx.record(ok=False, error=msg)
            raise ProviderError(msg)
        try:
            j = r.json()
        except ValueError as e:
            raise ProviderError("Resposta inválida do SubDL.") from e
        if j.get("status") is False:
            err = str(j.get("error") or "")
            if "api" in err.lower() and "key" in err.lower():
                self.ctx.record(ok=False, error="API key inválida", auth_ok=False)
                raise ProviderError("API key do SubDL inválida.", auth=True)
            if "not found" in err.lower() or "no subtitle" in err.lower():
                return {"subtitles": []}
            raise ProviderError("SubDL: pedido recusado.")
        return j

    def search(self, q: SearchQuery) -> list[SubtitleResult]:
        j = self._get(self.build_params(q, self.ctx.get_secret("subdl_api_key")))
        self.ctx.record(ok=True, auth_ok=True)
        out = []
        for s in (j.get("subtitles") or [])[: q.max_results]:
            url = s.get("url") or ""
            if not _PATH_OK.match(url):
                continue
            hi = bool(s.get("hi"))
            if q.hi_policy == "exclude" and hi:
                pass  # mantém: a avaliação penaliza; o utilizador decide
            out.append(SubtitleResult(
                provider=self.name, file_id=url, language=from_subdl_lang(s.get("lang") or s.get("language")),
                release=s.get("release_name") or s.get("name") or "", file_name=s.get("name") or "",
                hearing_impaired=hi, uploader=s.get("author"),
                season=_i(s.get("season")), episode=_i(s.get("episode")) if not s.get("full_season") else None,
                page_url=f"https://subdl.com{s['subtitlePage']}" if str(s.get("subtitlePage", "")).startswith("/") else None,
                format="srt",
            ))
        return out

    def download(self, r: SubtitleResult) -> DownloadedFile:
        if not _PATH_OK.match(r.file_id):
            raise ProviderError("Identificador de download SubDL inválido.")
        with self.ctx.http() as c:
            data = fetch_limited(c, DL_BASE + r.file_id, headers={"User-Agent": USER_AGENT}, allowed=DL_HOSTS)
        self.ctx.record(ok=True)
        return DownloadedFile(r.file_id.rsplit("/", 1)[-1], data)

    def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "API key não configurada."}
        try:
            self._get({"api_key": self.ctx.get_secret("subdl_api_key"), "film_name": "Inception", "type": "movie", "languages": "EN", "subs_per_page": 1})
        except ProviderError as e:
            return {"ok": False, "message": str(e), "auth_error": e.auth}
        return {"ok": True, "message": "API key aceite (pesquisa de teste concluída)."}
