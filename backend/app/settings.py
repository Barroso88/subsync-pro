"""Definições de utilizador persistidas na BD (JSON por chave)."""
from __future__ import annotations

import copy
import json
from typing import Any

from .db import Database

DEFAULTS: dict[str, Any] = {
    # idiomas: priority = primary (têm de existir) | secondary (apenas preferência de pesquisa)
    "languages": [{"code": "pt-PT", "priority": "primary"}, {"code": "en", "priority": "secondary"}],
    "embedded_counts": True,       # legendas embutidas (texto) contam para a cobertura
    "prefer_forced": False,        # em pesquisas, preferir forçadas
    "hi_policy": "exclude",        # include | exclude | only  (SDH/HI)
    "prefer_full": True,
    "auto_download": False,
    "require_confirmation": True,
    "auto_min_tier": "hash",       # nível mínimo p/ download automático: hash | metadata
    "auto_max_per_run": 20,
    "scan_interval_minutes": 0,    # 0 = apenas manual
    "extra_languages": [],         # [{code,name}]
    "notify_plex_after_install": True,
    "plex": {"enabled": False, "url": "", "path_mappings": []},
    "radarr": {"enabled": False, "url": "", "path_mappings": []},
    "sonarr": {"enabled": False, "url": "", "path_mappings": []},
    "bazarr": {"enabled": False, "url": "", "path_mappings": []},
    "providers": {
        "opensubtitles": {"enabled": True, "priority": 1, "max_results": 30,
                          "requests_per_minute": 30, "daily_download_limit": 20},
        "subdl": {"enabled": False, "priority": 2, "max_results": 30,
                  "requests_per_minute": 20, "daily_download_limit": 50},
    },
    "generic_pt_variant": "",          # "" = «pt» sem variante não conta | "pt-PT" | "pt-BR": variante assumida para «pt» genérico (assinalada ≈pt)
    "embedded_variant_scan": False,    # LENTO: abre os vídeos (ffmpeg) para estimar pt-PT/pt-BR de faixas embutidas «pt». Só se ativado.
    "display_languages": {"hide_others": True, "keep": ["pt-PT", "pt-BR", "pt"]},   # só aparência: esconde chips/listas de legendas noutros idiomas (não apaga nada)
    "embedded_reference": True,        # ao descarregar, se não houver legenda externa de referência, extrair UMA faixa embutida (ffmpeg) para validar a sincronização
    "accept_pt_br_fallback": False,   # se faltar pt-PT, aceitar pt-BR (instalada como pt-BR, nunca renomeada)
    "translation": {
        "enabled": False,                  # permite o botão «Traduzir» nos resultados
        "auto_fallback": False,            # em lote: se não houver pt, traduzir a partir de inglês (fica «por confirmar»)
        "source_languages": ["en"],        # idiomas de origem aceites
        "engine": "deepl",                 # deepl | gemini
        "gemini_model": "",                # vazio = automático (Flash mais recente disponível para a chave)
    },
    "audio_sync": {"enabled": False},
}


class Settings:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict[str, Any]:
        out = copy.deepcopy(DEFAULTS)
        for r in self.db.query("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return out

    def get(self, key: str) -> Any:
        r = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        if r:
            try:
                return json.loads(r["value"])
            except ValueError:
                pass
        return copy.deepcopy(DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def update(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.set(k, v)

    def primary_langs(self) -> list[str]:
        return [l["code"] for l in self.get("languages") if l.get("priority") == "primary"]

    def all_langs(self) -> list[str]:
        return [l["code"] for l in self.get("languages")]
