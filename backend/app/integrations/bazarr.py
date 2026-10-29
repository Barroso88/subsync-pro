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
BAZARR_TO_LANG = {v: k for k, v in BAZARR_LANG.items()}


def _b(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


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

    def movie_history(self, radarr_id: int, length: int = 60) -> list[dict]:
        """Histórico do filme: que fornecedor do Bazarr descarregou cada ficheiro (do mais recente para o mais antigo)."""
        r = self._call("GET", "/movies/history", timeout=20, start=0, length=int(length), radarrid=int(radarr_id))
        if r.status_code >= 400:
            raise IntegrationError(f"Bazarr respondeu HTTP {r.status_code} ao ler o histórico.")
        try:
            data = r.json().get("data", [])
        except ValueError:
            return []
        out = []
        for e in data if isinstance(data, list) else []:
            if e.get("radarrId") not in (None, int(radarr_id)) or not e.get("subtitles_path") or not e.get("provider"):
                continue
            out.append({"file": str(e["subtitles_path"]).replace("\\", "/").rsplit("/", 1)[-1], "provider": str(e["provider"]), "score": e.get("score"),
                        "at": e.get("parsed_timestamp"), "language": (e.get("language") or {}).get("name")})
        return out

    def search_movie(self, radarr_id: int) -> list[dict]:
        """«Pesquisa manual» do Bazarr (`GET /providers/movies`): candidatas de TODOS os fornecedores dele. Demora (~10 s ou mais)."""
        r = self._call("GET", "/providers/movies", timeout=180, radarrid=int(radarr_id))
        if r.status_code == 404:
            raise IntegrationError("O Bazarr não conhece este filme (id do Radarr).")
        if r.status_code >= 400:
            raise IntegrationError(f"Bazarr respondeu HTTP {r.status_code} na pesquisa.")
        try:
            j = r.json()
        except ValueError:
            return []
        data = j.get("data", j) if isinstance(j, dict) else j
        out = []
        for e in data if isinstance(data, list) else []:
            if not e.get("provider") or not e.get("subtitle"):
                continue
            ri = e.get("release_info")
            release = " / ".join(str(x).strip() for x in ri if str(x).strip()) if isinstance(ri, list) else str(ri or "")
            release = " ".join(release.split())[:300]
            out.append({"provider": str(e["provider"]), "subtitle": str(e["subtitle"]), "language": str(e.get("language") or ""),
                        "hi": _b(e.get("hearing_impaired")), "forced": _b(e.get("forced")), "original_format": str(e.get("original_format", "False")),
                        "score": e.get("score"), "score_without_hash": e.get("score_without_hash"), "release": release,
                        "matches": [str(x) for x in (e.get("matches") or [])], "dont_matches": [str(x) for x in (e.get("dont_matches") or [])],
                        "uploader": e.get("uploader") if isinstance(e.get("uploader"), str) else None})
        return out

    def download_candidate(self, radarr_id: int, provider: str, subtitle: str, hi: bool, forced: bool, original_format: str = "False") -> dict:
        """`POST /providers/movies`: o Bazarr descarrega a legenda escolhida e grava-a ao lado do vídeo."""
        r = self._call("POST", "/providers/movies", timeout=240, radarrid=int(radarr_id), hi=str(bool(hi)), forced=str(bool(forced)),
                       original_format=str(original_format), provider=provider, subtitle=subtitle)
        if r.status_code in (200, 204):
            return {"ok": True, "status": r.status_code}
        if r.status_code == 404:
            raise IntegrationError("O Bazarr não conhece este filme (id do Radarr).")
        msg = ""
        try:
            msg = str(r.json().get("message") or "")[:200]
        except ValueError:
            pass
        return {"ok": False, "status": r.status_code, "message": msg or f"HTTP {r.status_code}"}

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
