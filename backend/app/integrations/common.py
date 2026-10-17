from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx


class IntegrationError(Exception):
    pass


def validate_service_url(url: str) -> str:
    """Proteção SSRF: só http(s), sem credenciais no URL, e sem endereços link-local/metadata/multicast.

    Endereços privados (LAN) e loopback são permitidos porque Plex/Radarr/Sonarr
    vivem normalmente na rede local; a aplicação deve estar atrás de autenticação.
    """
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise IntegrationError("URL inválido (use http:// ou https://).")
    if u.username or u.password:
        raise IntegrationError("Não inclua credenciais no URL.")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise IntegrationError("Não foi possível resolver o anfitrião.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise IntegrationError("Endereço de destino não permitido.")
    return url.strip().rstrip("/")


def map_remote_to_local(path: str, mappings: list[dict]) -> str:
    """Converte um caminho visto pelo serviço externo para o caminho visto por esta app."""
    best = None
    for m in mappings or []:
        r = (m.get("remote") or "").rstrip("/")
        if r and (path == r or path.startswith(r + "/")):
            if best is None or len(r) > len(best[0]):
                best = (r, (m.get("local") or "").rstrip("/"))
    if best:
        return best[1] + path[len(best[0]):]
    return path


def map_local_to_remote(path: str, mappings: list[dict]) -> str:
    return map_remote_to_local(path, [{"remote": m.get("local"), "local": m.get("remote")} for m in mappings or []])


def client(timeout: float = 15.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
                        headers={"User-Agent": "SubSyncPro/1.0"})
