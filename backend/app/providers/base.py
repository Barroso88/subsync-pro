"""Contrato dos adapters de fornecedores + utilitários HTTP seguros."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

USER_AGENT = "SubSyncPro v1.0"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


class ProviderError(Exception):
    def __init__(self, message: str, *, auth: bool = False, rate_limited: bool = False, retry_after: int | None = None):
        super().__init__(message)
        self.auth, self.rate_limited, self.retry_after = auth, rate_limited, retry_after


@dataclass
class SearchQuery:
    kind: str                       # movie | episode
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    series_title: str | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    moviehash: str | None = None
    file_name: str | None = None
    languages: list[str] = field(default_factory=list)
    hi_policy: str = "exclude"      # include | exclude | only
    forced_policy: str = "exclude"  # include | exclude | only
    max_results: int = 30


@dataclass
class SubtitleResult:
    provider: str
    file_id: str
    language: str | None
    release: str = ""
    file_name: str = ""
    hash_match: bool = False
    hearing_impaired: bool = False
    forced: bool = False
    fps: float | None = None
    download_count: int | None = None
    rating: float | None = None
    from_trusted: bool = False
    ai_translated: bool = False
    machine_translated: bool = False
    uploader: str | None = None
    upload_date: str | None = None
    feature_title: str | None = None
    feature_year: int | None = None
    season: int | None = None
    episode: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    page_url: str | None = None
    format: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class DownloadedFile:
    name: str
    data: bytes
    remaining: int | None = None


class RateLimiter:
    """Janela deslizante simples: no máximo `per_minute` pedidos por minuto."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._ts: list[float] = []
        self._lock = threading.Lock()

    def acquire(self, wait: bool = True, max_wait: float = 65.0) -> None:
        deadline = time.monotonic() + max_wait
        while True:
            with self._lock:
                now = time.monotonic()
                self._ts = [t for t in self._ts if now - t < 60]
                if len(self._ts) < self.per_minute:
                    self._ts.append(now)
                    return
                sleep = 60 - (now - self._ts[0])
            if not wait or time.monotonic() + sleep > deadline:
                raise ProviderError("Limite de pedidos por minuto atingido (configurável em Fornecedores).", rate_limited=True,
                                    retry_after=int(sleep) + 1)
            time.sleep(min(sleep, 2))


def check_url_host(url: str, allowed_suffixes: tuple[str, ...]) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ProviderError("URL de download inválido (apenas HTTPS).")
    h = u.hostname.lower()
    if not any(h == s.lstrip(".") or h.endswith(s if s.startswith(".") else "." + s) for s in allowed_suffixes):
        raise ProviderError(f"Anfitrião de download não autorizado: {h}")


def fetch_limited(client: httpx.Client, url: str, *, headers: dict | None = None, allowed: tuple[str, ...],
                  max_bytes: int = MAX_DOWNLOAD_BYTES, max_redirects: int = 3) -> bytes:
    """GET com limite de tamanho, anfitriões permitidos e redirecionamentos validados manualmente."""
    for _ in range(max_redirects + 1):
        check_url_host(url, allowed)
        with client.stream("GET", url, headers=headers, follow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = str(httpx.URL(url).join(r.headers["location"]))
                continue
            if r.status_code >= 400:
                raise ProviderError(f"Download falhou (HTTP {r.status_code}).", rate_limited=r.status_code == 429)
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ProviderError("Ficheiro descarregado excede o tamanho máximo permitido.")
            return bytes(buf)
    raise ProviderError("Demasiados redirecionamentos.")


class Provider:
    """Interface a implementar por cada fornecedor."""

    name = "base"
    title = "Base"
    supports_hash = False
    homepage = ""
    secrets: tuple[str, ...] = ()

    def __init__(self, ctx: "ProviderContext"):
        self.ctx = ctx

    def configured(self) -> bool:  # tem credenciais mínimas?
        raise NotImplementedError

    def search(self, q: SearchQuery) -> list[SubtitleResult]:
        raise NotImplementedError

    def download(self, r: SubtitleResult) -> DownloadedFile:
        raise NotImplementedError

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class ProviderContext:
    get_secret: Callable[[str], str]
    get_config: Callable[[], dict]            # configuração do fornecedor (settings)
    record: Callable[..., None]               # record(ok=bool, error=str|None, remaining=int|None, auth_ok=bool|None, reset=str|None)
    http: Callable[[], httpx.Client]
    limiter: RateLimiter | None = None
