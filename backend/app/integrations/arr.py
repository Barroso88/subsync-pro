"""Radarr / Sonarr (API v3): metadados e correspondência com os ficheiros no disco."""
from __future__ import annotations

from typing import Any

import httpx

from .common import IntegrationError, client, map_remote_to_local, public_image_host_ok, validate_service_url


class Arr:
    def __init__(self, kind: str, url: str, api_key: str, mappings: list[dict], transport: httpx.BaseTransport | None = None,
                 validate: bool = True):
        self.kind, self.mappings, self.key = kind, mappings, api_key
        self.url = validate_service_url(url) if validate else url.rstrip("/")
        self.transport = transport

    def _get(self, path: str, **params) -> Any:
        if not self.key:
            raise IntegrationError(f"API key do {self.kind.title()} não configurada.")
        try:
            with client(transport=self.transport) as c:
                r = c.get(f"{self.url}/api/v3{path}", params=params, headers={"X-Api-Key": self.key})
        except httpx.HTTPError as e:
            raise IntegrationError(f"Sem ligação ao {self.kind.title()}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise IntegrationError(f"{self.kind.title()} recusou a API key.")
        if r.status_code >= 400:
            raise IntegrationError(f"{self.kind.title()} respondeu HTTP {r.status_code}.")
        return r.json()

    def test(self) -> dict:
        j = self._get("/system/status")
        return {"ok": True, "version": j.get("version"), "app": j.get("appName")}

    def _poster(self, images: list[dict]) -> str | None:
        """URL público TMDb/TVDB primeiro (rápido, sem autenticação) e, como alternativa, `arr:<kind>:<caminho local>`."""
        for i in images or []:
            if i.get("coverType") == "poster":
                local = i.get("url") or ""
                remote = i.get("remoteUrl") or ""
                parts = []
                if remote.startswith("https://") and public_image_host_ok(remote):
                    parts.append(remote.replace("/original/", "/w342/"))
                if local.startswith("/"):
                    parts.append(f"arr:{self.kind}:{local}")
                return "|".join(parts) or None
        return None

    def movies(self) -> list[dict]:
        out = []
        for m in self._get("/movie"):
            mf = m.get("movieFile") or {}
            path = mf.get("path")
            if not path:
                continue
            out.append({"id": m.get("id"), "path": map_remote_to_local(path, self.mappings), "title": m.get("title"), "year": m.get("year"),
                        "imdb_id": m.get("imdbId"), "tmdb_id": m.get("tmdbId"), "poster": self._poster(m.get("images"))})
        return out

    def episodes(self) -> list[dict]:
        out = []
        for s in self._get("/series"):
            poster = self._poster(s.get("images"))
            files = {f["id"]: f for f in self._get("/episodefile", seriesId=s["id"])}
            eps = self._get("/episode", seriesId=s["id"])
            for e in eps:
                f = files.get(e.get("episodeFileId"))
                if not f or not f.get("path"):
                    continue
                out.append({"path": map_remote_to_local(f["path"], self.mappings), "series_title": s.get("title"),
                            "year": s.get("year"), "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
                            "episode_title": e.get("title"), "imdb_id": s.get("imdbId"), "tmdb_id": s.get("tmdbId"),
                            "tvdb_id": s.get("tvdbId"), "poster": poster})
        return out

    def fetch_image(self, rel: str) -> tuple[bytes, str]:
        """Tenta com cabeçalho X-Api-Key e depois com ?apikey= (conforme a versão/autenticação do Radarr/Sonarr)."""
        last = 0
        for headers, params in (({"X-Api-Key": self.key}, {}), ({}, {"apikey": self.key})):
            try:
                with client(transport=self.transport) as c:
                    r = c.get(f"{self.url}{rel}", headers=headers, params=params)
            except httpx.HTTPError as e:
                raise IntegrationError(f"Sem ligação: {type(e).__name__}") from e
            last = r.status_code
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return r.content, r.headers["content-type"]
        raise IntegrationError(f"{self.kind.title()} devolveu HTTP {last} para a imagem.")
