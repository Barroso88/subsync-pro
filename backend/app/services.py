"""Camada de serviço: orquestra BD, scanner, fornecedores, avaliação e instalação."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from stat import S_ISREG
from dataclasses import replace
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import coverage as cov
from . import ffprobe as fp
from .config import AppConfig, Secrets
from .db import Database, jloads, rows
from .evaluator import Evaluation, evaluate_content, evaluate_metadata
from .installer import (InstallConflict, InvalidDownload, build_dest_name, install, plan_install, stage, validate_download)
from .integrations.arr import Arr
from .integrations.bazarr import Bazarr
from .integrations.common import IntegrationError, map_remote_to_local
from .integrations.plex import Plex
from .jobs import JobCancelled, JobContext, JobManager, JobError
from .languages import normalize_lang
from .moviehash import moviehash
from .providers.base import ProviderContext, ProviderError, RateLimiter, SearchQuery, SubtitleResult
from .providers.registry import PROVIDERS
from .safepaths import PathSecurityError, is_within, require_within, sanitize_filename
from .scanner import Scanner, ScanAborted
from .settings import Settings
from .subtitles.parser import MAX_SUB_BYTES, ParsedSub, parse_bytes, decode_bytes, parse_srt
from . import subtitle_tools as st
from .translation import LANG_NAMES, TARGETS, DeepL, DeepLError, Gemini, count_chars, to_srt, translate_cues
from .util import add_event, log, now, scrub

TIER_ORDER = {"incompatible": 0, "weak": 1, "probable": 2, "hash": 3}


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, cfg: AppConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.db = Database(cfg.db_path)
        self.db.migrate()
        self.secrets = Secrets(cfg.data_dir)
        self.settings = Settings(self.db)
        self.transport = transport      # injeção para testes
        self.jobs = JobManager(self.db, cfg.max_jobs)
        self._ref_cache: dict = {}
        self._tr_lock = threading.Lock()
        self._origin_checked: dict[int, float] = {}
        self._ref_lock = threading.Lock()
        self.jobs.mark_interrupted()
        self.scanner = Scanner(self.db, self.settings, cfg.ffprobe_bin, cfg.ffprobe_workers, cfg.ffmpeg_bin)
        self._providers: dict[str, Any] = {}
        self._limiters: dict[str, RateLimiter] = {}
        self._stop = threading.Event()
        self._sched: threading.Thread | None = None
        self.jobs.register("scan", self._job_scan)
        self.jobs.register("batch", self._job_batch)
        self.jobs.register("sync_integrations", self._job_sync)
        self.jobs.register("refresh_plex", self._job_refresh_plex)
        self.jobs.register("revalidate", self._job_revalidate)
        self.jobs.register("webhook_import", self._job_webhook_import)
        self.jobs.register("bazarr_batch", self._job_bazarr_batch)
        self.jobs.register("convert_scan", self._job_convert_scan)
        self.jobs.register("audio_sync", self._job_audio_sync, lane="audio")      # leitura de áudio dos vídeos: uma de cada vez
        self.jobs.register("audio_batch", self._job_audio_batch, lane="audio")
        self.jobs.register("candidate_check", self._job_candidate_check, lane="audio")
        self.jobs.register("translate", self._job_translate, lane="translate")   # uma tradução de cada vez (limites dos serviços)
        self.jobs.register("convert_batch", self._job_convert_batch)

    # ---- ciclo de vida -----------------------------------------------------
    def start(self) -> None:
        if self.cfg.start_background:
            self._sched = threading.Thread(target=self._scheduler, name="scheduler", daemon=True)
            self._sched.start()

    def stop(self) -> None:
        self._stop.set()
        self.jobs.shutdown()
        self.db.close()

    def _scheduler(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(30):
            try:
                mins = int(self.settings.get("scan_interval_minutes") or 0)
                if mins > 0 and time.monotonic() - last >= mins * 60:
                    last = time.monotonic()
                    self.start_scan(None, scheduled=True)
            except Exception as e:  # noqa: BLE001
                log.error("scheduler: %s", scrub(str(e)))

    # ---- bibliotecas -------------------------------------------------------
    def libraries(self) -> list[dict]:
        out = []
        for r in self.db.query("SELECT * FROM libraries ORDER BY name"):
            d = dict(r)
            d["exists"] = os.path.isdir(d["path"])
            d["os_writable"] = os.access(d["path"], os.W_OK) if d["exists"] else False
            d["effective_write"] = bool(d["writable"]) and d["os_writable"]
            d["counts"] = {"media": self.db.scalar("SELECT COUNT(*) FROM media_items WHERE library_id=? AND present=1", (d["id"],))}
            out.append(d)
        return out

    def add_library(self, name: str, ltype: str, path: str, writable: bool) -> int:
        if ltype not in ("movies", "series"):
            raise ServiceError("Tipo de biblioteca inválido.")
        try:
            real = require_within(path, self.cfg.allowed_roots)
        except PathSecurityError:
            raise ServiceError(f"O caminho tem de estar dentro de: {', '.join(self.cfg.allowed_roots)}")
        if not os.path.isdir(real):
            raise ServiceError("Pasta inexistente.")
        if writable and not os.access(real, os.W_OK):
            raise ServiceError("A pasta está montada só de leitura (ou sem permissões) para este utilizador.")
        try:
            cur = self.db.execute("INSERT INTO libraries(name,type,path,writable) VALUES(?,?,?,?)",
                                  (name.strip()[:80] or Path(real).name, ltype, real, int(writable)))
        except Exception as e:  # noqa: BLE001
            if "UNIQUE" in str(e):
                raise ServiceError("Esta pasta já está registada.")
            raise
        return cur.lastrowid

    def update_library(self, lid: int, name: str | None, writable: bool | None) -> None:
        lib = self.db.one("SELECT * FROM libraries WHERE id=?", (lid,))
        if not lib:
            raise ServiceError("Biblioteca não encontrada.", 404)
        if writable and not os.access(lib["path"], os.W_OK):
            raise ServiceError("A pasta está montada só de leitura (ou sem permissões) para este utilizador.")
        if name is not None:
            self.db.execute("UPDATE libraries SET name=? WHERE id=?", (name.strip()[:80], lid))
        if writable is not None:
            self.db.execute("UPDATE libraries SET writable=? WHERE id=?", (int(writable), lid))

    def delete_library(self, lid: int) -> None:
        self.db.execute("DELETE FROM libraries WHERE id=?", (lid,))   # só BD; nada no disco

    def writable_roots(self) -> list[str]:
        return [l["path"] for l in self.libraries() if l["effective_write"]]

    # ---- scanner / tarefas -------------------------------------------------
    def start_scan(self, library_id: int | None, subpath: str | None = None, force: bool = False, scheduled: bool = False, force_subs: bool = False) -> int:
        title = "Análise " + ("agendada" if scheduled else "de bibliotecas")
        return self.jobs.submit("scan", title, {"library_id": library_id, "subpath": subpath, "force": force, "force_subs": force_subs}, dedupe=True)

    def _job_scan(self, ctx: JobContext, p: dict) -> dict:
        libs = [dict(r) for r in self.db.query("SELECT * FROM libraries" + (" WHERE id=?" if p.get("library_id") else ""),
                                                (p["library_id"],) if p.get("library_id") else ())]
        total: dict[str, int] = {}
        max_id_before = self.db.scalar("SELECT COALESCE(MAX(id),0) FROM media_items") or 0      # tudo o que tiver id maior foi descoberto NESTA análise
        for lib in libs:
            ctx.check_cancel()
            try:
                st = self.scanner.scan_library(lib, ctx, p.get("subpath"), bool(p.get("force")), bool(p.get("force_subs")))
            except ScanAborted as e:
                add_event(self.db, "scan", str(e), "error")
                raise
            for k, v in st.items():
                total[k] = total.get(k, 0) + v
        if self.settings.get("plex")["enabled"] or self.settings.get("radarr")["enabled"] or self.settings.get("sonarr")["enabled"]:
            try:
                self._sync_integrations(ctx)
            except IntegrationError as e:
                add_event(self.db, "integration", f"Sincronização de metadados: {e}", "warning")
        s = self.settings.all()
        if s["auto_download"] and any(p.get("enabled") for p in s["providers"].values()):
            # só os itens NOVOS desta análise (um filme que entrou agora), não os que já estavam em falta: esses procuram-se à mão em lote
            ids = [i for i in self.missing_media_ids(limit=1_000_000) if i > max_id_before][:int(s["auto_max_per_run"])]
            if ids:
                self.jobs.submit("batch", f"Download automático ({len(ids)} {'item novo' if len(ids) == 1 else 'itens novos'})",
                                 {"media_ids": ids, "install": not s["require_confirmation"], "min_tier": s["auto_min_tier"],
                                  "auto": True})
        return total

    # ---- integrações -------------------------------------------------------
    def _integration(self, name: str):
        cfg = self.settings.get(name)
        if not cfg["enabled"] or not cfg["url"]:
            return None
        if name == "plex":
            return Plex(cfg["url"], self.secrets.get("plex_token"), cfg.get("path_mappings", []), self.transport)
        if name == "bazarr":
            return Bazarr(cfg["url"], self.secrets.get("bazarr_api_key"), self.transport)
        return Arr(name, cfg["url"], self.secrets.get(f"{name}_api_key"), cfg.get("path_mappings", []), self.transport)

    def test_integration(self, name: str) -> dict:
        try:
            svc = self._integration(name)
            if svc is None:
                return {"ok": False, "message": "Integração desativada ou sem URL."}
            return svc.test()
        except IntegrationError as e:
            return {"ok": False, "message": str(e)}


    def _set_poster(self, mid: int, new: str | None, source: str) -> None:
        """Capa do Plex tem prioridade; as restantes ficam como alternativa. Muda a ordem => limpa a cache."""
        if not new:
            return
        cur = self.db.one("SELECT poster FROM media_items WHERE id=?", (mid,))
        old = (cur["poster"] or "") if cur else ""
        plex_ref = next((r for r in old.split("|") if r.startswith("plex:")), None)
        if source == "plex":
            rest = [r for r in old.split("|") if r and not r.startswith("plex:")]
            final = "|".join([new] + rest)
        else:
            final = "|".join(([plex_ref] if plex_ref else []) + [r for r in new.split("|") if r])
        if final != old:
            self.db.execute("UPDATE media_items SET poster=? WHERE id=?", (final, mid))
            for suffix in (".img", ".type", ".src"):
                try:
                    (self.cfg.poster_dir / f"{mid}{suffix}").unlink()
                except OSError:
                    pass

    def _sync_integrations(self, ctx: JobContext | None = None) -> dict:
        res = {"updated": 0}
        lookup = {r["path"]: r["id"] for r in self.db.query("SELECT id,path FROM media_items WHERE present=1")}

        def upd(path, **cols):
            mid = lookup.get(path)
            if not mid:
                return
            cols = {k: v for k, v in cols.items() if v is not None}
            if cols:
                sets = ",".join(f"{k}=?" for k in cols)
                self.db.execute(f"UPDATE media_items SET {sets} WHERE id=?", (*cols.values(), mid))
                res["updated"] += 1

        errors = []
        for name in ("radarr", "sonarr", "plex"):
            if ctx:
                ctx.check_cancel()
            svc = self._integration(name)
            if svc is None:
                continue
            try:
                if name == "radarr":
                    for m in svc.movies():
                        upd(m["path"], imdb_id=m["imdb_id"], tmdb_id=m["tmdb_id"], year=m["year"], radarr_id=m.get("id"), radarr_title=m.get("title"), added_at=m.get("added"),
                            imdb_rating=m.get("imdb_rating"), imdb_votes=m.get("imdb_votes"))
                        if lookup.get(m["path"]):
                            self._set_poster(lookup[m["path"]], m["poster"], "arr")
                elif name == "sonarr":
                    for e in svc.episodes():
                        upd(e["path"], imdb_id=e["imdb_id"], tmdb_id=e["tmdb_id"], tvdb_id=e["tvdb_id"], episode_title=e["episode_title"], added_at=e.get("added"))
                        if lookup.get(e["path"]):
                            self._set_poster(lookup[e["path"]], e["poster"], "arr")
                else:
                    for it in svc.items():
                        mid = lookup.get(it["path"])
                        if mid:
                            upd(it["path"], plex_key=it["rating_key"], added_at=it.get("added"))
                            self._set_poster(mid, it["poster"], "plex")
            except IntegrationError as e:
                errors.append(f"{name}: {e}")
        if errors:
            raise IntegrationError("; ".join(errors))
        add_event(self.db, "integration", f"Metadados sincronizados ({res['updated']} atualizações).")
        return res

    def sync_new_item(self, media_id: int) -> dict:
        """Metadados e capa de UM item (um ficheiro acabado de entrar), sem a análise completa: Radarr/Sonarr por id e Plex pelos adicionados
        mais recentemente. Nunca falha: erros ficam no registo."""
        m = self.db.one("SELECT * FROM media_items WHERE id=?", (media_id,))
        res = {"radarr": False, "sonarr": False, "plex": False}
        if not m:
            return res
        for name in ("radarr", "sonarr", "plex"):
            svc = self._integration(name)
            if svc is None:
                continue
            try:
                if name == "radarr" and m["kind"] == "movie" and m["tmdb_id"]:
                    r = svc.movie_by_tmdb(m["tmdb_id"])
                    if r:
                        self.db.execute("UPDATE media_items SET radarr_id=?, radarr_title=?, year=COALESCE(?,year), imdb_id=COALESCE(?,imdb_id), added_at=COALESCE(?,added_at), "
                                        "imdb_rating=COALESCE(?,imdb_rating), imdb_votes=COALESCE(?,imdb_votes) WHERE id=?",
                                        (r["id"], r["title"], r["year"], r["imdb_id"], r["added"], r.get("imdb_rating"), r.get("imdb_votes"), media_id))
                        self._set_poster(media_id, r["poster"], "arr")
                        res["radarr"] = True
                elif name == "sonarr" and m["kind"] == "episode" and m["tvdb_id"]:
                    self._set_poster(media_id, svc.series_poster(m["tvdb_id"]), "arr")
                    res["sonarr"] = True
                elif name == "plex":
                    for it in svc.items(recent=80):
                        if it["path"] == m["path"]:
                            self.db.execute("UPDATE media_items SET plex_key=?, added_at=COALESCE(?,added_at) WHERE id=?", (it["rating_key"], it.get("added"), media_id))
                            self._set_poster(media_id, it["poster"], "plex")
                            res["plex"] = True
                            break
            except IntegrationError as e:
                add_event(self.db, "integration", f"Metadados de «{m['title']}» ({name}): {e}", "warning", media_id)
        return res

    def _later_sync(self, media_id: int, delays: tuple[int, ...] = (120, 600)) -> None:
        """O Plex só conhece o ficheiro depois de o analisar: repete a sincronização do item mais tarde (a capa do Plex tem prioridade)."""
        def run(i: int) -> None:
            try:
                row = self.db.one("SELECT poster FROM media_items WHERE id=? AND present=1", (media_id,))
                if row is None or "plex:" in (row["poster"] or ""):
                    return
                if not self.sync_new_item(media_id)["plex"] and i + 1 < len(delays):
                    t = threading.Timer(delays[i + 1] - delays[i], run, (i + 1,))
                    t.daemon = True
                    t.start()
            except Exception as e:  # noqa: BLE001
                log.warning("sincronização tardia do item %s: %s", media_id, scrub(str(e)))
        t = threading.Timer(delays[0], run, (0,))
        t.daemon = True
        t.start()

    def _job_sync(self, ctx: JobContext, p: dict) -> dict:
        return self._sync_integrations(ctx)

    def _job_refresh_plex(self, ctx: JobContext, p: dict) -> dict:
        return self.refresh_plex(p["media_id"])

    def refresh_plex(self, media_id: int) -> dict:
        m = self.db.one("SELECT plex_key FROM media_items WHERE id=?", (media_id,))
        svc = self._integration("plex")
        if not svc or not m or not m["plex_key"]:
            return {"refreshed": False, "reason": "Plex não configurado ou item sem correspondência"}
        svc.refresh_item(m["plex_key"])
        return {"refreshed": True}

    def fetch_poster(self, media_id: int) -> tuple[bytes, str] | None:
        """Devolve (bytes, content-type) ou None; o motivo da falha fica em self.poster_error."""
        self.poster_error = ""
        self.poster_source = ""
        m = self.db.one("SELECT poster FROM media_items WHERE id=?", (media_id,))
        if not m or not m["poster"]:
            self.poster_error = "Sem capa associada (sincronize os metadados)."
            return None
        cache = self.cfg.poster_dir / f"{media_id}.img"
        ctype_file = cache.with_suffix(".type")
        src_file = cache.with_suffix(".src")
        if cache.exists():
            self.poster_source = src_file.read_text() if src_file.exists() else "desconhecida"
            return cache.read_bytes(), ctype_file.read_text() if ctype_file.exists() else "image/jpeg"
        from .integrations.common import fetch_public_image
        errors: list[str] = []
        for ref in str(m["poster"]).split("|"):          # ordem guardada: Plex → público → Radarr/Sonarr
            try:
                if ref.startswith("plex:"):
                    rel = ref[5:]
                    if not rel.startswith("/library/") or ".." in rel:
                        continue
                    svc = self._integration("plex")
                    if not svc:
                        errors.append("Plex desativado")
                        continue
                    data, ctype = svc.fetch_image(rel)
                elif ref.startswith("arr:"):
                    _, kind, rel = ref.split(":", 2)
                    if ".." in rel or not rel.startswith("/"):
                        continue
                    svc = self._integration(kind)
                    if not svc:
                        errors.append(f"{kind} desativado")
                        continue
                    data, ctype = svc.fetch_image(rel)
                elif ref.startswith("https://"):
                    data, ctype = fetch_public_image(ref, self.transport)
                else:
                    continue
            except IntegrationError as e:
                errors.append(str(e))
                continue
            self.poster_source = "plex" if ref.startswith("plex:") else "arr" if ref.startswith("arr:") else "tmdb/público"
            cache.write_bytes(data)
            ctype_file.write_text(ctype)
            src_file.write_text(self.poster_source)
            return data, ctype
        self.poster_error = "; ".join(errors) or "Capa indisponível."
        log.warning("capa %s: %s", media_id, scrub(self.poster_error))
        return None

    # ---- consultas ---------------------------------------------------------
    def media_dict(self, media_id: int) -> dict:
        r = self.db.one("SELECT * FROM media_items WHERE id=?", (media_id,))
        if not r:
            raise ServiceError("Item não encontrado.", 404)
        return dict(r)

    def missing_media_ids(self, limit: int = 1000) -> list[int]:
        c = cov.compute(self.db, self.settings)
        return [i for i, v in c.items() if v["missing"]][:limit]

    # ---- pesquisa de legendas ---------------------------------------------
    def provider(self, name: str):
        if name not in PROVIDERS:
            raise ServiceError("Fornecedor desconhecido.", 404)
        if name not in self._providers:
            cfg = lambda n=name: self.settings.get("providers").get(n, {})
            if name not in self._limiters or self._limiters[name].per_minute != cfg().get("requests_per_minute", 30):
                self._limiters[name] = RateLimiter(cfg().get("requests_per_minute", 30))
            ctx = ProviderContext(self.secrets.get, cfg, lambda **kw: self._record(name, **kw),
                                  lambda: httpx.Client(transport=self.transport, timeout=30, follow_redirects=False),
                                  self._limiters[name])
            self._providers[name] = PROVIDERS[name](ctx)
        return self._providers[name]

    def _record(self, name: str, ok: bool | None = None, error: str | None = None, remaining: int | None = None,
                auth_ok: bool | None = None, reset: str | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO provider_state(name) VALUES(?)", (name,))
        if error:
            self.db.execute("UPDATE provider_state SET last_error=?, last_error_at=? WHERE name=?", (scrub(error), now(), name))
        elif ok:
            self.db.execute("UPDATE provider_state SET last_ok_at=?, last_error=NULL WHERE name=?", (now(), name))
        if remaining is not None:
            self.db.execute("UPDATE provider_state SET remaining_quota=?, quota_reset=? WHERE name=?", (remaining, reset, name))
        if auth_ok is not None:
            self.db.execute("UPDATE provider_state SET auth_ok=? WHERE name=?", (int(auth_ok), name))

    def provider_status(self) -> list[dict]:
        out = []
        pcfg = self.settings.get("providers")
        for name, cls in PROVIDERS.items():
            p = self.provider(name)
            st = self.db.one("SELECT * FROM provider_state WHERE name=?", (name,))
            cfg = pcfg.get(name, {})
            today = date.today().isoformat()
            used = st["downloads_today"] if st and st["day"] == today else 0
            out.append({
                "name": name, "title": cls.title, "homepage": cls.homepage, "supports_hash": cls.supports_hash, "notice": getattr(cls, "notice", None),
                "enabled": cfg.get("enabled", False), "configured": p.configured(), "config": cfg,
                "secrets": {k: {"set": bool(self.secrets.get(k)), "source": self.secrets.source(k)} for k in cls.secrets},
                "downloads_today": used, "daily_limit": cfg.get("daily_download_limit"),
                "remaining_quota": st["remaining_quota"] if st else None, "quota_reset": st["quota_reset"] if st else None,
                "last_ok_at": st["last_ok_at"] if st else None, "last_error": st["last_error"] if st else None,
                "auth_ok": None if not st or st["auth_ok"] is None else bool(st["auth_ok"]),
            })
        return out

    def build_query(self, media: dict, languages: list[str] | None = None) -> SearchQuery:
        s = self.settings.all()
        langs = languages or [l["code"] for l in s["languages"]]
        if not languages and s["translation"]["enabled"]:
            langs = langs + [x for x in s["translation"]["source_languages"] if x not in langs]   # candidatas a traduzir
        pl: list[str] = []
        for l in langs:
            pl += ["pt-PT", "pt-BR"] if l == "pt" else [l]
        if s["accept_pt_br_fallback"] and "pt-PT" in pl and "pt-BR" not in pl:
            pl.append("pt-BR")
        if media["moviehash"] is None and os.path.isfile(media["path"]):
            try:
                h = moviehash(media["path"])
                if h:
                    media["moviehash"] = h
                    self.db.execute("UPDATE media_items SET moviehash=? WHERE id=?", (h, media["id"]))
            except OSError:
                pass
        return SearchQuery(
            kind=media["kind"], title=media["series_title"] if media["kind"] == "episode" else media["title"],
            year=media["year"] if media["kind"] == "movie" else None, season=media["season"], episode=media["episode"],
            series_title=media["series_title"], imdb_id=media["imdb_id"], tmdb_id=media["tmdb_id"],
            moviehash=media["moviehash"], file_name=media["file_name"], languages=pl, hi_policy=s["hi_policy"],
            forced_policy="include" if s["prefer_forced"] else "exclude",
            max_results=int(s["providers"]["opensubtitles"].get("max_results", 30)))

    def _bazarr_search(self, media: dict, q, s: dict) -> list[dict]:
        """Pesquisa manual no Bazarr (fornecedores dele) → candidatas no mesmo formato dos resultados da app, avaliadas pelas mesmas regras."""
        from .integrations.bazarr import BAZARR_TO_LANG
        svc = self._integration("bazarr")
        out = []
        for c in svc.search_movie(media["radarr_id"]):
            lang = BAZARR_TO_LANG.get(c["language"], c["language"])
            if q.languages and lang not in q.languages:
                continue
            r = SubtitleResult(provider="bazarr", file_id=c["subtitle"], language=lang, release=c["release"], file_name=c["release"],
                               hash_match="hash" in c["matches"], hearing_impaired=c["hi"], forced=c["forced"], found_by="Bazarr", via=c["provider"], uploader=c["uploader"],
                               extra={"hi": c["hi"], "forced": c["forced"], "original_format": c["original_format"], "bazarr_score": c["score"],
                                      "matches": c["matches"], "dont_matches": c["dont_matches"], "provider_label": self.PROVIDER_LABEL.get(c["provider"].lower(), c["provider"])})
            ev = evaluate_metadata(media, r, hi_policy=s["hi_policy"], prefer_forced=s["prefer_forced"])
            out.append({"result": r.to_dict(), "evaluation": ev.to_dict()})
        return out

    def search(self, media_id: int, languages: list[str] | None = None, sources: list[str] | None = None) -> dict:
        media = self.media_dict(media_id)
        s = self.settings.all()
        q = self.build_query(media, languages)
        results, errors = [], []
        run_app = sources is None or "app" in sources
        bz_future = bz_pool = None
        if self._integration("bazarr") is not None and (sources is None or "bazarr" in sources):
            if media["kind"] == "movie" and media.get("radarr_id"):
                from concurrent.futures import ThreadPoolExecutor      # em paralelo com a pesquisa da app: a do Bazarr demora (corre todos os fornecedores dele)
                bz_pool = ThreadPoolExecutor(max_workers=1)
                bz_future = bz_pool.submit(self._bazarr_search, media, q, s)
            elif sources is not None:
                errors.append({"provider": "bazarr", "message": "O Bazarr só pesquisa filmes com id do Radarr (sincronize os metadados do Radarr)."})
        steps = [("full", "hash + identificação" if q.moviehash else "identificação")]
        if q.moviehash and (q.imdb_id or q.tmdb_id or q.title):
            steps.append(("hash", "só hash do ficheiro"))
        steps.append(("title", "só título, sem ano"))
        tried: list[str] = []
        active = []
        for name, pcfg in sorted(s["providers"].items(), key=lambda kv: kv[1].get("priority", 99)):
            if not run_app or not pcfg.get("enabled") or name not in PROVIDERS:
                continue
            p = self.provider(name)
            if not p.configured():
                errors.append({"provider": name, "message": "Fornecedor sem credenciais configuradas.", "auth": True})
                continue
            active.append((name, p))

        def one(name: str, p) -> tuple[list[dict], list[str], list[dict]]:
            res_, tried_, err_ = [], [], []
            try:
                for mode, label in steps:
                    if mode == "hash" and not p.supports_hash:
                        continue
                    found = p.search(replace(q, mode=mode))
                    tried_.append(f"{name}: {label} → {len(found)}")
                    if not found:
                        continue
                    for r in found:
                        r.found_by = label
                        ev = evaluate_metadata(media, r, hi_policy=s["hi_policy"], prefer_forced=s["prefer_forced"])
                        res_.append({"result": r.to_dict(), "evaluation": ev.to_dict()})
                    break
            except ProviderError as e:
                err_.append({"provider": name, "message": str(e), "auth": e.auth, "rate_limited": e.rate_limited, "retry_after": e.retry_after})
            return res_, tried_, err_
        if len(active) > 1:
            # fornecedores em paralelo (cada um tem o seu próprio limite de ritmo): o tempo total passa a ser o do mais lento, não a soma
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(active)) as ex:
                outs = list(ex.map(lambda np_: one(*np_), active))
        else:
            outs = [one(*np_) for np_ in active]
        for res_, tried_, err_ in outs:                                # na ordem de prioridade
            results.extend(res_)
            tried.extend(tried_)
            errors.extend(err_)
        if bz_future is not None:
            try:
                found = bz_future.result()
                tried.append(f"bazarr: pesquisa manual → {len(found)}")
                results.extend(found)
            except (IntegrationError, ServiceError) as e:
                errors.append({"provider": "bazarr", "message": str(e)})
            finally:
                bz_pool.shutdown(wait=False)
        results.sort(key=lambda x: (-x["evaluation"]["score"], -(x["result"]["download_count"] or 0)))
        return {"media_id": media_id, "query": {"languages": q.languages, "hash_used": bool(q.moviehash), "tried": tried},
                "results": results, "errors": errors}

    # ---- download: preparar / instalar ------------------------------------
    def _quota_ok(self, name: str) -> None:
        cfg = self.settings.get("providers").get(name, {})
        lim = cfg.get("daily_download_limit")
        st = self.db.one("SELECT * FROM provider_state WHERE name=?", (name,))
        if lim and st and st["day"] == date.today().isoformat() and st["downloads_today"] >= lim:
            raise ServiceError(f"Limite diário de downloads ({lim}) atingido para {name}.", 429)

    def _count_download(self, name: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO provider_state(name) VALUES(?)", (name,))
        today = date.today().isoformat()
        self.db.execute("UPDATE provider_state SET downloads_today=CASE WHEN day=? THEN downloads_today+1 ELSE 1 END, day=? WHERE name=?",
                        (today, today, name))

    def _reference_for(self, media: dict, candidate_lang: str | None) -> tuple[str, ParsedSub] | None:
        subs = self.db.query("SELECT * FROM subtitles WHERE media_id=? AND kind='external' AND present=1 AND status='ok' "
                             "AND text_based=1 AND forced=0 AND association='certain' AND sync_status!='suspect' AND cue_count>=30",
                             (media["id"],))
        roots = [l["path"] for l in self.libraries()]
        subs = sorted(subs, key=lambda s: (s["lang"] == candidate_lang, s["sync_status"] != "plausible"))
        for s in subs:
            try:
                path = require_within(s["path"], roots)
                parsed = parse_bytes(Path(path).read_bytes(), Path(path).suffix.lower())
            except (OSError, PathSecurityError):
                continue
            if parsed and len(parsed.cues) >= 30:
                return os.path.basename(path), parsed
        return self._embedded_reference(media, candidate_lang)

    _REF_CACHE_MAX = 6

    def _embedded_reference(self, media: dict, candidate_lang: str | None) -> tuple[str, ParsedSub] | None:
        """Sem referência externa: extrai UMA faixa de texto embutida (só quando se descarrega uma legenda) e usa os seus tempos.
        Compara só tempos, por isso serve qualquer idioma. Em cache para não reabrir o vídeo a cada candidata."""
        if not self.settings.get("embedded_reference") or not media.get("path"):
            return None
        try:
            stt = os.stat(media["path"])
        except OSError:
            return None
        tracks = self.db.query("SELECT stream_index,lang,title FROM subtitles WHERE media_id=? AND kind='embedded' AND present=1 AND text_based=1 "
                               "AND forced=0 AND stream_index IS NOT NULL", (media["id"],))
        tracks = [t for t in tracks if "comment" not in (t["title"] or "").lower()]
        tracks.sort(key=lambda t: (t["lang"] != candidate_lang, t["lang"] != "en", t["stream_index"]))
        for t in tracks[:2]:
            hit = self._embedded_parsed(media, t["stream_index"], stt, fast=True)
            if hit:
                return f"faixa embutida #{t['stream_index']} ({t['lang'] or '?'})", hit
        return None

    REF_WINDOWS = ((0.08, 180), (0.22, 180), (0.38, 180), (0.54, 180), (0.70, 180), (0.86, 180))      # (posição no filme, duração em s)

    def _embedded_parsed(self, media: dict, stream_index: int, stt=None, fast: bool = False) -> "ParsedSub | None":
        """Extrai (ffmpeg, uma vez; em cache) uma faixa de texto embutida e devolve as falas, ou None se não for utilizável (menos de 30 falas).
        fast=True (avaliar um download): lê só 6 trechos de 3 min espalhados pelo filme (procura no ficheiro em vez de o ler todo; ≈5× mais rápido
        num filme de 6 GB) — chega para estimar offset e deriva. Sem duração conhecida, ou se os trechos não chegarem, lê a faixa toda."""
        try:
            stt = stt or os.stat(media["path"])
        except OSError:
            return None
        dur = float(media.get("duration") or 0)
        use_windows = fast and dur >= 20 * 60
        key = (media["id"], stream_index, stt.st_size, int(stt.st_mtime), use_windows)
        with self._ref_lock:
            hit = self._ref_cache.get(key)
        if hit is None:
            hit = False
            if use_windows:
                wins = [(round(dur * f, 1), float(d)) for f, d in self.REF_WINDOWS]
                data = fp.extract_subtitle_windows(media["path"], stream_index, wins, self.cfg.ffmpeg_bin)
                parsed = parse_bytes(data, ".srt") if data else None
                if parsed and len(parsed.cues) >= 30:
                    parsed.windows = [(int(a * 1000), int((a + d) * 1000)) for a, d in wins]
                    hit = parsed
            if not hit:
                data = fp.extract_subtitle_text(media["path"], stream_index, self.cfg.ffmpeg_bin)
                parsed = parse_bytes(data, ".srt") if data else None
                hit = parsed if parsed and len(parsed.cues) >= 30 else False
            with self._ref_lock:
                self._ref_cache[key] = hit
                while len(self._ref_cache) > self._REF_CACHE_MAX:
                    self._ref_cache.pop(next(iter(self._ref_cache)))
        return hit or None

    def prepare(self, media_id: int, result: dict, lang: str | None = None) -> dict:
        """Descarrega para staging, valida, avalia e calcula o destino. Não escreve na biblioteca."""
        media = self.media_dict(media_id)
        try:
            r = SubtitleResult(**{k: v for k, v in result.items() if k in SubtitleResult.__dataclass_fields__})
        except TypeError:
            raise ServiceError("Resultado inválido.")
        if r.provider not in PROVIDERS:
            raise ServiceError("Fornecedor desconhecido.")
        lang = normalize_lang(lang or r.language) or None
        self._quota_ok(r.provider)
        did = self.db.execute(
            "INSERT INTO downloads(media_id,provider,provider_file_id,lang,release,file_name,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (media_id, r.provider, str(r.file_id), lang, r.release[:200], r.file_name[:200], "preparing", now())).lastrowid
        try:
            prov = self.provider(r.provider)
            df = prov.download(r)
            self._count_download(r.provider)
            v = validate_download(df.name, df.data, media)
            s = self.settings.all()
            ev = evaluate_metadata(media, r, hi_policy=s["hi_policy"], prefer_forced=s["prefer_forced"])
            from .subtitles.parser import analyze
            an = analyze(v.parsed, media.get("duration"), size=len(df.data))
            if lang in (None, "pt"):
                from .languages import detect_language
                dl_, conf = detect_language(v.parsed.text)
                if dl_ in ("pt-PT", "pt-BR") and conf >= 0.5:
                    lang = dl_
                    ev.notes.append(f"Variante «{dl_}» estimada pelo conteúdo (o fornecedor não a indicou).")
            ev = evaluate_content(ev, media, v.parsed, an, self._reference_for(media, lang))
            return self._finish_prepare(did, media, r, lang, v, ev, an)
        except (ProviderError, InvalidDownload, ServiceError) as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e)), did))
            add_event(self.db, "download", f"Falhou ({r.provider} #{r.file_id}): {e}", "error", media_id)
            raise ServiceError(str(e), getattr(e, "status", 502 if isinstance(e, ProviderError) else 422))
        except Exception as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e))[:300], did))
            raise

    def upload_subtitle(self, media_id: int, filename: str, data: bytes, lang: str | None = None, forced: bool = False, hi: bool = False) -> dict:
        """Legenda do próprio utilizador: validada, avaliada com a mesma prova (referência) e deixada em «por confirmar» (staging); nada é escrito na biblioteca."""
        from .evaluator import Evaluation, Factor
        from .languages import detect_language
        from .naming import parse_subtitle_name
        from .subtitles.parser import analyze
        media = self.media_dict(media_id)
        filename = sanitize_filename(os.path.basename(filename or "legenda.srt")) or "legenda.srt"
        if not data:
            raise ServiceError("Ficheiro vazio.", 422)
        if len(data) > MAX_SUB_BYTES:
            raise ServiceError("Ficheiro demasiado grande (máx. 8 MB).", 413)
        name = parse_subtitle_name(Path(filename))
        chosen = normalize_lang(lang) if lang and lang != "auto" else None
        r = SubtitleResult(provider="upload", file_id=st.sha1(data)[:16], language=chosen or name.lang, release=Path(filename).stem, file_name=filename,
                           forced=bool(forced or name.forced), hearing_impaired=bool(hi or name.hi), format=Path(filename).suffix.lstrip(".").lower() or "srt", found_by="enviada por si")
        did = self.db.execute(
            "INSERT INTO downloads(media_id,provider,provider_file_id,lang,release,file_name,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (media_id, "upload", r.file_id, r.language, r.release[:200], filename[:200], "preparing", now())).lastrowid
        try:
            v = validate_download(filename, data, media)
            an = analyze(v.parsed, media.get("duration"), size=len(data))
            detected, conf = detect_language(v.parsed.text)
            lang_final = r.language
            ev = Evaluation(40, "user", "not_confirmed", [Factor("Enviada manualmente por si (sem avaliação de um fornecedor)", 0, "info")])
            if not lang_final and detected:
                lang_final = detected
                ev.notes.append(f"Idioma «{detected}» estimado pelo conteúdo (não o indicou): confirme.")
            elif lang_final in (None, "pt") and detected in ("pt-PT", "pt-BR") and conf >= 0.5:
                lang_final = detected
                ev.notes.append(f"Variante «{detected}» estimada pelo conteúdo.")
            elif lang_final and detected and conf >= 0.5 and normalize_lang(detected) != lang_final and not (lang_final.startswith("pt") and detected.startswith("pt")):
                ev.factors.append(Factor(f"O idioma indicado («{lang_final}») não parece corresponder ao conteúdo («{detected}»)", -15, "negative"))
            elif lang_final in ("pt-PT", "pt-BR") and detected in ("pt-PT", "pt-BR") and detected != lang_final and conf >= 0.5:
                ev.factors.append(Factor(f"Indicou «{lang_final}» mas o conteúdo parece «{detected}» (heurística): confirme antes de instalar", -10, "negative"))
            r.language = lang_final
            ev = evaluate_content(ev, media, v.parsed, an, self._reference_for(media, lang_final))
            ev.notes.append("Enviada por si: a app só valida o formato e compara tempos com uma referência, se existir. Não avalia a qualidade do texto.")
            add_event(self.db, "download", f"Legenda enviada pelo utilizador: {filename}", "info", media_id)
            return self._finish_prepare(did, media, r, lang_final, v, ev, an, {"upload": {"filename": filename, "detected_lang": detected}})
        except InvalidDownload as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e)), did))
            raise ServiceError(str(e), 422)
        except Exception as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e))[:300], did))
            raise

    def _finish_prepare(self, did: int, media: dict, r: SubtitleResult, lang: str | None, v, ev, an, extra: dict | None = None) -> dict:
        staged = stage(self.cfg.staging_dir, v)
        lib = self.db.one("SELECT * FROM libraries WHERE id=?", (media["library_id"],))
        subs = rows(self.db.query("SELECT * FROM subtitles WHERE media_id=?", (media["id"],)))
        try:
            plan = plan_install(media, lang, self.writable_roots(), subs, forced=r.forced, hi=r.hearing_impaired, ext=v.ext)
            dest, conflict, existing = plan.dest, plan.conflict, plan.existing
            writable_err = None
        except PathSecurityError as e:
            dest = os.path.join(media["dir"], build_dest_name(media["path"], lang, forced=r.forced, hi=r.hearing_impaired, ext=v.ext))
            conflict, existing, writable_err = None, [], str(e)
        evd = ev.to_dict()
        evd.update({"encoding": v.encoding, "writable_error": writable_err, "existing": existing,
                    "library": lib["name"] if lib else None, "cue_count": an.cue_count})
        if extra:
            evd.update(extra)
        self.db.execute(
            "UPDATE downloads SET status='prepared', lang=?, dest_path=?, score=?, evaluation=?, staged_path=?, conflict=?, error=NULL WHERE id=?",
            (lang, dest, ev.score, json.dumps(evd), staged, conflict, did))
        return self.get_download(did)

    # ---- tradução automática (DeepL) -----------------------------------------
    def _engine_name(self) -> str:
        e = self.settings.get("translation").get("engine")
        return e if e in ("deepl", "gemini") else "deepl"

    GEMINI_SLOTS = ("gemini_api_key", "gemini_api_key_2", "gemini_api_key_3")

    def _gemini_keys(self) -> list[str]:
        return [k for k in (self.secrets.get(n) for n in self.GEMINI_SLOTS) if k]

    def _engine_key(self) -> str:
        if self._engine_name() == "gemini":
            return (self._gemini_keys() or [""])[0]
        return self.secrets.get("deepl_api_key")

    def _deepl(self) -> "DeepL | Gemini":
        """Motor de tradução escolhido em Definições (o nome do método é histórico)."""
        name, key = self._engine_name(), self._engine_key()
        if not key:
            raise ServiceError(f"API key do {'Gemini' if name == 'gemini' else 'DeepL'} não configurada (Fornecedores → Tradução).", 400)
        if name == "gemini":
            ks = self._gemini_keys()
            g = Gemini(ks[0], self.settings.get("translation").get("gemini_model") or "", self.transport, extra_keys=ks[1:])
            g.address = {"informal": "informal", "formal": "formal"}.get(self.settings.get("translation").get("address") or "", "")
            return g
        return DeepL(key, self.transport)

    def _targets(self) -> list[str]:
        return list(LANG_NAMES if self._engine_name() == "gemini" else TARGETS)

    def translation_test(self) -> dict:
        """Uma tradução mínima para confirmar chave e modelo (consome uma fração de pedido)."""
        eng = self._deepl()
        if isinstance(eng, Gemini):
            eng.timeout, eng.deadline = 40.0, time.monotonic() + 90.0       # o teste não pode ficar minutos pendurado: falha com diagnóstico
        src = ["Good morning, how are you?", "Come on, we've got to get out of here!", "I can't believe you did that to me.",
               "Mind the gap, the bus is leaving.", "Don't be silly, give me your phone."]       # cumprimento, gíria, tratamento, vocabulário PT/BR (ecrã, autocarro, telemóvel)
        try:
            out = eng.translate(src, "pt-PT", "en")
        except DeepLError as e:
            raise ServiceError(str(e), 502)
        return {"ok": True, "engine": self._engine_name(), "model": getattr(eng, "model", None), "sample": out[0] if out else "",
                "stats": getattr(eng, "stats", None),
                "pairs": [{"src": a, "out": b} for a, b in zip(src, out)]}

    def gemini_models(self) -> dict:
        key = (self._gemini_keys() or [""])[0]
        if not key:
            raise ServiceError("API key do Gemini não configurada.", 400)
        try:
            names = Gemini(key, "", self.transport).list_models()
        except DeepLError as e:
            raise ServiceError(str(e), 502)
        return {"models": names, "suggested": Gemini.pick_flash(names)}

    def translation_status(self) -> dict:
        cfg = self.settings.get("translation")
        eng = self._engine_name()
        kname = "gemini_api_key" if eng == "gemini" else "deepl_api_key"
        st = {"enabled": bool(cfg["enabled"]), "auto_fallback": bool(cfg["auto_fallback"]), "configured": bool(self._engine_key()), "engine": eng,
              "model": (cfg.get("gemini_model") or "automático") if eng == "gemini" else None, "deepl_configured": bool(self.secrets.get("deepl_api_key")),
              "gemini_configured": bool(self._gemini_keys()),
              "gemini_keys": [{"slot": i + 1, "name": n, "set": bool(self.secrets.get(n)), "source": self.secrets.source(n),
                               "exhausted_until": (datetime.fromtimestamp(Gemini.exhausted_until(self.secrets.get(n)), timezone.utc).isoformat()
                                                   if self.secrets.get(n) and Gemini.exhausted_until(self.secrets.get(n)) else None)}
                              for i, n in enumerate(self.GEMINI_SLOTS)],
              "source": self.secrets.source(kname), "targets": self._targets(), "usage": None, "error": None, "plan": None}
        if st["configured"] and eng == "deepl":
            st["plan"] = "free" if self.secrets.get("deepl_api_key").endswith(":fx") else "pro"
            try:
                u = self._deepl().usage()
                st["usage"] = {"used": u.used, "limit": u.limit, "remaining": u.remaining}
            except DeepLError as e:
                st["error"] = str(e)
        return st

    def start_translate(self, media_id: int, result: dict, target: str) -> int:
        """Traduz em segundo plano (com progresso e cancelamento). Falhas óbvias (desativado, sem chave, idioma) recusam-se logo."""
        if not self.settings.get("translation")["enabled"]:
            raise ServiceError("A tradução automática está desativada (Definições → Idiomas).", 403)
        if target not in self._targets():
            raise ServiceError("Idioma de destino não suportado para tradução.")
        self._deepl()
        m = self.db.one("SELECT title FROM media_items WHERE id=?", (media_id,))
        if not m:
            raise ServiceError("Item não encontrado.", 404)
        return self.jobs.submit("translate", f"Traduzir legenda para {target}: {m['title']}", {"media_id": media_id, "result": result, "target": target}, dedupe=True)

    def _job_translate(self, ctx: JobContext, p: dict) -> dict:
        try:
            if p.get("embedded_sub_id"):
                return self.translate_embedded(p["media_id"], p["embedded_sub_id"], p["target"], ctx)
            return self.translate_prepare(p["media_id"], p["result"], p["target"], ctx)
        except ServiceError as e:
            raise JobError(str(e)) from e      # a mensagem chega à tarefa (e ao utilizador) tal como está

    def _translate_finish(self, did: int, media: dict, r: SubtitleResult, src: str | None, target: str, v, source_name: str, deepl, ctx: "JobContext | None",
                          reference: "tuple[str, Any] | None" = None, embedded: bool = False) -> dict:
        """Traduz as falas já validadas de `v`, revalida, avalia e deixa em «por confirmar». Usado pela legenda descarregada e pela faixa embutida."""
        from .subtitles.parser import analyze
        from .evaluator import Factor

        def prog(done: int, total: int) -> None:
            if ctx:
                if ctx.total == 0:
                    ctx.set_total(total)
                ctx.step("A traduzir…", inc=1, message=f"Bloco {done} de {total}", force=True)
        n_chars = count_chars(v.parsed.cues)
        eng_label = "Gemini" if self._engine_name() == "gemini" else "DeepL"
        usage = deepl.usage()
        if usage.limit and n_chars > usage.remaining:
            raise ServiceError(f"Quota {eng_label} insuficiente: a tradução precisa de ~{n_chars} caracteres e restam {usage.remaining}.", 429)
        # uma tradução de cada vez em toda a app (também as do lote automático): partilham os limites do serviço
        while not self._tr_lock.acquire(timeout=1.0):
            if ctx:
                ctx.step("Em espera por outra tradução…", inc=0, message="Em fila: há outra tradução em curso", force=True)
        try:
            cues, sent = translate_cues(v.parsed.cues, target, src, deepl, prog)
        finally:
            self._tr_lock.release()
        text = to_srt(cues)
        tv = validate_download("traduzida.srt", text.encode("utf-8"), media)   # revalida o resultado
        s = self.settings.all()
        ev = evaluate_metadata(media, r, hi_policy=s["hi_policy"], prefer_forced=s["prefer_forced"])
        an = analyze(tv.parsed, media.get("duration"), size=len(text))
        ev = evaluate_content(ev, media, tv.parsed, an, reference or self._reference_for(media, target))
        ev.score = max(0, ev.score - 15)
        ev.factors.append(Factor(f"Tradução automática ({eng_label}): qualidade inferior a uma legenda humana", -15, "negative"))
        kept = getattr(deepl, "untranslated", 0)
        if kept:
            ev.factors.append(Factor(f"{kept} falas ficaram no idioma original (recusadas pelo modelo)", -5, "negative"))
            ev.notes.append(f"{kept} falas não foram traduzidas pelo modelo e mantêm o idioma original: reveja.")
        left = getattr(deepl, "suspect_left", 0)
        fixed = getattr(deepl, "suspect_fixed", 0)
        if fixed:
            ev.notes.append(f"Verificação final: {fixed} falas que pareciam por traduzir ou truncadas foram repetidas e melhoraram.")
        if left:
            ev.factors.append(Factor(f"{left} falas continuam com ar de por traduzir ou truncadas", -3, "negative"))
            ev.notes.append(f"Mesmo depois de repetidas, {left} falas continuam parecidas com o original ou muito curtas: reveja-as (podem ser nomes, letras de música ou palavras iguais nos dois idiomas).")
        if r.forced:
            ev.notes.append("Faixa FORÇADA: só tem as partes em língua estrangeira. Fica guardada como «.forced» e não conta como legenda completa do idioma.")
        if embedded:
            ev.notes.append("Traduzida a partir de uma faixa de legenda embutida no próprio vídeo: os tempos são os dessa faixa (a «referência» é a própria faixa de origem, "
                            "por isso só prova que a tradução manteve os tempos, não que a faixa esteja sincronizada com o áudio).")
        ev.notes.append(f"Traduzida de «{src or 'idioma desconhecido'}» para «{target}»; os tempos são os da legenda original. "
                        "Itálicos e formatação foram removidos. Reveja antes de usar.")
        gst = getattr(deepl, "stats", None) or {}
        extra = {"translation": {"engine": self._engine_name(), "source_lang": src, "target": target, "characters": sent,
                                  "source_file": source_name, "embedded": embedded,
                                  **({"requests": gst.get("requests"), "invalid": gst.get("invalid"), "rate_limited": gst.get("rate_limited"),
                                      "suspect_fixed": getattr(deepl, "suspect_fixed", 0), "suspect_left": getattr(deepl, "suspect_left", 0)} if gst else {})}}
        add_event(self.db, "translation", f"Tradução {src or '?'}→{target} ({sent} caracteres, {eng_label})", "info", media["id"])
        return self._finish_prepare(did, media, r, target, tv, ev, an, extra)

    def start_translate_embedded(self, media_id: int, sub_id: int, target: str) -> int:
        """Traduz uma faixa de legenda de TEXTO embutida no vídeo (extração com ffmpeg só quando o utilizador pede)."""
        if not self.settings.get("translation")["enabled"]:
            raise ServiceError("A tradução automática está desativada (Definições → Idiomas).", 403)
        if target not in self._targets():
            raise ServiceError("Idioma de destino não suportado para tradução.")
        self._deepl()
        sub = self.db.one("SELECT * FROM subtitles WHERE id=? AND media_id=? AND kind='embedded' AND present=1", (sub_id, media_id))
        if not sub:
            raise ServiceError("Faixa embutida não encontrada.", 404)
        if not sub["text_based"] or sub["stream_index"] is None:
            raise ServiceError("Esta faixa é de imagem (PGS/VobSub): não pode ser traduzida como texto.", 422)
        if normalize_lang(sub["lang"]) == target:
            raise ServiceError("A faixa já está no idioma de destino.")
        m = self.db.one("SELECT title FROM media_items WHERE id=?", (media_id,))
        return self.jobs.submit("translate", f"Traduzir faixa embutida para {target}: {m['title'] if m else media_id}",
                                {"media_id": media_id, "embedded_sub_id": sub_id, "target": target}, dedupe=True)

    def translate_embedded(self, media_id: int, sub_id: int, target: str, ctx: "JobContext | None" = None) -> dict:
        media = self.media_dict(media_id)
        sub = self.db.one("SELECT * FROM subtitles WHERE id=? AND media_id=? AND kind='embedded' AND present=1", (sub_id, media_id))
        if not sub or not sub["text_based"] or sub["stream_index"] is None:
            raise ServiceError("Faixa embutida não encontrada ou não é de texto.", 404)
        if not self.settings.get("translation")["enabled"] or target not in self._targets():
            raise ServiceError("Tradução desativada ou idioma de destino não suportado.", 403)
        src = normalize_lang(sub["lang"])
        deepl = self._deepl()
        fname = f"{Path(media['path']).stem}.{src or 'und'}.srt"
        r = SubtitleResult(provider="embedded", file_id=f"stream{sub['stream_index']}", language=src, release=Path(media["path"]).stem, file_name=fname,
                           forced=bool(sub["forced"]), hearing_impaired=bool(sub["hi"]), format="srt", found_by="faixa embutida")
        did = self.db.execute(
            "INSERT INTO downloads(media_id,provider,provider_file_id,lang,release,file_name,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (media_id, "embedded", f"stream{sub['stream_index']}", target, r.release[:200], fname[:200], "preparing", now())).lastrowid
        try:
            if ctx:
                ctx.step("A extrair a faixa embutida (pode demorar em ficheiros grandes)…", inc=0, message="A ler a faixa de legenda do vídeo", force=True)
            data = fp.extract_subtitle_text(media["path"], sub["stream_index"], self.cfg.ffmpeg_bin)
            if not data:
                raise ServiceError("Não foi possível extrair a faixa (ffmpeg falhou, ficheiro inacessível ou faixa ilegível).", 422)
            v = validate_download(fname, data, media)
            ref = (f"faixa embutida #{sub['stream_index']} ({sub['lang'] or '?'})", v.parsed) if len(v.parsed.cues) >= 30 else None
            return self._translate_finish(did, media, r, src, target, v, f"faixa embutida #{sub['stream_index']}", deepl, ctx, reference=ref, embedded=True)
        except (InvalidDownload, ServiceError, DeepLError) as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e)), did))
            add_event(self.db, "translation", f"Tradução da faixa embutida falhou: {e}", "error", media_id)
            raise ServiceError(str(e), getattr(e, "status", 502 if isinstance(e, DeepLError) else 422))
        except Exception as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e))[:300], did))
            raise

    def translate_prepare(self, media_id: int, result: dict, target: str, ctx: "JobContext | None" = None) -> dict:
        """Descarrega a legenda de origem, traduz o texto (tempos intactos) e deixa-a «por confirmar»."""
        if not self.settings.get("translation")["enabled"]:
            raise ServiceError("A tradução automática está desativada (Definições → Idiomas).", 403)
        if target not in self._targets():
            raise ServiceError("Idioma de destino não suportado para tradução.")
        media = self.media_dict(media_id)
        try:
            r = SubtitleResult(**{k: v for k, v in result.items() if k in SubtitleResult.__dataclass_fields__})
        except TypeError:
            raise ServiceError("Resultado inválido.")
        if r.provider not in PROVIDERS:
            raise ServiceError("Fornecedor desconhecido.")
        src = normalize_lang(r.language)
        if src == target:
            raise ServiceError("A legenda já está no idioma de destino.")
        deepl = self._deepl()
        self._quota_ok(r.provider)
        did = self.db.execute(
            "INSERT INTO downloads(media_id,provider,provider_file_id,lang,release,file_name,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (media_id, r.provider, str(r.file_id), target, r.release[:200], r.file_name[:200], "preparing", now())).lastrowid
        try:
            if ctx:
                ctx.step("A descarregar a legenda original…", inc=0, message="A descarregar a legenda original", force=True)
            df = self.provider(r.provider).download(r)
            self._count_download(r.provider)
            v = validate_download(df.name, df.data, media)
            return self._translate_finish(did, media, r, src, target, v, df.name, deepl, ctx)
        except (ProviderError, InvalidDownload, ServiceError, DeepLError) as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e)), did))
            add_event(self.db, "translation", f"Tradução falhou: {e}", "error", media_id)
            st = getattr(e, "status", 502 if isinstance(e, (ProviderError, DeepLError)) else 422)
            raise ServiceError(str(e), st)
        except Exception as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e))[:300], did))
            raise

    def get_download(self, did: int) -> dict:
        r = self.db.one("SELECT d.*, m.title AS media_title, m.kind AS media_kind, m.series_title, m.season, m.episode "
                        "FROM downloads d LEFT JOIN media_items m ON m.id=d.media_id WHERE d.id=?", (did,))
        if not r:
            raise ServiceError("Download não encontrado.", 404)
        d = dict(r)
        d["evaluation"] = jloads(d["evaluation"])
        d.pop("staged_path", None)
        return d

    def install(self, did: int, policy: str = "fail", reindex: bool = True) -> dict:
        r = self.db.one("SELECT * FROM downloads WHERE id=?", (did,))
        if not r:
            raise ServiceError("Download não encontrado.", 404)
        if r["status"] != "prepared":
            raise ServiceError(f"Estado inválido para instalar: {r['status']}", 409)
        if policy not in ("fail", "replace", "keep_both"):
            raise ServiceError("Política de conflito inválida.")
        media = self.media_dict(r["media_id"])
        staged = r["staged_path"]
        if not staged or not is_within(staged, self.cfg.staging_dir):
            raise ServiceError("Ficheiro em preparação inválido.", 500)
        try:
            final = install(staged, r["dest_path"], self.writable_roots(), self.cfg.backup_dir, policy)
        except InstallConflict as e:
            raise ServiceError(f"Já existe um ficheiro em {e.dest}. Escolha substituir (com cópia de segurança) ou manter ambos.", 409)
        except (PathSecurityError, PermissionError, OSError, InvalidDownload) as e:
            self.db.execute("UPDATE downloads SET status='failed', error=? WHERE id=?", (scrub(str(e)), did))
            add_event(self.db, "download", f"Instalação falhou: {e}", "error", media["id"])
            raise ServiceError(str(e), 403 if isinstance(e, (PathSecurityError, PermissionError)) else 500)
        try:
            os.unlink(staged)
        except OSError:
            pass
        self.db.execute("UPDATE downloads SET status='installed', dest_path=?, installed_at=?, staged_path=NULL WHERE id=?",
                        (final, now(), did))
        self._origin_from_download(dict(r), final)
        add_event(self.db, "download", f"Legenda instalada: {os.path.basename(final)}", "info", media["id"])
        if reindex:
            lib = dict(self.db.one("SELECT * FROM libraries WHERE id=?", (media["library_id"],)))
            try:
                self.scanner.scan_library(lib, None, media["dir"], force=False)
            except Exception as e:  # noqa: BLE001
                log.warning("reanálise após instalar: %s", scrub(str(e)))
            self._pipeline_after_install(media, final)
            if self.settings.get("notify_plex_after_install"):
                try:
                    self.refresh_plex(media["id"])
                except IntegrationError as e:
                    add_event(self.db, "integration", f"Plex não notificado: {e}", "warning", media["id"])
        return self.get_download(did)

    # ---- pipeline automático ao instalar (só legendas novas; cada passo tem backup e é restaurável) ----
    PIPELINE_STEPS = ("convert", "clean", "hi", "durations", "reflow", "audio")

    def _pipeline_after_install(self, media: dict, final: str) -> list[str]:
        cfg = self.settings.get("pipeline") or {}
        if not cfg.get("enabled"):
            return []
        try:
            return self._pipeline(media, final, cfg)
        except Exception as e:  # noqa: BLE001 - o pipeline nunca pode estragar uma instalação que correu bem
            log.warning("pipeline após instalar: %s", scrub(str(e)))
            add_event(self.db, "edit", f"Pipeline automático falhou em {os.path.basename(final)}: {e}", "warning", media["id"])
            return []

    def _pipeline(self, media: dict, final: str, cfg: dict) -> list[str]:
        row = self.db.one("SELECT id FROM subtitles WHERE path=? AND present=1 AND kind='external'", (final,))
        if not row:
            return []
        sid = row["id"]
        done: list[str] = []
        eids: list[int] = []

        def run(action: str, params: dict, label: str) -> None:
            nonlocal sid
            try:
                pv = self.sub_preview(sid, action, params)
            except ServiceError as e:
                if e.status not in (422, 400):
                    done.append(f"{label}: não foi possível ({e})")
                return                                              # 422 = nada a alterar
            res = self.sub_apply(sid, action, params, pv["base_hash"])
            if res.get("edit_id"):
                eids.append(res["edit_id"])
            if action == "convert" and res.get("target"):
                new = self.db.one("SELECT id FROM subtitles WHERE path=? AND present=1", (os.path.join(os.path.dirname(final), res["target"]),))
                if new:
                    sid = new["id"]
            n = (pv.get("stats") or {}).get("changed")
            done.append(label + (f" ({n} falas)" if n else ""))

        if cfg.get("convert", True):
            run("convert", {"fix_mojibake": True, "keep_original": False}, "convertida para SRT/UTF-8")
        sub = self.db.one("SELECT path FROM subtitles WHERE id=? AND present=1", (sid,))
        if not sub or not sub["path"].lower().endswith(".srt"):
            self._merge_pipeline_edits(eids, done)
            return self._pipeline_report(media, final, done)
        if cfg.get("clean", True) or cfg.get("hi"):
            run("textfix", {"common": bool(cfg.get("clean", True)), "hi": bool(cfg.get("hi")), "style": False, "uppercase": False}, "texto limpo")
        if cfg.get("durations"):
            run("durations", {}, "durações ajustadas")
        if cfg.get("reflow"):
            run("reflow", {}, "linhas reformatadas")
        self._merge_pipeline_edits(eids, done)
        if cfg.get("audio", True) and media.get("duration"):
            try:
                self.start_audio_sync(sid)
                done.append("verificação com o áudio em fila (o resultado fica à espera da sua aprovação)")
            except ServiceError:
                pass
        return self._pipeline_report(media, final, done)

    def _merge_pipeline_edits(self, eids: list[int], labels: list[str]) -> None:
        """Uma só cópia para toda a cadeia: fica a do original (1.º passo); as cópias intermédias e as linhas de histórico dos passos seguintes são removidas.
        Restaurar repõe o original (e, se houve conversão para outro ficheiro, remove o .srt criado)."""
        if len(eids) < 2:
            return
        first = self.db.one("SELECT * FROM subtitle_edits WHERE id=?", (eids[0],))
        last = self.db.one("SELECT * FROM subtitle_edits WHERE id=?", (eids[-1],))
        if not first or not last:
            return
        params = jloads(first["params"], {}) or {}
        params["pipeline"] = [re.sub(r" \(\d+ falas\)$", "", l) for l in labels if "não foi possível" not in l and "fila" not in l]
        self.db.execute("UPDATE subtitle_edits SET after_hash=?, params=? WHERE id=?", (last["after_hash"], json.dumps(params), first["id"]))
        for eid in eids[1:]:
            e = self.db.one("SELECT backup_path FROM subtitle_edits WHERE id=?", (eid,))
            if e and e["backup_path"] and is_within(e["backup_path"], self.cfg.backup_dir) and e["backup_path"] != first["backup_path"]:
                try:
                    os.unlink(e["backup_path"])
                except OSError:
                    pass
            self.db.execute("DELETE FROM subtitle_edits WHERE id=?", (eid,))

    def _pipeline_report(self, media: dict, final: str, done: list[str]) -> list[str]:
        if done:
            add_event(self.db, "edit", f"Pipeline automático em {os.path.basename(final)}: " + "; ".join(done) + ". Fica uma só cópia do original em «Alterações a legendas» (restaurável).", "info", media["id"])
        return done

    # ---- origem das legendas (app / Bazarr / enviada / traduzida) ------------------------
    PROVIDER_LABEL = {"opensubtitles": "OpenSubtitles", "opensubtitlescom": "OpenSubtitles.com", "opensubtitlesorg": "OpenSubtitles.org", "subdl": "SubDL", "subtitlecat": "Subtitlecat",
                      "legendasdivx": "LegendasDivx", "legendastv": "Legendas.tv", "legendasnet": "Legendas.net", "podnapisi": "Podnapisi", "pipocas": "Pipocas.tv",
                      "addic7ed": "Addic7ed", "subscene": "Subscene", "yifysubtitles": "YIFY Subtitles", "deepl": "DeepL", "gemini": "Gemini"}

    def _record_origin(self, path: str, media_id: int, origin: str, provider: str | None, detail: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO sub_origins(path,media_id,origin,provider,detail,at) VALUES(?,?,?,?,?,?)", (path, media_id, origin, provider, detail, now()))

    def _origin_from_download(self, r: dict, final: str) -> None:
        ev = jloads(r.get("evaluation"), {}) or {}
        mid = r["media_id"]
        if ev.get("translation"):
            self._record_origin(final, mid, "translation", ev["translation"].get("engine"),
                                f"traduzida de {ev['translation'].get('source_lang') or '?'}" + (" (faixa embutida)" if ev["translation"].get("embedded") else ""))
        elif r["provider"] == "upload":
            self._record_origin(final, mid, "upload", None, None)
        else:
            self._record_origin(final, mid, "app", r["provider"], f"score {r['score']}" if r.get("score") is not None else None)

    def _origin_view(self, row: dict) -> dict:
        p = row["provider"]
        return {"origin": row["origin"], "provider": p, "provider_label": self.PROVIDER_LABEL.get((p or "").lower(), p), "detail": row["detail"], "at": row["at"]}

    def _bazarr_attribute(self, media: dict, svc=None, force_files: set[str] | None = None) -> int:
        """Liga ficheiros do filme ao fornecedor que o Bazarr diz ter usado (pelo nome do ficheiro) e guarda. Devolve quantos novos."""
        svc = svc or self._integration("bazarr")
        if svc is None or media.get("kind") != "movie" or not media.get("radarr_id"):
            return 0
        subs = self.db.query("SELECT path FROM subtitles WHERE media_id=? AND kind='external' AND present=1", (media["id"],))
        known = {r["path"] for r in self.db.query("SELECT path FROM sub_origins WHERE media_id=?", (media["id"],))}
        todo = {os.path.basename(s["path"]): s["path"] for s in subs if s["path"] not in known or os.path.basename(s["path"]) in (force_files or set())}
        if not todo:
            return 0
        n = 0
        try:
            for e in svc.movie_history(media["radarr_id"]):          # do mais recente para o mais antigo: o primeiro de cada ficheiro é o que o criou por último
                path = todo.pop(e["file"], None)
                if path:
                    self._record_origin(path, media["id"], "bazarr", e["provider"], f"score {e['score']}" if e.get("score") else None)
                    n += 1
        except IntegrationError as ex:
            log.info("origens (Bazarr): %s", scrub(str(ex)))
        return n

    def media_origins(self, media_id: int) -> list[dict]:
        media = self.media_dict(media_id)
        # instalações feitas pela app antes de existir este registo: o histórico de downloads já sabe de onde vieram
        known = {r["path"] for r in self.db.query("SELECT path FROM sub_origins WHERE media_id=?", (media_id,))}
        for d in self.db.query("SELECT * FROM downloads WHERE media_id=? AND status='installed' AND dest_path IS NOT NULL ORDER BY id", (media_id,)):
            if d["dest_path"] not in known and os.path.isfile(d["dest_path"]):
                self._origin_from_download(dict(d), d["dest_path"])
                known.add(d["dest_path"])
        if time.monotonic() - self._origin_checked.get(media_id, -1e9) > 600:      # no máximo 1 pergunta ao Bazarr por filme a cada 10 min
            self._origin_checked[media_id] = time.monotonic()
            self._bazarr_attribute(media)
        out = []
        for s in self.db.query("SELECT id,path FROM subtitles WHERE media_id=? AND kind='external' AND present=1", (media_id,)):
            o = self.db.one("SELECT * FROM sub_origins WHERE path=?", (s["path"],))
            if o:
                out.append({"subtitle_id": s["id"], **self._origin_view(dict(o))})
        return out

    def discard(self, did: int) -> None:
        r = self.db.one("SELECT * FROM downloads WHERE id=?", (did,))
        if not r:
            raise ServiceError("Download não encontrado.", 404)
        if r["staged_path"] and is_within(r["staged_path"], self.cfg.staging_dir):
            try:
                os.unlink(r["staged_path"])
            except OSError:
                pass
        if r["status"] in ("prepared", "failed"):
            self.db.execute("UPDATE downloads SET status='discarded', staged_path=NULL WHERE id=?", (did,))

    # ---- lote ---------------------------------------------------------------
    def batch_preview(self, media_ids: list[int], languages: list[str] | None = None) -> dict:
        c = cov.compute(self.db, self.settings, media_ids)
        primary = languages or self.settings.primary_langs()
        pairs = sum(len([l for l in primary if l in v["missing"] or languages]) for v in c.values())
        needing = [i for i, v in c.items() if (v["missing"] if not languages else [l for l in languages if l not in v["covered"]])]
        st = {p["name"]: p for p in self.provider_status()}
        return {"selected": len(media_ids), "needing_subtitles": len(needing), "language_pairs": pairs,
                "languages": primary, "estimated_requests": len(needing) * 2,
                "providers": [{"name": n, "downloads_today": p["downloads_today"], "daily_limit": p["daily_limit"],
                               "configured": p["configured"]} for n, p in st.items() if p["enabled"]],
                "days_estimate": next((max(1, -(-len(needing) // p["daily_limit"])) for p in st.values() if p["enabled"] and p["daily_limit"] and needing), None),
                "require_confirmation": self.settings.get("require_confirmation")}

    def start_batch(self, media_ids: list[int], languages: list[str] | None, install_now: bool, min_tier: str = "probable") -> int:
        if not media_ids:
            raise ServiceError("Nenhum item selecionado.")
        if install_now and not self.writable_roots():
            raise ServiceError("Nenhuma biblioteca tem escrita autorizada.", 403)
        return self.jobs.submit("batch", f"Procurar legendas ({len(media_ids)} itens)",
                                {"media_ids": media_ids, "languages": languages, "install": install_now, "min_tier": min_tier})

    def _job_batch(self, ctx: JobContext, p: dict) -> dict:
        ids = p["media_ids"]
        ctx.set_total(len(ids))
        c = cov.compute(self.db, self.settings, ids)
        out = {"installed": 0, "prepared": 0, "no_match": 0, "errors": 0, "skipped_conflict": 0, "details": []}
        min_rank = TIER_ORDER.get(p.get("min_tier", "probable"), 2)
        for mid in ids:
            ctx.check_cancel()
            media = self.db.one("SELECT title FROM media_items WHERE id=?", (mid,))
            ctx.step(media["title"] if media else str(mid), message=f"{out['installed']} instaladas · {out['prepared']} por confirmar")
            want = p.get("languages") or c.get(mid, {}).get("missing") or []
            want = [l for l in want if l not in c.get(mid, {}).get("covered", {})]
            for lang in want:
                ctx.check_cancel()
                try:
                    res = self.search(mid, [lang])
                except ServiceError as e:
                    out["errors"] += 1
                    out["details"].append({"media_id": mid, "lang": lang, "error": str(e)})
                    continue
                for e in res["errors"]:
                    if e.get("rate_limited") or e.get("auth"):
                        out["details"].append({"media_id": mid, "lang": lang, "error": e["message"]})
                        if e.get("auth"):
                            raise ServiceError(e["message"])
                allowed = {lang} | ({"pt-BR"} if lang == "pt-PT" and self.settings.get("accept_pt_br_fallback") else set())
                good = [x for x in res["results"] if x["result"]["language"] in allowed and not x["result"]["forced"]
                        and TIER_ORDER[x["evaluation"]["compat"]] >= min_rank and x["evaluation"]["compat"] != "incompatible"]
                good.sort(key=lambda x: x["result"]["language"] != lang)   # idioma pedido primeiro; fallback só se não houver
                if not good and self._can_auto_translate(lang):
                    if self._batch_translate(mid, lang, out):
                        continue
                if not good:
                    out["no_match"] += 1
                    continue
                best = good[0]
                try:
                    d = self.prepare(mid, best["result"], lang)
                except ServiceError as e:
                    out["errors"] += 1
                    out["details"].append({"media_id": mid, "lang": lang, "error": str(e)})
                    if e.status == 429:
                        raise
                    continue
                if d["evaluation"]["compat"] == "incompatible" or d["evaluation"]["sync"] == "suspect":
                    self.discard(d["id"])
                    out["no_match"] += 1
                    continue
                if not p.get("install") or d["conflict"] or d["evaluation"].get("writable_error"):
                    out["prepared"] += 1
                    if d["conflict"]:
                        out["skipped_conflict"] += 1
                    continue
                try:
                    self.install(d["id"], "fail")
                    out["installed"] += 1
                except ServiceError as e:
                    out["errors"] += 1
                    out["details"].append({"media_id": mid, "lang": lang, "error": str(e)})
        out["details"] = out["details"][:50]
        add_event(self.db, "batch", f"Lote concluído: {out['installed']} instaladas, {out['prepared']} por confirmar, {out['no_match']} sem resultado adequado.")
        return out

    def _can_auto_translate(self, lang: str) -> bool:
        t = self.settings.get("translation")
        return bool(t["enabled"] and t["auto_fallback"] and lang in self._targets() and self._engine_key())

    def _batch_translate(self, mid: int, lang: str, out: dict) -> bool:
        """Sem legenda no idioma pedido: traduz a melhor de um idioma de origem. Fica SEMPRE «por confirmar»."""
        sources = self.settings.get("translation")["source_languages"]
        try:
            res = self.search(mid, sources)
        except ServiceError:
            return False
        cand = [x for x in res["results"] if x["result"]["language"] in sources and not x["result"]["forced"]
                and TIER_ORDER[x["evaluation"]["compat"]] >= TIER_ORDER["probable"]]
        if not cand:
            return False
        try:
            self.translate_prepare(mid, cand[0]["result"], lang)
        except ServiceError as e:
            out["errors"] += 1
            out["details"].append({"media_id": mid, "lang": lang, "error": str(e)})
            if e.status == 429:
                raise
            return True
        out["prepared"] += 1
        out["translated"] = out.get("translated", 0) + 1
        return True

    # ---- webhooks (Radarr/Sonarr) -------------------------------------------
    def webhook_token(self) -> str:
        tok = self.secrets.get("webhook_token")
        if not tok:
            import secrets as _s
            tok = _s.token_urlsafe(24)
            self.secrets.set("webhook_token", tok)
        return tok

    def handle_webhook(self, source: str, payload: dict) -> dict:
        """Radarr/Sonarr avisam que importaram um ficheiro: analisa só essa pasta e (se ativo) procura a legenda."""
        ev = str(payload.get("eventType", ""))
        if ev == "Test":
            add_event(self.db, "webhook", f"{source.title()}: teste de webhook recebido (ligação e token OK).", "info")
            return {"ok": True, "test": True}
        if ev not in ("Download", "MovieFileImported", "Upgrade", "EpisodeFileImported"):
            return {"ok": True, "ignored": ev or "sem eventType"}
        f = payload.get("movieFile") if source == "radarr" else payload.get("episodeFile")
        remote = (f or {}).get("path")
        if not remote:
            rel = (f or {}).get("relativePath")
            base = (payload.get("movie") or payload.get("series") or {}).get("folderPath") or (payload.get("movie") or {}).get("path") or (payload.get("series") or {}).get("path")
            remote = f"{base.rstrip('/')}/{rel}" if base and rel else None
        if not remote or not isinstance(remote, str):
            raise ServiceError("Payload sem caminho do ficheiro.", 422)
        mappings = (self.settings.get(source) or {}).get("path_mappings", [])
        local = map_remote_to_local(remote, mappings)
        meta = payload.get("movie") or payload.get("series") or {}
        jid = self.jobs.submit("webhook_import", f"Novo ficheiro ({source.title()}): {os.path.basename(local)}",
                               {"path": local, "source": source, "remote": remote,
                                "imdb_id": meta.get("imdbId"), "tmdb_id": meta.get("tmdbId"), "tvdb_id": meta.get("tvdbId")}, dedupe=True)
        return {"ok": True, "job_id": jid}

    def _job_webhook_import(self, ctx: JobContext, p: dict) -> dict:
        local = p["path"]
        roots = [l["path"] for l in self.libraries()]
        try:
            real = require_within(local, roots)
        except PathSecurityError:
            raise ServiceError(f"O caminho «{local}» não está em nenhuma biblioteca. Verifique o mapeamento de caminhos de {p['source'].title()} (remoto → local).")
        if not os.path.isfile(real):
            raise ServiceError(f"Ficheiro não encontrado: {real} (mapeamento de caminhos errado?).")
        lib = next(l for l in self.libraries() if is_within(real, l["path"]))
        ctx.set_total(2)
        self.scanner.scan_library(lib, ctx, os.path.dirname(real))
        ctx.step(os.path.basename(real), message="analisado")
        m = self.db.one("SELECT id FROM media_items WHERE path=?", (real,))
        if not m:
            raise ServiceError("O ficheiro não foi reconhecido como vídeo.")
        cols = {k: p.get(k) for k in ("imdb_id", "tmdb_id", "tvdb_id") if p.get(k)}
        if cols:
            self.db.execute(f"UPDATE media_items SET {','.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), m["id"]))
        add_event(self.db, "webhook", f"{p['source'].title()}: novo ficheiro {os.path.basename(real)}", "info", m["id"])
        synced = self.sync_new_item(m["id"])                       # capa, título do Radarr, ids… sem esperar pela análise completa
        if self.settings.get("plex")["enabled"] and not synced["plex"]:
            self._later_sync(m["id"])
        s = self.settings.all()
        res: dict = {"media_id": m["id"], "searched": False, "synced": synced}
        if s["auto_download"] and any(x.get("enabled") for x in s["providers"].values()):
            res["batch"] = self._job_batch(ctx, {"media_ids": [m["id"]], "install": not s["require_confirmation"], "min_tier": s["auto_min_tier"], "auto": True})
            res["searched"] = True
        ctx.step(os.path.basename(real), message="concluído", force=True)
        return res

    # ---- Bazarr: pedir-lhe que procure/descarregue (inclui os fornecedores dele, ex.: LegendasDivx) ----
    BAZARR_WAIT = 45.0      # segundos a esperar que o Bazarr registe o download (a API devolve logo e ele trabalha em segundo plano)
    BAZARR_POLL = 3.0

    @staticmethod
    def _hist_keys(entries: list[dict]) -> set[tuple]:
        return {(e["file"], e["provider"], e.get("at"), e.get("score")) for e in entries}

    def bazarr_request(self, media_id: int, lang: str) -> dict:
        svc = self._integration("bazarr")
        if svc is None:
            raise ServiceError("Integração com o Bazarr desativada (Definições → Bazarr).", 400)
        media = self.media_dict(media_id)
        if media["kind"] != "movie" or not media.get("radarr_id"):
            raise ServiceError("Só filmes com id do Radarr (sincronize os metadados do Radarr).", 422)
        lib = self.db.one("SELECT * FROM libraries WHERE id=?", (media["library_id"],))
        if not any(l["id"] == media["library_id"] and l["effective_write"] for l in self.libraries()):
            raise ServiceError("A biblioteca deste filme está em modo só de leitura: não se pede ao Bazarr que escreva nela.", 403)
        had = lang in cov.compute(self.db, self.settings, [media_id])[media_id]["covered"]
        try:
            before = self._hist_keys(svc.movie_history(media["radarr_id"]))
        except IntegrationError:
            before = None
        try:
            r = svc.download_movie_subtitle(media["radarr_id"], lang)
        except IntegrationError as e:
            add_event(self.db, "bazarr", f"Bazarr ({media['title']}, {lang}): {e}", "warning", media_id)
            raise ServiceError(str(e), 502)
        registered = None
        if r.get("ok") and before is not None:
            # o Bazarr responde logo (204) e descarrega em segundo plano: espera que apareça um download novo no histórico dele antes de reanalisar
            deadline = time.monotonic() + self.BAZARR_WAIT
            while True:
                try:
                    registered = self._hist_keys(svc.movie_history(media["radarr_id"])) - before
                except IntegrationError:
                    break
                if registered or time.monotonic() >= deadline:
                    break
                time.sleep(self.BAZARR_POLL)
            if registered:
                time.sleep(min(2.0, self.BAZARR_WAIT))                # deixa o Bazarr acabar de gravar o ficheiro
        try:  # a app valida o resultado: reanalisa só a pasta do filme
            self.scanner.scan_library(dict(lib), None, media["dir"])
        except Exception as e:  # noqa: BLE001
            log.warning("reanálise após Bazarr: %s", scrub(str(e)))
        now_cov = cov.compute(self.db, self.settings, [media_id])[media_id]
        found = lang in now_cov["covered"] and not had
        self._bazarr_attribute(media, svc)
        src = None
        if found:
            newest = self.db.one("SELECT o.* FROM sub_origins o JOIN subtitles s ON s.path=o.path WHERE o.media_id=? AND o.origin='bazarr' AND s.present=1 "
                                 "ORDER BY o.at DESC LIMIT 1", (media_id,))
            src = self._origin_view(dict(newest)) if newest else None
        if found:
            msg = "Legenda encontrada e validada" + (f" (Bazarr · {src['provider_label']}{', ' + src['detail'] if src['detail'] else ''})" if src else "") + "."
        elif not r.get("ok"):
            msg = f"O Bazarr não encontrou: {r.get('message')}"
        elif had:
            msg = f"Já tinha {lang}; o Bazarr não trouxe nenhuma legenda diferente."
        elif registered:
            msg = "O Bazarr descarregou uma legenda mas não é do idioma pedido ou a app ainda não a reconheceu: veja a lista de legendas do filme."
        elif before is not None:
            msg = (f"O Bazarr aceitou o pedido mas não registou nenhum download em {int(self.BAZARR_WAIT)} s (pode não ter encontrado legenda, ou ainda estar a "
                   "trabalhar: use «Reanalisar» daqui a pouco).")
        else:
            msg = "O Bazarr não obteve nenhuma legenda nova."
        add_event(self.db, "bazarr", f"Bazarr ({media['title']}, {lang}): {msg}", "info" if found else "warning", media_id)
        if self.settings.get("notify_plex_after_install") and found:
            try:
                self.refresh_plex(media_id)
            except IntegrationError:
                pass
        return {"media_id": media_id, "lang": lang, "found": found, "message": msg, "had_before": had, "source": src}

    def bazarr_download_candidate(self, media_id: int, result: dict) -> dict:
        """O Bazarr descarrega a candidata escolhida e GRAVA-A ao lado do vídeo (a API dele não a devolve). Antes guarda-se o conteúdo dos ficheiros de legenda
        existentes: se o Bazarr substituir algum, fica cópia restaurável. A seguir a app reanalisa e regista a origem."""
        svc = self._integration("bazarr")
        if svc is None:
            raise ServiceError("Integração com o Bazarr desativada (Definições → Bazarr).", 400)
        media = self.media_dict(media_id)
        if media["kind"] != "movie" or not media.get("radarr_id"):
            raise ServiceError("Só filmes com id do Radarr (sincronize os metadados do Radarr).", 422)
        if result.get("provider") != "bazarr" or not result.get("via") or not result.get("file_id"):
            raise ServiceError("Resultado inválido (não é uma candidata do Bazarr).", 422)
        lib = self.db.one("SELECT * FROM libraries WHERE id=?", (media["library_id"],))
        if not any(l["id"] == media["library_id"] and l["effective_write"] for l in self.libraries()):
            raise ServiceError("A biblioteca deste filme está em modo só de leitura: não se pede ao Bazarr que escreva nela.", 403)
        ex = result.get("extra") or {}
        before: dict[str, bytes] = {}
        for r in self.db.query("SELECT path FROM subtitles WHERE media_id=? AND kind='external' AND present=1 LIMIT 60", (media_id,)):
            try:
                if os.path.getsize(r["path"]) <= MAX_SUB_BYTES:
                    before[r["path"]] = Path(r["path"]).read_bytes()
            except OSError:
                pass
        try:
            res = svc.download_candidate(media["radarr_id"], result["via"], result["file_id"], bool(ex.get("hi")), bool(ex.get("forced")), str(ex.get("original_format", "False")))
        except IntegrationError as e:
            add_event(self.db, "bazarr", f"Bazarr ({media['title']}): {e}", "warning", media_id)
            raise ServiceError(str(e), 502)
        if not res.get("ok"):
            msg = f"O Bazarr não conseguiu descarregar esta legenda: {res.get('message')}"
            add_event(self.db, "bazarr", f"Bazarr ({media['title']}): {msg}", "warning", media_id)
            return {"media_id": media_id, "found": False, "message": msg, "source": None, "overwritten": []}
        try:
            self.scanner.scan_library(dict(lib), None, media["dir"], force_subs=True)
        except Exception as e:  # noqa: BLE001
            log.warning("reanálise após Bazarr: %s", scrub(str(e)))
        overwritten, touched = [], set()
        for r in self.db.query("SELECT * FROM subtitles WHERE media_id=? AND kind='external' AND present=1", (media_id,)):
            old = before.get(r["path"])
            try:
                cur = Path(r["path"]).read_bytes() if os.path.getsize(r["path"]) <= MAX_SUB_BYTES else None
            except OSError:
                continue
            if old is None:
                touched.add(os.path.basename(r["path"]))
            elif cur is not None and st.sha1(cur) != st.sha1(old):
                touched.add(os.path.basename(r["path"]))
                backup = self._backup_bytes(old, r["id"], r["path"])
                self._record_edit(dict(r), "bazarr_overwrite", {"provider": result["via"]}, backup, st.sha1(old), st.sha1(cur))
                overwritten.append(os.path.basename(r["path"]))
        self._bazarr_attribute(media, svc, force_files=touched)
        src = None
        for name in touched:
            o = self.db.one("SELECT o.* FROM sub_origins o JOIN subtitles s ON s.path=o.path WHERE s.media_id=? AND s.present=1 AND o.origin='bazarr' AND o.path LIKE ? ORDER BY o.at DESC LIMIT 1",
                            (media_id, "%" + name.replace("%", "").replace("_", "")))
            if o:
                src = self._origin_view(dict(o))
                break
        found = bool(touched)
        label = ex.get("provider_label") or self.PROVIDER_LABEL.get(result["via"].lower(), result["via"])
        msg = ((f"Descarregada pelo Bazarr ({(src or {}).get('provider_label') or label}{', ' + src['detail'] if src and src.get('detail') else ''})"
                + (f"; substituiu {', '.join(overwritten)} (cópia guardada, restaurável no histórico de alterações)" if overwritten else "") + ".")
               if found else "O Bazarr aceitou o pedido mas não apareceu nenhuma legenda nova na pasta.")
        add_event(self.db, "bazarr", f"Bazarr ({media['title']}): {msg}", "info" if found else "warning", media_id)
        if found and self.settings.get("notify_plex_after_install"):
            try:
                self.refresh_plex(media_id)
            except IntegrationError:
                pass
        return {"media_id": media_id, "found": found, "message": msg, "source": src, "overwritten": overwritten}

    def _backup_bytes(self, data: bytes, sid: int, path: str) -> str:
        self.cfg.backup_dir.mkdir(parents=True, exist_ok=True)
        dst = self.cfg.backup_dir / f"{int(time.time())}-{sid}-{sanitize_filename(os.path.basename(path))}"
        dst.write_bytes(data)
        return str(dst)

    def start_bazarr_batch(self, media_ids: list[int], languages: list[str] | None) -> int:
        if not media_ids:
            raise ServiceError("Nenhum item selecionado.")
        if self._integration("bazarr") is None:
            raise ServiceError("Integração com o Bazarr desativada.", 400)
        return self.jobs.submit("bazarr_batch", f"Pedir legendas ao Bazarr ({len(media_ids)} filmes)",
                                {"media_ids": media_ids, "languages": languages})

    def _job_bazarr_batch(self, ctx: JobContext, p: dict) -> dict:
        ids = p["media_ids"]
        ctx.set_total(len(ids))
        c = cov.compute(self.db, self.settings, ids)
        out = {"found": 0, "not_found": 0, "skipped": 0, "errors": 0, "details": []}
        for mid in ids:
            ctx.check_cancel()
            m = self.db.one("SELECT title FROM media_items WHERE id=?", (mid,))
            ctx.step(m["title"] if m else str(mid), message=f"{out['found']} encontradas · {out['not_found']} sem resultado")
            want = [l for l in (p.get("languages") or c.get(mid, {}).get("missing") or []) if l not in c.get(mid, {}).get("covered", {})]
            for lang in want:
                ctx.check_cancel()
                try:
                    r = self.bazarr_request(mid, lang)
                    out["found" if r["found"] else "not_found"] += 1
                except ServiceError as e:
                    if e.status in (400, 403, 422):
                        out["skipped"] += 1
                    else:
                        out["errors"] += 1
                    if len(out["details"]) < 40:
                        out["details"].append({"media_id": mid, "lang": lang, "error": str(e)})
                    if "recusou a API key" in str(e) or "desativada" in str(e):
                        raise
        add_event(self.db, "bazarr", f"Lote Bazarr: {out['found']} encontradas, {out['not_found']} sem resultado, {out['errors']} erros.")
        return out

    # ---- ferramentas de legendas existentes (pré-visualizar / aplicar com backup / restaurar) ----
    def _sub(self, sid: int) -> dict:
        r = self.db.one("SELECT s.*, m.duration AS media_duration, m.dir AS media_dir FROM subtitles s "
                        "LEFT JOIN media_items m ON m.id=s.media_id WHERE s.id=?", (sid,))
        if not r or r["kind"] != "external" or not r["present"]:
            raise ServiceError("Legenda externa não encontrada.", 404)
        return dict(r)

    def _read_editable(self, sub: dict, *, any_ext: bool = False) -> tuple[str, bytes]:
        try:
            real = st.check_editable(sub["path"], self.writable_roots(), allow_any_sub_ext=any_ext)
        except st.ToolError as e:
            raise ServiceError(str(e), e.status)
        return real, Path(real).read_bytes()

    def sub_cues(self, sid: int, head: int = 30, tail: int = 30) -> dict:
        sub = self._sub(sid)
        real = require_within(sub["path"], [l["path"] for l in self.libraries()])
        data = Path(real).read_bytes()
        text, _ = decode_bytes(data)
        cues = parse_srt(text).cues if sub["path"].lower().endswith(".srt") else []
        rows_ = [{"n": i + 1, "start": c.start_ms, "end": c.end_ms, "text": c.text[:90]} for i, c in enumerate(cues)]
        return {"total": len(rows_), "head": rows_[:head], "tail": rows_[-tail:] if len(rows_) > head else [],
                "references": [{"id": r["id"], "name": os.path.basename(r["path"]), "lang": r["lang"], "cues": r["cue_count"]}
                               for r in self.db.query("SELECT id,path,lang,cue_count FROM subtitles WHERE media_id=? AND kind='external' AND present=1 "
                                                      "AND status='ok' AND text_based=1 AND id!=? AND cue_count>=30", (sub["media_id"], sid))]
                              + [{"id": r["id"], "name": f"Faixa embutida #{r['stream_index']}" + (f" · {r['title']}" if r["title"] else ""), "lang": r["lang"], "cues": None, "embedded": True}
                                 for r in self.db.query("SELECT id,stream_index,lang,title FROM subtitles WHERE media_id=? AND kind='embedded' AND present=1 AND text_based=1 "
                                                        "AND forced=0 AND stream_index IS NOT NULL ORDER BY stream_index", (sub["media_id"],))]}

    def _plan(self, sid: int, action: str, params: dict):
        sub = self._sub(sid)
        real, data = self._read_editable(sub)
        text, enc = decode_bytes(data)
        parsed = parse_srt(text)
        if not parsed.cues:
            raise ServiceError("Legenda sem eventos legíveis.", 422)
        ref = None
        embedded_ref = None
        if action == "reference":
            rid = params.get("reference_id")
            rr = self.db.one("SELECT * FROM subtitles WHERE id=? AND media_id=? AND present=1 AND text_based=1 AND ((kind='external' AND status='ok') OR (kind='embedded' AND forced=0))",
                             (rid, sub["media_id"]))
            if not rr or rr["id"] == sub["id"]:
                raise ServiceError("Referência inválida (tem de ser outra legenda ou faixa de texto embutida do mesmo filme).", 422)
            if rr["kind"] == "embedded":
                media = self.media_dict(sub["media_id"])
                ref = self._embedded_parsed(media, rr["stream_index"])
                if ref is None:
                    raise ServiceError("Não foi possível extrair a faixa embutida (ffmpeg falhou, o vídeo não está acessível ou a faixa tem menos de 30 falas).", 422)
                embedded_ref = f"faixa embutida #{rr['stream_index']}"
            else:
                try:
                    rreal = require_within(rr["path"], [l["path"] for l in self.libraries()])
                except PathSecurityError:
                    raise ServiceError("Referência fora das bibliotecas.", 403)
                ref = parse_bytes(Path(rreal).read_bytes(), os.path.splitext(rr["path"])[1].lower())
                if ref is None:
                    raise ServiceError("Não foi possível ler a referência.", 422)
        try:
            fn, info = st.make_mapper(action, params, parsed.cues, ref)
        except st.ToolError as e:
            raise ServiceError(str(e), e.status)
        if embedded_ref:
            info["embedded_ref"] = embedded_ref
        return sub, real, data, text, enc, parsed, fn, info

    def _text_plan(self, sid: int, params: dict, mode: str = "textfix"):
        sub = self._sub(sid)
        real, data = self._read_editable(sub)
        text, enc = decode_bytes(data)
        orig = parse_srt(text)
        if not orig.cues:
            raise ServiceError("Legenda sem eventos legíveis.", 422)
        if mode == "durations":
            try:
                new_text, stats, changes = st.fix_durations(text, params)
            except st.ToolError as e:
                raise ServiceError(str(e), e.status)
            if stats["changed"] == 0:
                raise ServiceError("Nada a alterar: nenhuma fala desaparece antes do tempo de leitura com estas opções.", 422)
            new = parse_srt(new_text)
            ok = len(new.cues) == len(orig.cues)
            for i, (a, b) in enumerate(zip(orig.cues, new.cues)):                      # início igual, fim nunca encurta, nunca sobrepõe a seguinte
                nxt = orig.cues[i + 1].start_ms if i + 1 < len(orig.cues) else None
                if a.start_ms != b.start_ms or b.end_ms < a.end_ms or a.text != b.text or (nxt is not None and a.end_ms <= nxt < b.end_ms):
                    ok = False
                    break
            if not ok:
                raise ServiceError("Verificação falhou: o resultado alteraria inícios, texto ou sobreporia falas. Nada foi alterado.", 500)
            return sub, real, data, enc, new_text, stats, changes
        if mode in ("reflow", "ptpt"):
            if mode == "ptpt" and (sub.get("lang") or "pt").lower()[:2] != "pt":
                raise ServiceError("A adaptação pt-BR → pt-PT só se aplica a legendas em português.", 422)
            try:
                new_text, stats, changes = (st.reflow_srt if mode == "reflow" else st.adapt_ptpt)(text, params)
            except st.ToolError as e:
                raise ServiceError(str(e), e.status)
            if stats["changed"] == 0:
                raise ServiceError("Nada a alterar: " + ("as linhas já cumprem o comprimento máximo." if mode == "reflow" else "não encontrei expressões do português do Brasil para adaptar."), 422)
            new = parse_srt(new_text)
            ok = len(new.cues) == len(orig.cues) and all((a.start_ms, a.end_ms) == (b.start_ms, b.end_ms) for a, b in zip(orig.cues, new.cues))
            if ok and mode == "reflow":                                                 # o texto (sem espaços nem tags) tem de ser igual
                norm = lambda t: re.sub(r"<[^>]+>|\s+", "", t)
                ok = all(norm(a.text) == norm(b.text) for a, b in zip(orig.cues, new.cues))
            if not ok:
                raise ServiceError("Verificação falhou: o resultado alteraria tempos ou perderia texto. Nada foi alterado.", 500)
            return sub, real, data, enc, new_text, stats, changes
        try:
            new_text, stats, changes = st.transform_srt_text(text, params, sub.get("lang"))
        except st.ToolError as e:
            raise ServiceError(str(e), e.status)
        if stats["changed"] == 0:
            raise ServiceError("Nada a alterar com estas opções: o texto já está limpo.", 422)
        # verificação: tempos e ordem iguais aos originais (menos as falas removidas)
        new = parse_srt(new_text)
        dropped = set(stats["dropped_positions"])
        expected = [(c.start_ms, c.end_ms) for i, c in enumerate(orig.cues, 1) if i not in dropped]
        if [(c.start_ms, c.end_ms) for c in new.cues] != expected:
            raise ServiceError("Verificação falhou: os tempos mudariam. Nada foi alterado.", 500)
        return sub, real, data, enc, new_text, stats, changes

    def _text_preview(self, sid: int, params: dict, mode: str = "textfix") -> dict:
        sub, real, data, enc, new_text, stats, changes = self._text_plan(sid, params, mode)
        if mode == "reflow":
            return {"kind": "text", "summary": f"{stats['changed']} de {stats['cues']} falas serão reformatadas (linhas até {params.get('max_len', 42)} caracteres, no máximo 2 linhas).",
                    "base_hash": st.sha1(data), "name": os.path.basename(real), "encoding": enc, "stats": {k: v for k, v in stats.items() if k != "by_tag"}, "changes": changes,
                    "warnings": ["Só mudam as quebras de linha: o texto e os tempos ficam iguais (verificado antes de gravar)."] +
                    ([f"{stats['by_tag']['continua longa']} falas continuam com uma linha acima do máximo porque o texto não cabe em 2 linhas."] if stats["by_tag"].get("continua longa") else [])}
        if mode == "ptpt":
            bt = stats["by_tag"]
            return {"kind": "text", "summary": f"{stats['changed']} de {stats['cues']} falas serão adaptadas ({', '.join(f'{n} de {k}' for k, n in bt.items())}).",
                    "base_hash": st.sha1(data), "name": os.path.basename(real), "encoding": enc, "stats": {k: v for k, v in stats.items() if k != "by_tag"}, "changes": changes,
                    "warnings": ["Regras fixas (vocabulário, grafia e «estar a + infinitivo»). Não muda o tratamento («você»), a colocação dos pronomes nem gíria: reveja a pré-visualização."]}
        if mode == "durations":
            return {"kind": "text", "summary": f"{stats['changed']} de {stats['cues']} falas ficam mais tempo no ecrã (+{stats['extended_ms'] / 1000:.1f} s no total; só se alonga o fim).",
                    "base_hash": st.sha1(data), "name": os.path.basename(real), "encoding": enc, "stats": stats, "changes": changes,
                    "warnings": ["Só se alonga o fim das falas curtas: o início, o texto e a ordem não mudam, nenhuma fala encurta e nenhuma sobrepõe a seguinte."]}
        parts = [f"{stats['style']} com tags de estilo" if stats["style"] else "", f"{stats['hi']} com marcas HI" if stats["hi"] else "",
                 f"{stats['common']} com correções comuns" if stats["common"] else "", f"{stats['uppercase']} em maiúsculas" if stats["uppercase"] else "",
                 f"{stats['watermark']} anúncios/marcas de água" if stats["watermark"] else ""]
        return {"kind": "text", "summary": f"{stats['changed']} de {stats['cues']} falas serão alteradas ({', '.join(p for p in parts if p)}).",
                "base_hash": st.sha1(data), "name": os.path.basename(real), "encoding": enc, "stats": {k: v for k, v in stats.items() if k != "dropped_positions"},
                "changes": changes, "warnings": ([f"{stats['dropped']} falas ficam vazias e serão removidas."] if stats["dropped"] else []) +
                (["«Maiúsculas→frase» perde a capitalização de nomes próprios (reveja a pré-visualização)."] if stats["uppercase"] else [])}

    def sub_quality(self, sid: int, max_len: int = 42) -> dict:
        sub = self._sub(sid)
        try:
            real = require_within(sub["path"], [l["path"] for l in self.libraries()])
        except PathSecurityError:
            raise ServiceError("Legenda fora das bibliotecas.", 403)
        data = Path(real).read_bytes()
        if not real.lower().endswith(".srt"):
            raise ServiceError("O relatório de qualidade só analisa legendas SRT. Converta-a primeiro.", 422)
        text, enc = decode_bytes(data)
        return {**st.analyze_quality(text, max(28, min(60, int(max_len))), sub.get("lang")), "name": os.path.basename(real), "encoding": enc}

    def _convert_plan(self, sid: int, params: dict):
        sub = self._sub(sid)
        real, data = self._read_editable(sub, any_ext=True)
        ext = os.path.splitext(real)[1].lower()
        try:
            plan = st.plan_convert(data, ext, params)
        except st.ToolError as e:
            raise ServiceError(str(e), e.status)
        target = real if plan["fmt"] == "srt" and ext == ".srt" else st.convert_target(real, plan["fmt"])
        if target != real:
            try:
                require_within(target, self.writable_roots())
            except PathSecurityError:
                raise ServiceError("O destino não está numa biblioteca com escrita ativa.", 403)
            if os.path.lexists(target):
                raise ServiceError(f"Já existe {os.path.basename(target)}; não é substituído. Mova-o para backup primeiro.", 409)
        new = parse_srt(plan["new_text"])
        if [(c.start_ms, c.end_ms) for c in new.cues] != plan["expected"]:
            raise ServiceError("Verificação falhou: os tempos não coincidem com o original. Nada foi alterado.", 500)
        return sub, real, data, target, plan

    def _convert_preview(self, sid: int, params: dict) -> dict:
        sub, real, data, target, plan = self._convert_plan(sid, params)
        keep = bool(params.get("keep_original"))
        return {"kind": "convert", "summary": f"{os.path.basename(real)} → {os.path.basename(target)}: " + "; ".join(plan["changes"]) + ".",
                "base_hash": st.sha1(data), "name": os.path.basename(real), "target": os.path.basename(target), "encoding": plan["enc"], "bom": plan["bom"],
                "format": plan["fmt"], "cues": plan["cues"], "dropped": plan["dropped"], "samples": plan["samples"], "moji": plan["moji"],
                "warnings": plan["warnings"] + ([f"O original {os.path.basename(real)} será movido para backup (restaurável)."] if target != real and not keep else [])}

    def _convert_apply(self, sid: int, params: dict, base_hash: str) -> dict:
        sub, real, data, target, plan = self._convert_plan(sid, params)
        if st.sha1(data) != base_hash:
            raise ServiceError("O ficheiro mudou desde a pré-visualização. Volte a pré-visualizar.", 409)
        keep = bool(params.get("keep_original"))
        new_bytes = plan["new_text"].encode("utf-8")
        backup = self._backup(real, sid)
        try:
            st.atomic_replace(target, new_bytes)
            if target != real and Path(target).read_bytes() != new_bytes:
                raise OSError(5, "Verificação da escrita falhou")
            if target != real and not keep:
                os.unlink(real)
        except OSError as e:
            if target != real and os.path.lexists(target) and st.sha1(Path(target).read_bytes()) == st.sha1(new_bytes):
                os.unlink(target)
            raise ServiceError(f"Não foi possível escrever: {e.strerror}", 403)
        if target != real and not keep:
            self.db.execute("UPDATE subtitles SET present=0 WHERE id=?", (sid,))
        eid = self._record_edit(sub, "convert", {**params, "output_path": target, "keep_original": keep}, backup, st.sha1(data), st.sha1(new_bytes))
        add_event(self.db, "edit", f"Legenda convertida ({'; '.join(plan['changes'])}): {os.path.basename(target)} — cópia em backup #{eid}", "info", sub["media_id"])
        self._rescan_dir(sub)
        return {"edit_id": eid, "target": os.path.basename(target)}

    def sub_preview(self, sid: int, action: str, params: dict) -> dict:
        if action in ("textfix", "durations", "reflow", "ptpt"):
            return self._text_preview(sid, params, action)
        if action == "convert":
            return self._convert_preview(sid, params)
        sub, real, data, text, enc, parsed, fn, info = self._plan(sid, action, params)
        pv = st.build_preview(parsed.cues, fn, sub.get("media_duration"))
        pv.update({"summary": info["summary"], "base_hash": st.sha1(data), "name": os.path.basename(real), "encoding": enc,
                   "estimate": {k: v for k, v in (info.get("estimate") or {}).items() if k != "linear"} or None})
        if info.get("embedded_ref"):
            pv.setdefault("warnings", []).append(f"A referência é a {info['embedded_ref']}, extraída do vídeo: só se comparam tempos (serve qualquer idioma). Se essa faixa estiver desfasada do áudio, esta legenda ficará igualmente desfasada; confirme com «Alinhar com o áudio».")
        return pv

    def _backup(self, real: str, sid: int) -> str:
        self.cfg.backup_dir.mkdir(parents=True, exist_ok=True)
        base = f"{int(time.time())}-{sid}-{sanitize_filename(os.path.basename(real))}"
        dst, n = self.cfg.backup_dir / base, 1
        while dst.exists():                                       # vários passos seguidos na mesma legenda (pipeline) nunca se sobrepõem
            dst, n = self.cfg.backup_dir / f"{n}-{base}", n + 1
        shutil.copy2(real, dst)
        return str(dst)

    def _record_edit(self, sub: dict, action: str, params: dict, backup: str | None, before: str, after: str | None) -> int:
        return self.db.execute(
            "INSERT INTO subtitle_edits(subtitle_id,media_id,library_id,path,action,params,backup_path,before_hash,after_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sub["id"], sub["media_id"], sub["library_id"], sub["path"], action, json.dumps({k: v for k, v in params.items()}), backup, before, after, now())).lastrowid

    def _rescan_dir(self, sub: dict) -> None:
        lib = self.db.one("SELECT * FROM libraries WHERE id=?", (sub["library_id"],))
        if lib:
            try:
                self.scanner.scan_library(dict(lib), None, os.path.dirname(sub["path"]), force_subs=True)
            except Exception as e:  # noqa: BLE001
                log.warning("reanálise após edição: %s", scrub(str(e)))

    def _text_apply(self, sid: int, params: dict, base_hash: str, mode: str = "textfix") -> dict:
        sub, real, data, enc, new_text, stats, changes = self._text_plan(sid, params, mode)
        if st.sha1(data) != base_hash:
            raise ServiceError("O ficheiro mudou desde a pré-visualização. Volte a pré-visualizar.", 409)
        backup = self._backup(real, sid)
        try:
            st.atomic_replace(real, st.encode_like(new_text, data, enc))
        except OSError as e:
            raise ServiceError(f"Não foi possível escrever: {e.strerror}", 403)
        eid = self._record_edit(sub, mode, params, backup, st.sha1(data), st.sha1(Path(real).read_bytes()))
        what = {"durations": "Durações das falas ajustadas", "reflow": "Linhas da legenda reformatadas", "ptpt": "Legenda adaptada para pt-PT"}.get(mode, "Texto da legenda limpo")
        add_event(self.db, "edit", f"{what} ({stats['changed']} falas): {os.path.basename(real)} — cópia em backup #{eid}", "info", sub["media_id"])
        self._rescan_dir(sub)
        return {"edit_id": eid, "changed": stats["changed"], "dropped": stats.get("dropped", 0)}

    def sub_apply(self, sid: int, action: str, params: dict, base_hash: str) -> dict:
        if action in ("textfix", "durations", "reflow", "ptpt"):
            return self._text_apply(sid, params, base_hash, action)
        if action == "convert":
            return self._convert_apply(sid, params, base_hash)
        sub, real, data, text, enc, parsed, fn, info = self._plan(sid, action, params)
        if st.sha1(data) != base_hash:
            raise ServiceError("O ficheiro mudou desde a pré-visualização. Volte a pré-visualizar.", 409)
        new_text, stats = st.retime_srt(text, fn)
        if stats["cues"] and stats["dropped"] > max(3, stats["cues"] * 0.05):
            raise ServiceError(f"A alteração removeria {stats['dropped']} falas (>5 %): provavelmente o deslocamento está errado.", 422)
        new_bytes = st.encode_like(new_text, data, enc)
        if len(parse_srt(new_text).cues) != stats["cues"] - stats["dropped"]:
            raise ServiceError("Verificação falhou: o resultado não tem o número esperado de falas. Nada foi alterado.", 500)
        backup = self._backup(real, sid)
        try:
            st.atomic_replace(real, new_bytes)
        except OSError as e:
            raise ServiceError(f"Não foi possível escrever: {e.strerror}", 403)
        eid = self._record_edit(sub, action, params, backup, st.sha1(data), st.sha1(new_bytes))
        add_event(self.db, "edit", f"Legenda ajustada ({action}): {os.path.basename(real)} — cópia em backup #{eid}", "info", sub["media_id"])
        self._rescan_dir(sub)
        return {"edit_id": eid, "summary": info["summary"], "dropped": stats["dropped"], "clamped": stats["clamped"]}

    # ---- alinhar a legenda com a fala do áudio (opcional, a pedido) ------------------------
    def audio_tracks(self, sid: int) -> list[dict]:
        from . import audio_sync as au
        sub = self._sub(sid)
        m = self.db.one("SELECT path FROM media_items WHERE id=?", (sub["media_id"],))
        if not m or not os.path.isfile(m["path"]):
            raise ServiceError("O vídeo não está acessível.", 422)
        return au.list_audio_tracks(m["path"], self.cfg.ffprobe_bin)

    def start_audio_sync(self, sid: int, track: int = 0, rigorous: bool = True) -> int:
        sub = self._sub(sid)
        if not sub["path"].lower().endswith(".srt") or not sub.get("media_id"):
            raise ServiceError("Só legendas SRT associadas a um vídeo.", 422)
        m = self.db.one("SELECT path,duration FROM media_items WHERE id=?", (sub["media_id"],))
        if not m or not m["duration"] or not os.path.isfile(m["path"]):
            raise ServiceError("O vídeo não está acessível ou não tem duração conhecida (reanalise o filme).", 422)
        track = max(0, min(int(track), 15))
        params = {"subtitle_id": sid, **({"track": track} if track else {}), **({} if rigorous else {"rigorous": False})}
        return self.jobs.submit("audio_sync", f"Alinhar com o áudio: {os.path.basename(sub['path'])}" + (f" (faixa de áudio {track + 1})" if track else ""), params, dedupe=True)

    @staticmethod
    def _speech_cues(cues) -> list[tuple[int, int]]:
        """Falas para comparar com o áudio: tira as descrições para surdos ([música], (grita), «NOME:»), que aparecem quando ninguém fala e desviariam a análise."""
        from . import audio_sync as au
        out = []
        for c in cues:
            lines = st.remove_hi([l for l in c.text.split("\n")])
            if lines:
                out.append((c.start_ms, c.end_ms, len(" ".join(lines))))
        return au.spoken_span(out) if out else au.spoken_span([(c.start_ms, c.end_ms, len(c.text)) for c in cues])

    def _audio_core(self, sub: dict, prog, track: int = 0, rigorous: bool = True) -> dict:
        """Analisa uma legenda contra o áudio do vídeo, guarda o resultado (audio_checks) e marca «alinhada» se for o caso."""
        from . import audio_sync as au
        m = self.db.one("SELECT path,duration FROM media_items WHERE id=?", (sub["media_id"],))
        try:
            real = require_within(sub["path"], [l["path"] for l in self.libraries()])
        except PathSecurityError:
            raise JobError("A legenda está fora das bibliotecas.")
        data = Path(real).read_bytes()
        text, _ = decode_bytes(data)
        cues = self._speech_cues(parse_srt(text).cues)      # só FALA (sem [música], (suspira), «NOME:»); falas que desaparecem cedo demais contam com o tempo de leitura
        au.PROBE_BIN = self.cfg.ffprobe_bin
        try:
            res = au.analyze(m["path"], cues, float(m["duration"]), self.cfg.ffmpeg_bin, prog, track=track, rigorous=rigorous)
        except au.AudioSyncError as e:
            raise JobError(str(e))
        res["subtitle_id"], res["base_hash"] = sub["id"], st.sha1(data)
        self._save_audio_check(sub, res)
        if res["mode"] == "aligned":                       # prova real (áudio): marca como alinhada
            self.db.execute("UPDATE subtitles SET sync_status='aligned', sync_detail=? WHERE id=?",
                            (f"Alinhada com o áudio ({res['agree']}/{res['windows_total']} janelas concordam)", sub["id"]))
        add_event(self.db, "edit", f"Alinhamento com o áudio de {os.path.basename(sub['path'])}: {res['mode']} ({res['agree']}/{res['windows_total']} janelas)", "info", sub["media_id"])
        return res

    def _save_audio_check(self, sub: dict, res: dict) -> None:
        status = {"aligned": "aligned", "offset": "pending", "linear": "pending"}.get(res["mode"], "inconclusive")
        self.db.execute("INSERT OR REPLACE INTO audio_checks(subtitle_id,media_id,path,mode,status,result,base_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (sub["id"], sub["media_id"], sub["path"], res["mode"], status, json.dumps(res), res.get("base_hash"), now()))

    def _job_audio_sync(self, ctx: JobContext, p: dict) -> dict:
        sub = self._sub(p["subtitle_id"])

        def prog(i: int, total: int, msg: str) -> None:
            if ctx.total == 0:
                ctx.set_total(total + 1)
            ctx.step(msg, inc=1 if i > 0 else 0, message=f"Janela {min(i + 1, total)} de {total}", force=True)
        return self._audio_core(sub, prog, int(p.get("track", 0)), bool(p.get("rigorous", True)))

    # ---- verificação de sincronização em lote (resultados ficam à espera de aprovação) ------------
    def audio_batch_candidates(self, media_ids: list[int] | None = None, redo: bool = False) -> list[dict]:
        langs = set(self.settings.primary_langs()) | set((self.settings.get("display_languages") or {}).get("keep", []))
        q = ("SELECT s.id,s.path,s.lang,s.media_id,s.content_hash,m.title,m.year,m.series_title,m.season,m.episode,m.path AS vpath FROM subtitles s JOIN media_items m ON m.id=s.media_id "
             "WHERE s.kind='external' AND s.present=1 AND s.status='ok' AND lower(s.path) LIKE '%.srt' AND s.association='certain' AND m.duration>0 "
             "AND s.sync_status IN ('plausible','unchecked','suspect')")
        args: list = []
        if media_ids:
            q += f" AND s.media_id IN ({','.join('?' * len(media_ids))})"
            args += list(media_ids)
        out = []
        for r in self.db.query(q + " ORDER BY m.title, s.id", tuple(args)):
            if r["lang"] not in langs:
                continue
            ck = self.db.one("SELECT base_hash FROM audio_checks WHERE subtitle_id=?", (r["id"],))
            if ck and not redo and ck["base_hash"] == r["content_hash"]:
                continue                                                   # já analisada com este conteúdo
            if not os.path.isfile(r["vpath"]):
                continue
            out.append(dict(r))
        return out

    def start_audio_batch(self, media_ids: list[int] | None = None, redo: bool = False, limit: int = 200) -> dict:
        items = self.audio_batch_candidates(media_ids, redo)[:max(1, min(int(limit), 500))]
        if not items:
            raise ServiceError("Nada para verificar: todas as legendas por confirmar já foram analisadas (ou os vídeos não estão acessíveis).", 422)
        jid = self.jobs.submit("audio_batch", f"Verificar sincronização com o áudio ({len(items)} legendas)", {"subtitle_ids": [i["id"] for i in items]})
        return {"job_id": jid, "count": len(items)}

    def _job_audio_batch(self, ctx: JobContext, p: dict) -> dict:
        ids = p["subtitle_ids"]
        ctx.set_total(len(ids))
        out = {"analysed": 0, "aligned": 0, "pending": 0, "inconclusive": 0, "errors": 0}
        for n, sid in enumerate(ids, 1):
            ctx.check_cancel()
            try:
                sub = self._sub(sid)
            except ServiceError:
                ctx.step(inc=1, force=True)
                continue
            name = os.path.basename(sub["path"])
            label = f"{n} de {len(ids)}: {name}"

            def prog(i: int, total: int, msg: str, label=label) -> None:
                ctx.step(label, inc=0, message=f"Janela {min(i + 1, total)} de {total}", force=True)
            try:
                res = self._audio_core(sub, prog)
                out["analysed"] += 1
                out[{"aligned": "aligned", "offset": "pending", "linear": "pending"}.get(res["mode"], "inconclusive")] += 1      # «uneven» também conta como inconclusiva
            except JobCancelled:
                raise
            except JobError as e:
                out["errors"] += 1
                self.db.execute("INSERT OR REPLACE INTO audio_checks(subtitle_id,media_id,path,mode,status,result,base_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                (sid, sub["media_id"], sub["path"], "error", "error", json.dumps({"reason": str(e)}), None, now()))
            ctx.step(label, inc=1, message=f"{n} de {len(ids)} verificadas", force=True)
        return out

    # ---- escolher o melhor candidato com evidência do áudio ----------------------------------
    @staticmethod
    def _cand_key(r: dict) -> str:
        return f"{r.get('provider')}:{r.get('file_id')}"

    def start_candidate_check(self, media_id: int, results: list[dict], lang: str | None = None) -> int:
        media = self.media_dict(media_id)
        if not isinstance(results, list) or not 1 <= len(results) <= 3:
            raise ServiceError("Escolha entre 1 e 3 candidatas (cada uma gasta um download da quota do fornecedor).", 422)
        for r in results:
            if not isinstance(r, dict) or r.get("provider") not in PROVIDERS:
                raise ServiceError("Resultado inválido.", 422)
        if not media.get("duration") or not os.path.isfile(media["path"]):
            raise ServiceError("O vídeo não está acessível ou não tem duração conhecida (reanalise o filme).", 422)
        return self.jobs.submit("candidate_check", f"Verificar candidatas com o áudio: {media['title']}",
                                {"media_id": media_id, "results": results, "lang": lang})

    def _job_candidate_check(self, ctx: JobContext, p: dict) -> dict:
        from . import audio_sync as au
        media = self.media_dict(p["media_id"])
        results = p["results"]
        memo: dict[tuple, Any] = {}

        def extract(path, t0, d, ff, *a):                       # o áudio do filme lê-se UMA vez e serve todas as candidatas
            if (t0, d) not in memo:
                memo[(t0, d)] = au.extract_window(path, t0, d, ff)
            return memo[(t0, d)]
        au.PROBE_BIN = self.cfg.ffprobe_bin
        ctx.set_total(len(results))
        out = []
        for n, r in enumerate(results, 1):
            ctx.check_cancel()
            key = self._cand_key(r)
            label = f"Candidata {n} de {len(results)}"
            ctx.step(f"{label}: a descarregar…", inc=0, message="A descarregar a legenda", force=True)
            try:
                dl = self.prepare(p["media_id"], r, p.get("lang"))
            except ServiceError as e:
                out.append({"key": key, "error": str(e)})
                ctx.step(label, inc=1, force=True)
                continue
            did = dl["id"]
            row = self.db.one("SELECT staged_path FROM downloads WHERE id=?", (did,))
            cues = None
            try:
                parsed = parse_bytes(Path(row["staged_path"]).read_bytes(), ".srt") if row and row["staged_path"] else None
                cues = self._speech_cues(parsed.cues) if parsed and parsed.cues else None
            except OSError:
                pass
            item: dict = {"key": key, "download_id": did, "score": dl.get("score")}
            if not cues:
                item["error"] = "Não foi possível ler as falas da legenda descarregada."
            else:
                def prog(i: int, total: int, msg: str, label=label) -> None:
                    ctx.step(f"{label}: {msg}", inc=0, message=f"Janela {min(i + 1, total)} de {total}", force=True)
                try:
                    res = au.analyze(media["path"], cues, float(media["duration"]), self.cfg.ffmpeg_bin, prog, extract=extract, rigorous=True)
                    summ = {k: res.get(k) for k in ("mode", "offset_s", "scale", "agree", "windows_total", "windows_clear", "confidence", "z_total", "reason")}
                    item["audio"] = summ
                    ev = jloads(self.db.one("SELECT evaluation FROM downloads WHERE id=?", (did,))["evaluation"], {}) or {}
                    ev["audio"] = summ
                    self.db.execute("UPDATE downloads SET evaluation=? WHERE id=?", (json.dumps(ev), did))
                except au.AudioSyncError as e:
                    item["error"] = str(e)
            out.append(item)
            ctx.step(label, inc=1, force=True)
        ok = [i for i in out if i.get("audio") and i["audio"]["mode"] in ("aligned", "offset", "linear")]
        ok.sort(key=lambda i: (i["audio"]["mode"] != "aligned", -(i["audio"].get("agree") or 0), abs(i["audio"].get("offset_s") or 0)))
        best = ok[0]["key"] if ok else None
        add_event(self.db, "download", f"Candidatas verificadas com o áudio ({len(out)}): " + ", ".join(f"{i['key']}={i.get('audio', {}).get('mode', 'erro')}" for i in out), "info", p["media_id"])
        return {"candidates": out, "best": best}

    def audio_checks(self, status: str | None = None) -> dict:
        q = ("SELECT a.*, m.title, m.year, m.series_title, m.season, m.episode, s.lang, s.sync_status AS cur_sync, s.content_hash FROM audio_checks a "
             "LEFT JOIN media_items m ON m.id=a.media_id LEFT JOIN subtitles s ON s.id=a.subtitle_id")
        args: tuple = ()
        if status:
            q += " WHERE a.status=?"
            args = (status,)
        items = []
        for r in self.db.query(q + " ORDER BY a.created_at DESC LIMIT 500", args):
            res = jloads(r["result"], {}) or {}
            stale = r["content_hash"] is not None and r["base_hash"] is not None and r["content_hash"] != r["base_hash"]
            items.append({"subtitle_id": r["subtitle_id"], "media_id": r["media_id"], "title": r["title"], "year": r["year"], "series_title": r["series_title"],
                          "season": r["season"], "episode": r["episode"], "name": os.path.basename(r["path"] or ""), "lang": r["lang"], "mode": r["mode"], "status": r["status"],
                          "stale": stale, "created_at": r["created_at"], "decided_at": r["decided_at"],
                          "offset_s": res.get("offset_s"), "scale": res.get("scale"), "agree": res.get("agree"), "windows_total": res.get("windows_total"),
                          "windows_clear": res.get("windows_clear"), "reason": res.get("reason"), "z_total": res.get("z_total")})
        counts = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) AS n FROM audio_checks GROUP BY status")}
        waiting = len(self.audio_batch_candidates())
        return {"items": items, "counts": counts, "unchecked": waiting}

    def audio_check_apply(self, sid: int) -> dict:
        r = self.db.one("SELECT * FROM audio_checks WHERE subtitle_id=?", (sid,))
        if not r or r["status"] != "pending":
            raise ServiceError("Não há nenhuma correção pendente para esta legenda.", 404)
        res = jloads(r["result"], {}) or {}
        params = {"scale": res.get("scale", 1), "offset_seconds": res.get("offset_s", 0)}
        out = self.sub_apply(sid, "linear", params, r["base_hash"])      # verifica que o ficheiro é o analisado (409 se mudou), faz backup e grava
        self.db.execute("UPDATE audio_checks SET status='applied', decided_at=? WHERE subtitle_id=?", (now(), sid))
        new = self.db.one("SELECT id FROM subtitles WHERE path=? AND present=1", (r["path"],))
        if new:
            self.db.execute("UPDATE subtitles SET sync_status='aligned', sync_detail=? WHERE id=?",
                            (f"Corrigida com o áudio (aprovada): desvio {res.get('offset_s', 0):+.2f} s, {res.get('agree')}/{res.get('windows_total')} janelas concordam", new["id"]))
        return out

    def audio_check_dismiss(self, sid: int) -> None:
        if not self.db.one("SELECT 1 FROM audio_checks WHERE subtitle_id=?", (sid,)):
            raise ServiceError("Resultado não encontrado.", 404)
        self.db.execute("UPDATE audio_checks SET status='dismissed', decided_at=? WHERE subtitle_id=?", (now(), sid))

    def sub_delete(self, sid: int) -> dict:
        """Remove a legenda da pasta mas guarda uma cópia (restaurável). Nunca apaga vídeos."""
        sub = self._sub(sid)
        real, data = self._read_editable(sub, any_ext=True) if os.path.getsize(sub["path"]) else (None, b"")
        if real is None:
            raise ServiceError("Ficheiro vazio: use a página «Legendas vazias».", 422)
        backup = self._backup(real, sid)
        try:
            os.unlink(real)
        except OSError as e:
            raise ServiceError(f"Não foi possível remover: {e.strerror}", 403)
        self.db.execute("UPDATE subtitles SET present=0 WHERE id=?", (sid,))
        eid = self._record_edit(sub, "delete", {}, backup, st.sha1(data), None)
        add_event(self.db, "edit", f"Legenda movida para backup: {os.path.basename(real)} (#{eid})", "info", sub["media_id"])
        return {"edit_id": eid}

    def edits_for(self, media_id: int) -> list[dict]:
        out = []
        for r in self.db.query("SELECT * FROM subtitle_edits WHERE media_id=? ORDER BY id DESC LIMIT 50", (media_id,)):
            d = dict(r)
            d["name"] = os.path.basename(d["path"])
            d["params"] = jloads(d["params"], {})
            d["backup_exists"] = bool(d["backup_path"]) and os.path.isfile(d["backup_path"])
            d.pop("backup_path", None)
            out.append(d)
        return out

    def restore_edit(self, eid: int, force: bool = False) -> dict:
        e = self.db.one("SELECT * FROM subtitle_edits WHERE id=?", (eid,))
        if not e:
            raise ServiceError("Alteração não encontrada.", 404)
        if e["restored_at"]:
            raise ServiceError("Já foi restaurada.", 409)
        if not e["backup_path"] or not is_within(e["backup_path"], self.cfg.backup_dir) or not os.path.isfile(e["backup_path"]):
            raise ServiceError("A cópia de segurança já não existe.", 410)
        try:
            real = require_within(e["path"], self.writable_roots())
        except PathSecurityError:
            raise ServiceError("O destino não está numa biblioteca com escrita ativa.", 403)
        if not os.access(os.path.dirname(real), os.W_OK):
            raise ServiceError("Sem permissão de escrita na pasta.", 403)
        backup_bytes = Path(e["backup_path"]).read_bytes()
        out_path = (jloads(e["params"], {}) or {}).get("output_path") if e["action"] == "convert" else None
        if out_path and out_path != e["path"]:
            # conversão para outro ficheiro: remove o .srt criado (se ainda for o nosso) e repõe o original
            try:
                outp = require_within(out_path, self.writable_roots())
            except PathSecurityError:
                raise ServiceError("O destino não está numa biblioteca com escrita ativa.", 403)
            if os.path.lexists(outp) and st.sha1(Path(outp).read_bytes()) != e["after_hash"] and not force:
                raise ServiceError("O ficheiro convertido foi alterado depois desta edição. Restaurar remove-o (use «forçar»).", 409)
            if os.path.lexists(real) and st.sha1(Path(real).read_bytes()) != st.sha1(backup_bytes) and not force:
                raise ServiceError("Já existe um ficheiro com este nome; não é substituído.", 409)
            if os.path.lexists(outp):
                os.unlink(outp)
            if not os.path.lexists(real) or st.sha1(Path(real).read_bytes()) != st.sha1(backup_bytes):
                st.atomic_replace(real, backup_bytes)
            self.db.execute("UPDATE subtitle_edits SET restored_at=? WHERE id=?", (now(), eid))
            add_event(self.db, "edit", f"Conversão revertida: {os.path.basename(real)} (#{eid})", "info", e["media_id"])
            self._rescan_dir({"library_id": e["library_id"], "path": real})
            return {"restored": True}
        if e["action"] == "delete":
            if os.path.lexists(real):
                raise ServiceError("Já existe um ficheiro com este nome; não é substituído.", 409)
        else:
            if not os.path.isfile(real):
                pass  # o ficheiro foi removido entretanto: repõe
            elif st.sha1(Path(real).read_bytes()) != e["after_hash"] and not force:
                raise ServiceError("O ficheiro foi alterado depois desta edição. Restaurar sobrepõe essas alterações (use «forçar»).", 409)
        st.atomic_replace(real, backup_bytes)
        self.db.execute("UPDATE subtitle_edits SET restored_at=? WHERE id=?", (now(), eid))
        add_event(self.db, "edit", f"Legenda restaurada: {os.path.basename(real)} (#{eid})", "info", e["media_id"])
        sub = {"library_id": e["library_id"], "path": real}
        self._rescan_dir(sub)
        return {"restored": True}

    # ---- cópias de segurança: inventário e limpeza ----------------------------------------------
    def _backup_inventory(self, days: int, include_moved: bool) -> list[dict]:
        """Cada ficheiro de /data/backups com a classificação que decide se a limpeza o apaga.
        restored = todas as edições que o usam já foram restauradas; moved = é a única cópia de uma legenda movida para backup (só se apaga se pedir);
        outros (edições por restaurar e cópias de «substituir») só se apagam depois de `days` dias."""
        root = self.cfg.backup_dir
        if not root.exists():
            return []
        refs: dict[str, list[dict]] = {}
        for e in self.db.query("SELECT id,action,backup_path,restored_at FROM subtitle_edits WHERE backup_path IS NOT NULL"):
            refs.setdefault(e["backup_path"], []).append(dict(e))
        limit = time.time() - max(0, days) * 86400
        out = []
        for p in root.iterdir():
            try:
                lst = p.lstat()
            except OSError:
                continue
            if not S_ISREG(lst.st_mode):
                continue                                              # nunca segue atalhos nem toca em pastas
            rs = refs.get(str(p), [])
            moved = any(r["action"] == "delete" and not r["restored_at"] for r in rs)
            restored = bool(rs) and all(r["restored_at"] for r in rs)
            old = lst.st_mtime < limit
            if moved:
                kind, eligible = "moved", include_moved and old
            elif restored:
                kind, eligible = "restored", True
            else:
                kind, eligible = ("old" if old else "recent"), old
            out.append({"path": str(p), "size": lst.st_size, "kind": kind, "eligible": eligible})
        return out

    def backups_summary(self, days: int = 30, include_moved: bool = False) -> dict:
        days = max(0, min(int(days), 3650))
        inv = self._backup_inventory(days, include_moved)

        def agg(items: list[dict]) -> dict:
            return {"files": len(items), "bytes": sum(i["size"] for i in items)}
        return {"days": days, "include_moved": include_moved, "total": agg(inv), "restored": agg([i for i in inv if i["kind"] == "restored"]),
                "old": agg([i for i in inv if i["kind"] == "old"]), "recent": agg([i for i in inv if i["kind"] == "recent"]),
                "moved": {**agg([i for i in inv if i["kind"] == "moved"]), "eligible": agg([i for i in inv if i["kind"] == "moved" and i["eligible"]])},
                "will_free": agg([i for i in inv if i["eligible"]])}

    def backups_cleanup(self, days: int = 30, include_moved: bool = False) -> dict:
        days = max(0, min(int(days), 3650))
        freed = files = 0
        for i in self._backup_inventory(days, include_moved):
            if not i["eligible"] or not is_within(i["path"], self.cfg.backup_dir):
                continue
            try:
                os.unlink(i["path"])
            except OSError:
                continue
            files += 1
            freed += i["size"]
        add_event(self.db, "cleanup", f"Limpeza de cópias de segurança: {files} ficheiros apagados ({freed // 1024} KB), com mais de {days} dias ou já restauradas"
                  + (" (incluindo legendas movidas para backup)" if include_moved else ""))
        return {"deleted": files, "freed_bytes": freed, "summary": self.backups_summary(days, include_moved)}

    # ---- legendas duplicadas (mesmo conteúdo, nomes diferentes) -----------------------
    @staticmethod
    def _keep_rank(r: dict) -> tuple:
        """Menor = melhor candidata a ficar: variante explícita no nome (pt-PT/pt-BR), idioma conhecido pelo nome, SRT, a mais antiga."""
        pt = ("pt-PT", "pt-BR")
        wrong = r["lang"] in pt and r["detected_lang"] in pt and r["lang"] != r["detected_lang"]      # o nome diz uma variante e o conteúdo é a outra
        explicit = r["lang_source"] == "filename" and (r["lang"] or "") not in ("", "pt")
        return (wrong, not explicit, r["lang_source"] != "filename", os.path.splitext(r["path"])[1].lower() != ".srt", r["mtime"] or 0, len(r["path"]))

    def duplicate_groups(self, q: str | None = None) -> list[dict]:
        rows_ = self.db.query(
            "SELECT s.id,s.path,s.lang,s.lang_source,s.detected_lang,s.size,s.mtime,s.media_id,s.library_id,s.content_hash,m.title,m.radarr_title,m.year,m.kind,m.series_title,m.season,m.episode "
            "FROM subtitles s JOIN media_items m ON m.id=s.media_id "
            "WHERE s.kind='external' AND s.present=1 AND s.content_hash IS NOT NULL AND s.status='ok' AND (s.media_id, s.content_hash) IN "
            "(SELECT media_id, content_hash FROM subtitles WHERE kind='external' AND present=1 AND content_hash IS NOT NULL AND media_id IS NOT NULL "
            " GROUP BY media_id, content_hash HAVING COUNT(*)>1) ORDER BY lower(coalesce(m.series_title, nullif(m.radarr_title,''), m.title)), m.season, m.episode, s.media_id, s.path")
        writable = {l["id"]: l["effective_write"] for l in self.libraries()}
        # ficheiros que já movemos para backup e que voltaram a aparecer com o mesmo caminho e conteúdo: alguém (Bazarr, Radarr…) os está a recriar
        moved: dict[tuple, int] = {}
        for e in self.db.query("SELECT path, before_hash FROM subtitle_edits WHERE action='delete' AND before_hash IS NOT NULL AND media_id IN (%s)" %
                               ",".join(str(int(r["media_id"])) for r in rows_) if rows_ else "SELECT NULL WHERE 0"):
            moved[(e["path"], e["before_hash"])] = moved.get((e["path"], e["before_hash"]), 0) + 1
        groups: dict[tuple, dict] = {}
        for r in rows_:
            d = dict(r)
            g = groups.setdefault((d["media_id"], d["content_hash"]), {
                "media_id": d["media_id"], "hash": d["content_hash"],
                "title": (d["series_title"] and f"{d['series_title']} S{d['season'] or 0:02d}E{d['episode'] or 0:02d}") or (d["radarr_title"] or "").strip() or d["title"],
                "year": d["year"], "deletable": bool(writable.get(d["library_id"])), "files": []})
            g["files"].append({"id": d["id"], "name": os.path.basename(d["path"]), "lang": d["lang"], "lang_source": d["lang_source"], "detected_lang": d["detected_lang"], "size": d["size"], "mtime": d["mtime"],
                                   "reappeared": moved.get((d["path"], d["content_hash"]), 0), "_r": d})
        out = []
        for g in groups.values():
            # um ficheiro que outra ferramenta (Bazarr…) insiste em recriar é o que deve FICAR: se for movido, volta sempre
            best = min(g["files"], key=lambda f: (f["reappeared"] == 0, *self._keep_rank(f["_r"])))
            langs = {f["lang"] for f in g["files"] if f["lang"]}
            g["reappeared"] = sum(f["reappeared"] for f in g["files"])
            g["lang_conflict"] = len(langs) > 1          # ex.: «.pt-PT.srt» e «.pt-BR.srt» com o mesmo conteúdo: um deles está mal etiquetado
            g["content_lang"] = next((f["detected_lang"] for f in g["files"] if f["detected_lang"]), None)
            for f in g["files"]:
                f["keep"] = f is best
                f.pop("_r")
            if q and q.lower() not in (g["title"] or "").lower() and not any(q.lower() in f["name"].lower() for f in g["files"]):
                continue
            out.append(g)
        return out

    def duplicate_count(self) -> int:
        return self.db.scalar("SELECT COUNT(*) FROM (SELECT 1 FROM subtitles WHERE kind='external' AND present=1 AND content_hash IS NOT NULL AND media_id IS NOT NULL "
                              "GROUP BY media_id, content_hash HAVING COUNT(*)>1)") or 0

    def resolve_duplicates(self, items: list[dict]) -> dict:
        """items: [{keep: id, remove: [ids]}]. Cada remoção é revalidada no disco: o ficheiro a remover tem de ter EXATAMENTE os mesmos bytes
        que o que fica (que tem de existir). Vai para backup (restaurável), nunca é apagado definitivamente."""
        res = {"moved": 0, "skipped": [], "requested": sum(len(i.get("remove", [])) for i in items)}
        for it in items:
            keep = self.db.one("SELECT * FROM subtitles WHERE id=? AND kind='external' AND present=1", (it.get("keep"),))
            for rid in dict.fromkeys(it.get("remove", [])):
                def skip(reason: str):
                    res["skipped"].append({"id": rid, "reason": reason})
                r = self.db.one("SELECT * FROM subtitles WHERE id=? AND kind='external' AND present=1", (rid,))
                if not keep or not r or r["id"] == keep["id"] or r["media_id"] != keep["media_id"]:
                    skip("par inválido (têm de ser do mesmo filme e diferentes)")
                    continue
                try:
                    kreal = require_within(keep["path"], self.writable_roots())
                    rreal = require_within(r["path"], self.writable_roots())
                    if st.sha1(Path(kreal).read_bytes()) != st.sha1(Path(rreal).read_bytes()):
                        skip("o conteúdo já não é igual ao do ficheiro que fica")
                        continue
                except (PathSecurityError, OSError):
                    skip("fora de uma biblioteca com escrita ativa ou ilegível")
                    continue
                try:
                    self.sub_delete(rid)
                    res["moved"] += 1
                except ServiceError as e:
                    skip(str(e))
        add_event(self.db, "edit", f"Duplicados resolvidos: {res['moved']} legendas movidas para backup, {len(res['skipped'])} ignoradas.")
        return res

    # ---- conversão em lote (SRT/UTF-8) ---------------------------------------------
    MAX_CONVERT_ROWS = 4000

    def start_convert_scan(self) -> int:
        return self.jobs.submit("convert_scan", "Procurar legendas a converter", {}, dedupe=True)

    def _job_convert_scan(self, ctx: JobContext, p: dict) -> dict:
        """Só lê: lista as legendas que precisam de conversão/correção, sem alterar nada."""
        writable = {l["id"] for l in self.libraries() if l["effective_write"]}
        rows_ = [r for r in self.db.query(
            "SELECT s.id, s.path, s.lang, s.library_id, m.title, m.year FROM subtitles s LEFT JOIN media_items m ON m.id=s.media_id "
            "WHERE s.kind='external' AND s.present=1 AND s.status='ok' AND s.text_based=1 ORDER BY lower(coalesce(m.title,s.path)), s.path") if r["library_id"] in writable]
        ctx.set_total(len(rows_))
        out = {"checked": len(rows_), "items": [], "conflicts": 0, "errors": 0, "truncated": False, "by_reason": {}}
        for r in rows_:
            ctx.check_cancel()
            ctx.step(os.path.basename(r["path"]), message=f"{len(out['items'])} a converter")
            try:
                sub, real, data, target, plan = self._convert_plan(r["id"], {"fix_mojibake": True})
            except ServiceError as e:
                if e.status == 409 and "Já existe" in str(e):
                    out["conflicts"] += 1
                elif e.status != 422:
                    out["errors"] += 1
                continue
            reason = "formato" if plan["fmt"] != "srt" else ("acentos" if plan["moji"] else "codificação")
            out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
            if len(out["items"]) >= self.MAX_CONVERT_ROWS:
                out["truncated"] = True
                continue
            out["items"].append({"id": r["id"], "name": os.path.basename(real), "title": r["title"], "year": r["year"], "lang": r["lang"], "format": plan["fmt"],
                                 "encoding": plan["enc"], "bom": plan["bom"], "moji": plan["moji"], "target": os.path.basename(target), "reason": reason,
                                 "warnings": plan["warnings"][:1] if any("�" in w for w in plan["warnings"]) else []})
        add_event(self.db, "convert", f"Procura de legendas a converter: {len(out['items'])} candidatas em {out['checked']} verificadas ({out['conflicts']} com destino já existente).")
        return out

    def start_convert_batch(self, ids: list[int], params: dict) -> int:
        if not ids:
            raise ServiceError("Nenhuma legenda selecionada.")
        return self.jobs.submit("convert_batch", f"Converter legendas para SRT/UTF-8 ({len(ids)})",
                                {"ids": list(dict.fromkeys(ids)), "fix_mojibake": bool(params.get("fix_mojibake", True)), "keep_original": bool(params.get("keep_original"))})

    def _job_convert_batch(self, ctx: JobContext, p: dict) -> dict:
        ids = p["ids"]
        ctx.set_total(len(ids))
        prm = {"fix_mojibake": p.get("fix_mojibake", True), "keep_original": p.get("keep_original", False)}
        out = {"converted": 0, "skipped": 0, "errors": 0, "details": []}
        for sid in ids:
            ctx.check_cancel()
            r = self.db.one("SELECT path FROM subtitles WHERE id=?", (sid,))
            ctx.step(os.path.basename(r["path"]) if r else str(sid), message=f"{out['converted']} convertidas")
            try:
                sub, real, data, target, plan = self._convert_plan(sid, prm)     # reavalia no disco: o que foi pré-visualizado pode ter mudado
                self._convert_apply(sid, prm, st.sha1(data))
                out["converted"] += 1
            except ServiceError as e:
                out["skipped" if e.status in (404, 409, 422) else "errors"] += 1
                if len(out["details"]) < 40:
                    out["details"].append({"id": sid, "name": os.path.basename(r["path"]) if r else None, "error": str(e)})
        add_event(self.db, "convert", f"Conversão em lote: {out['converted']} convertidas, {out['skipped']} ignoradas, {out['errors']} erros.")
        return out

    # ---- legendas vazias (listar / apagar com salvaguardas) --------------------
    def _empty_rows(self, q: str | None = None, lang: str | None = None) -> list[dict]:
        where, args = ["s.kind='external'", "s.status='empty'", "s.present=1"], []
        if lang:
            where.append("s.lang IS ?" if lang == "none" else "s.lang=?")
            args.append(None if lang == "none" else lang)
        if q:
            where.append("(lower(s.path) LIKE ? OR lower(coalesce(m.title,'')) LIKE ?)")
            args += [f"%{q.lower()}%"] * 2
        rows_ = self.db.query(
            "SELECT s.id, s.path, s.lang, s.library_id, s.media_id, m.title, m.year, m.kind FROM subtitles s "
            f"LEFT JOIN media_items m ON m.id=s.media_id WHERE {' AND '.join(where)} ORDER BY lower(coalesce(m.title,s.path)), s.path", args)
        writable = {l["id"]: l["effective_write"] for l in self.libraries()}
        out = []
        for r in rows_:
            d = dict(r)
            d["name"] = os.path.basename(d["path"])
            d["folder"] = os.path.basename(os.path.dirname(d["path"]))
            d["deletable"] = bool(writable.get(d["library_id"]))
            out.append(d)
        return out

    def empty_subtitles(self, q: str | None, lang: str | None, limit: int, offset: int) -> dict:
        rows_ = self._empty_rows(q, lang)
        return {"total": len(rows_), "deletable": sum(1 for r in rows_ if r["deletable"]), "items": rows_[offset: offset + limit]}

    def empty_subtitle_ids(self, q: str | None, lang: str | None) -> list[int]:
        return [r["id"] for r in self._empty_rows(q, lang) if r["deletable"]]

    def delete_empty_subtitles(self, ids: list[int]) -> dict:
        """Apaga SÓ ficheiros de legenda que estão a 0 bytes NESTE momento, dentro de bibliotecas com escrita ativa.
        Cada passo é revalidado no disco (a BD pode estar desatualizada)."""
        from .naming import SUB_EXTS
        roots = self.writable_roots()
        res = {"deleted": 0, "skipped": [], "requested": len(ids)}
        for sid in dict.fromkeys(ids):
            r = self.db.one("SELECT id,path,kind,status,media_id FROM subtitles WHERE id=?", (sid,))

            def skip(reason: str, path: str | None = None):
                res["skipped"].append({"id": sid, "path": path or (r["path"] if r else None), "reason": reason})

            if not r or r["kind"] != "external":
                skip("não é uma legenda externa conhecida")
                continue
            path = r["path"]
            if os.path.splitext(path)[1].lower() not in SUB_EXTS:
                skip("extensão que não é de legenda")
                continue
            try:
                real = require_within(path, roots)
            except PathSecurityError:
                skip("fora de uma biblioteca com escrita ativa")
                continue
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                self.db.execute("UPDATE subtitles SET present=0 WHERE id=?", (sid,))
                skip("já não existe")
                continue
            except OSError as e:
                skip(f"erro ao ler: {e.strerror}")
                continue
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode) or real != os.path.realpath(path) or os.path.islink(path):
                skip("não é um ficheiro normal (atalho/pasta)")
                continue
            if st.st_size != 0:
                skip(f"já não está vazio ({st.st_size} bytes)")
                self.db.execute("UPDATE subtitles SET size=? WHERE id=?", (st.st_size, sid))
                continue
            try:
                os.unlink(path)
            except OSError as e:
                skip(f"não foi possível apagar: {e.strerror}")
                continue
            self.db.execute("UPDATE subtitles SET present=0 WHERE id=?", (sid,))
            add_event(self.db, "cleanup", f"Legenda vazia apagada: {os.path.basename(path)}", "info", r["media_id"])
            res["deleted"] += 1
        add_event(self.db, "cleanup", f"Limpeza de legendas vazias: {res['deleted']} apagadas, {len(res['skipped'])} ignoradas.")
        return res

    # ---- revalidação de legendas existentes ---------------------------------
    def _job_revalidate(self, ctx: JobContext, p: dict) -> dict:
        lib_ids = [r["id"] for r in self.db.query("SELECT id FROM libraries")]
        n = 0
        for lid in lib_ids:
            lib = dict(self.db.one("SELECT * FROM libraries WHERE id=?", (lid,)))
            self.scanner.scan_library(lib, ctx, None, force_subs=True)   # só relê legendas; não volta ao ffprobe
            n += 1
        return {"libraries": n}
