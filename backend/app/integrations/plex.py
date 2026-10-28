"""Plex: metadados, correspondência de itens e refresh após instalar legendas."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .common import IntegrationError, client, map_remote_to_local, validate_service_url


class Plex:
    def __init__(self, url: str, token: str, mappings: list[dict], transport: httpx.BaseTransport | None = None,
                 validate: bool = True):
        self.token, self.mappings, self.transport = token, mappings, transport
        self.url = validate_service_url(url) if validate else url.rstrip("/")

    def _req(self, method: str, path: str, **params):
        if not self.token:
            raise IntegrationError("Token do Plex não configurado.")
        try:
            with client(transport=self.transport) as c:
                r = c.request(method, f"{self.url}{path}", params=params,
                              headers={"X-Plex-Token": self.token, "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise IntegrationError(f"Sem ligação ao Plex: {type(e).__name__}") from e
        if r.status_code == 401:
            raise IntegrationError("Plex recusou o token.")
        if r.status_code >= 400:
            raise IntegrationError(f"Plex respondeu HTTP {r.status_code}.")
        return r

    def test(self) -> dict:
        j = self._req("GET", "/").json()["MediaContainer"]
        return {"ok": True, "version": j.get("version"), "name": j.get("friendlyName")}

    def sections(self) -> list[dict]:
        return self._req("GET", "/library/sections").json()["MediaContainer"].get("Directory", [])

    def items(self, recent: int | None = None) -> list[dict]:
        """Filmes e episódios com caminho, ratingKey e poster. `recent=N`: só os N adicionados mais recentemente a cada secção (barato)."""
        out = []
        for sec in self.sections():
            t = sec.get("type")
            if t not in ("movie", "show"):
                continue
            params: dict = {"type": 1} if t == "movie" else {"type": 4}
            if recent:
                params.update({"sort": "addedAt:desc", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": int(recent)})
            for m in self._req("GET", f"/library/sections/{sec['key']}/all", **params).json()["MediaContainer"].get("Metadata", []):
                for media in m.get("Media", []):
                    for part in media.get("Part", []):
                        if not part.get("file"):
                            continue
                        out.append({
                            "path": map_remote_to_local(part["file"], self.mappings), "rating_key": str(m.get("ratingKey")),
                            "title": m.get("grandparentTitle") or m.get("title"),
                            "added": datetime.fromtimestamp(int(m["addedAt"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if m.get("addedAt") else None,
                            "poster": f"plex:{m.get('grandparentThumb') or m.get('thumb')}" if (m.get("grandparentThumb") or m.get("thumb")) else None,
                        })
        return out

    def refresh_item(self, rating_key: str) -> None:
        self._req("PUT", f"/library/metadata/{rating_key}/refresh")

    def fetch_image(self, rel: str) -> tuple[bytes, str]:
        """Capa em tamanho reduzido (transcodificador de fotos do Plex); se falhar, a original."""
        try:
            r = self._req("GET", "/photo/:/transcode", width=342, height=513, minSize=1, upscale=0, url=rel)
            if r.headers.get("content-type", "").startswith("image/") and r.content:
                return r.content, r.headers["content-type"]
        except IntegrationError:
            pass
        r = self._req("GET", rel)
        return r.content, r.headers.get("content-type", "image/jpeg")
