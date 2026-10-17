"""Bazarr (API v1): pede ao Bazarr que procure e descarregue uma legenda de um filme.

A API é a que o próprio Bazarr documenta (swagger em /api/swagger.json):
  GET   /api/system/status
  PATCH /api/movies/subtitles?radarrid=&language=&forced=&hi=   («Download a movie subtitles»)
O Bazarr é que fala com os fornecedores dele (ex.: LegendasDivx), com a conta do utilizador.
"""
from __future__ import annotations

import httpx

from .common import IntegrationError, validate_service_url

# idioma interno -> código de idioma do Bazarr
BAZARR_LANG = {"pt-PT": "pt", "pt-BR": "pb", "en": "en", "es": "es", "fr": "fr", "de": "de", "it": "it"}


class Bazarr:
    def __init__(self, url: str, api_key: str, transport: httpx.BaseTransport | None = None, validate: bool = True):
        self.key, self.transport = api_key, transport
        self.url = validate_service_url(url) if validate else url.rstrip("/")

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, follow_redirects=False, transport=self.transport,
                            headers={"X-API-KEY": self.key, "User-Agent": "SubSyncPro/1.0"})

    def _call(self, method: str, path: str, timeout: float = 20, **params) -> httpx.Response:
        if not self.key:
            raise IntegrationError("API key do Bazarr não configurada.")
        try:
            with self._client(timeout) as c:
                r = c.request(method, f"{self.url}/api{path}", params=params)
        except httpx.TimeoutException as e:
            raise IntegrationError("O Bazarr demorou demasiado a responder.") from e
        except httpx.HTTPError as e:
            raise IntegrationError(f"Sem ligação ao Bazarr: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise IntegrationError("O Bazarr recusou a API key.")
        return r

    def test(self) -> dict:
        r = self._call("GET", "/system/status")
        if r.status_code >= 400:
            raise IntegrationError(f"Bazarr respondeu HTTP {r.status_code}.")
        d = r.json().get("data", r.json())
        return {"ok": True, "version": d.get("bazarr_version"), "name": "Bazarr"}

    def download_movie_subtitle(self, radarr_id: int, lang: str) -> dict:
        """Equivale ao botão de pesquisa do Bazarr. Pode demorar (procura em todos os fornecedores dele)."""
        code = BAZARR_LANG.get(lang)
        if not code:
            raise IntegrationError(f"Idioma não suportado na integração com o Bazarr: {lang}")
        r = self._call("PATCH", "/movies/subtitles", timeout=240, radarrid=int(radarr_id), language=code, forced="False", hi="False")
        if r.status_code in (200, 204):
            return {"ok": True, "status": r.status_code}
        if r.status_code == 404:
            raise IntegrationError("O Bazarr não conhece este filme (id do Radarr).")
        # o Bazarr devolve erro quando nenhum fornecedor tem legenda; é um resultado normal
        msg = ""
        try:
            msg = str(r.json().get("message") or "")[:200]
        except ValueError:
            pass
        return {"ok": False, "status": r.status_code, "message": msg or f"HTTP {r.status_code}"}
