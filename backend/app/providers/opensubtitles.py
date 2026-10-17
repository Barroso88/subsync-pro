"""OpenSubtitles.com — API REST oficial v1 (https://opensubtitles.stoplight.io).

Pesquisa requer `Api-Key`; descarregar requer também login (username/password → JWT).
Respeitamos os limites devolvidos pela API (`remaining`, 429) e não contornamos nada.
"""
from __future__ import annotations

import threading
from typing import Any

import httpx

from .base import (USER_AGENT, DownloadedFile, Provider, ProviderError, SearchQuery, SubtitleResult, fetch_limited)

API = "https://api.opensubtitles.com/api/v1"
DL_HOSTS = (".opensubtitles.com", ".opensubtitles.org")


def _i(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class OpenSubtitles(Provider):
    name = "opensubtitles"
    title = "OpenSubtitles.com"
    supports_hash = True
    homepage = "https://www.opensubtitles.com"
    secrets = ("opensubtitles_api_key", "opensubtitles_username", "opensubtitles_password")

    _token: str | None = None
    _token_base: str = "api.opensubtitles.com"
    _lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.ctx.get_secret("opensubtitles_api_key"))

    def _headers(self, auth: bool = False) -> dict:
        h = {"Api-Key": self.ctx.get_secret("opensubtitles_api_key"), "User-Agent": USER_AGENT,
             "Accept": "application/json", "Content-Type": "application/json"}
        if auth and self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _request(self, method: str, path: str, *, auth: bool = False, **kw) -> dict:
        if not self.configured():
            raise ProviderError("API key do OpenSubtitles não configurada.", auth=True)
        if self.ctx.limiter:
            self.ctx.limiter.acquire()
        try:
            with self.ctx.http() as c:
                url = f"{API}{path}"
                for _hop in range(4):
                    r = c.request(method, url, headers=self._headers(auth), timeout=20, **kw)
                    # a API normaliza os parâmetros (minúsculas/ordenados) com 301/302; seguimos só dentro do domínio oficial
                    if r.status_code in (301, 302, 307, 308) and r.headers.get("location"):
                        nxt = httpx.URL(url).join(r.headers["location"])
                        if nxt.scheme != "https" or not (nxt.host or "").endswith(".opensubtitles.com"):
                            raise ProviderError("Redirecionamento para domínio não autorizado.")
                        url = str(nxt)
                        kw = {k: v for k, v in kw.items() if k != "params"}
                        continue
                    break
        except httpx.HTTPError as e:
            msg = f"Erro de rede: {type(e).__name__}"
            self.ctx.record(ok=False, error=msg)
            raise ProviderError(msg) from e
        if r.status_code in (401, 403):
            if auth:
                OpenSubtitles._token = None
            msg = "Autenticação recusada pelo OpenSubtitles (API key/credenciais)."
            self.ctx.record(ok=False, error=msg, auth_ok=False)
            raise ProviderError(msg, auth=True)
        if r.status_code == 429:
            ra = _i(r.headers.get("retry-after"))
            self.ctx.record(ok=False, error="Limite de pedidos atingido (429)")
            raise ProviderError("OpenSubtitles: limite de pedidos atingido.", rate_limited=True, retry_after=ra)
        if r.status_code >= 400:
            msg = f"OpenSubtitles respondeu HTTP {r.status_code}"
            self.ctx.record(ok=False, error=msg)
            raise ProviderError(msg)
        try:
            return r.json()
        except ValueError as e:
            ct = r.headers.get("content-type", "?").split(";")[0]
            raise ProviderError(f"Resposta inválida do OpenSubtitles (HTTP {r.status_code}, {ct}).") from e

    def login(self) -> None:
        with self._lock:
            if OpenSubtitles._token:
                return
            u, p = self.ctx.get_secret("opensubtitles_username"), self.ctx.get_secret("opensubtitles_password")
            if not (u and p):
                raise ProviderError("Para descarregar é necessário username e password do OpenSubtitles.", auth=True)
            j = self._request("POST", "/login", json={"username": u, "password": p})
            OpenSubtitles._token = j.get("token")
            if not OpenSubtitles._token:
                raise ProviderError("Login no OpenSubtitles sem token.", auth=True)
            self.ctx.record(ok=True, auth_ok=True)

    # ------------------------------------------------------------------
    @staticmethod
    def build_params(q: SearchQuery) -> dict[str, Any]:
        p: dict[str, Any] = {"order_by": "download_count", "order_direction": "desc"}
        if q.languages:
            p["languages"] = ",".join(sorted({l.lower() for l in q.languages}))
        p["hearing_impaired"] = q.hi_policy
        p["foreign_parts_only"] = q.forced_policy
        if q.moviehash and q.mode in ("full", "hash"):
            p["moviehash"] = q.moviehash
        if q.mode == "hash":
            return {k: (v.lower() if isinstance(v, str) else v) for k, v in sorted(p.items())}
        use_ids = q.mode == "full"
        has_id = False
        if q.kind == "episode":
            p["type"] = "episode"
            if q.season is not None:
                p["season_number"] = q.season
            if q.episode is not None:
                p["episode_number"] = q.episode
            if use_ids and q.tmdb_id:
                p["parent_tmdb_id"], has_id = q.tmdb_id, True
            elif use_ids and q.imdb_id:
                p["parent_imdb_id"], has_id = q.imdb_id.lstrip("t"), True
            else:
                p["query"] = q.series_title or q.title
        else:
            p["type"] = "movie"
            if use_ids and q.imdb_id:
                p["imdb_id"], has_id = q.imdb_id.lstrip("t"), True
            elif use_ids and q.tmdb_id:
                p["tmdb_id"], has_id = q.tmdb_id, True
            else:
                p["query"] = q.title
            # o ano só entra quando NÃO há identificador (um ano ligeiramente diferente esvaziaria a pesquisa)
            if q.year and not has_id and q.mode == "full":
                p["year"] = q.year
        return {k: (v.lower() if isinstance(v, str) else v) for k, v in sorted(p.items())}

    def search(self, q: SearchQuery) -> list[SubtitleResult]:
        j = self._request("GET", "/subtitles", params=self.build_params(q))
        self.ctx.record(ok=True)
        out = []
        for d in j.get("data", [])[: q.max_results]:
            a = d.get("attributes", {})
            files = a.get("files") or []
            if not files:
                continue
            fd = a.get("feature_details") or {}
            lang = (a.get("language") or "").strip()
            out.append(SubtitleResult(
                provider=self.name, file_id=str(files[0].get("file_id")), language=_norm_lang(lang),
                release=a.get("release") or "", file_name=files[0].get("file_name") or "",
                hash_match=bool(a.get("moviehash_match")), hearing_impaired=bool(a.get("hearing_impaired")),
                forced=bool(a.get("foreign_parts_only")), fps=a.get("fps") or None,
                download_count=_i(a.get("download_count")), rating=a.get("ratings") or None,
                from_trusted=bool(a.get("from_trusted")), ai_translated=bool(a.get("ai_translated")),
                machine_translated=bool(a.get("machine_translated")),
                uploader=(a.get("uploader") or {}).get("name"), upload_date=a.get("upload_date"),
                feature_title=fd.get("movie_name") or fd.get("title"), feature_year=_i(fd.get("year")),
                season=_i(fd.get("season_number")), episode=_i(fd.get("episode_number")),
                imdb_id=str(fd.get("imdb_id")) if fd.get("imdb_id") else None, tmdb_id=_i(fd.get("tmdb_id")),
                page_url=a.get("url"), format="srt",
            ))
        return out

    def download(self, r: SubtitleResult) -> DownloadedFile:
        self.login()
        j = self._request("POST", "/download", auth=True, json={"file_id": int(r.file_id), "sub_format": "srt"})
        link = j.get("link")
        if not link:
            raise ProviderError(j.get("message") or "OpenSubtitles não devolveu link de download.")
        remaining = _i(j.get("remaining"))
        self.ctx.record(ok=True, remaining=remaining, reset=j.get("reset_time_utc"))
        with self.ctx.http() as c:
            data = fetch_limited(c, link, headers={"User-Agent": USER_AGENT}, allowed=DL_HOSTS)
        return DownloadedFile(j.get("file_name") or r.file_name or f"{r.file_id}.srt", data, remaining)

    def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "API key não configurada."}
        try:
            self._request("GET", "/infos/languages")
            self.ctx.record(ok=True, auth_ok=True)
        except ProviderError as e:
            return {"ok": False, "message": str(e), "auth_error": e.auth}
        res: dict[str, Any] = {"ok": True, "message": "API key aceite."}
        if self.ctx.get_secret("opensubtitles_username"):
            try:
                OpenSubtitles._token = None
                self.login()
                res["login"] = True
                res["message"] += " Login válido (downloads possíveis)."
            except ProviderError as e:
                res["login"] = False
                res["message"] += f" Login falhou: {e}"
        else:
            res["login"] = False
            res["message"] += " Sem credenciais de utilizador: pesquisa possível, descarga não."
        return res


def _norm_lang(tag: str) -> str | None:
    from ..languages import normalize_lang
    t = tag.lower()
    if t in ("pt-pt",):
        return "pt-PT"
    if t in ("pt-br",):
        return "pt-BR"
    return normalize_lang(t)
