"""Configuração de arranque (variáveis de ambiente) e segredos.

As definições de utilizador (idiomas, bibliotecas, fornecedores...) vivem na
base de dados; aqui só ficam coisas necessárias antes de a BD existir.
Segredos (API keys, tokens) vêm de variáveis de ambiente ou de um ficheiro
`secrets.json` (0600) na pasta de configuração. Nunca são devolvidos pela API.
"""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import threading
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class AppConfig:
    data_dir: Path = field(default_factory=lambda: Path(_env("SUBSYNC_DATA_DIR", "./data")))
    # Raízes dentro das quais as bibliotecas podem ser registadas (anti path-traversal).
    allowed_roots: list[str] = field(
        default_factory=lambda: [p for p in _env("SUBSYNC_ALLOWED_ROOTS", "/media").split(":") if p]
    )
    max_jobs: int = field(default_factory=lambda: _env_int("SUBSYNC_MAX_JOBS", 2))
    ffprobe_workers: int = field(default_factory=lambda: _env_int("SUBSYNC_FFPROBE_WORKERS", 4))
    ffprobe_bin: str = field(default_factory=lambda: _env("SUBSYNC_FFPROBE", "ffprobe"))
    ffmpeg_bin: str = field(default_factory=lambda: _env("SUBSYNC_FFMPEG", "ffmpeg"))
    auth_password: str = field(default_factory=lambda: _env("SUBSYNC_AUTH_PASSWORD"))
    log_level: str = field(default_factory=lambda: _env("SUBSYNC_LOG_LEVEL", "INFO"))
    static_dir: Path | None = field(
        default_factory=lambda: Path(_env("SUBSYNC_STATIC_DIR")) if _env("SUBSYNC_STATIC_DIR") else None
    )
    start_background: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "subsync.db"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def poster_dir(self) -> Path:
        return self.data_dir / "posters"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.staging_dir, self.backup_dir, self.poster_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


SECRET_KEYS = {
    "opensubtitles_api_key": "OPENSUBTITLES_API_KEY",
    "opensubtitles_username": "OPENSUBTITLES_USERNAME",
    "opensubtitles_password": "OPENSUBTITLES_PASSWORD",
    "subdl_api_key": "SUBDL_API_KEY",
    "deepl_api_key": "DEEPL_API_KEY",
    "webhook_token": "SUBSYNC_WEBHOOK_TOKEN",
    "plex_token": "PLEX_TOKEN",
    "radarr_api_key": "RADARR_API_KEY",
    "sonarr_api_key": "SONARR_API_KEY",
}


class Secrets:
    """Armazém de segredos: env tem prioridade sobre o ficheiro."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "secrets.json"
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                self._data = {}

    def get(self, key: str) -> str:
        env = SECRET_KEYS.get(key)
        if env and os.environ.get(env):
            return os.environ[env]
        return self._data.get(key, "")

    def source(self, key: str) -> str:
        env = SECRET_KEYS.get(key)
        if env and os.environ.get(env):
            return "env"
        return "file" if self._data.get(key) else "none"

    def set(self, key: str, value: str) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(key)
        with self._lock:
            if value:
                self._data[key] = value
            else:
                self._data.pop(key, None)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)

    def session_secret(self) -> bytes:
        with self._lock:
            sec = self._data.get("_session_secret")
            if not sec:
                sec = pysecrets.token_hex(32)
                self._data["_session_secret"] = sec
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh)
        return sec.encode()
