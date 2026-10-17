"""Camada de serviço: orquestra BD, scanner, fornecedores, avaliação e instalação."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from . import coverage as cov
from .config import AppConfig, Secrets
from .db import Database, jloads, rows
from .evaluator import Evaluation, evaluate_content, evaluate_metadata
from .installer import (InstallConflict, InvalidDownload, build_dest_name, install, plan_install, stage, validate_download)
from .integrations.arr import Arr
from .integrations.common import IntegrationError, map_remote_to_local
from .integrations.plex import Plex
from .jobs import JobCancelled, JobContext, JobManager
from .languages import normalize_lang
from .moviehash import moviehash
from .providers.base import ProviderContext, ProviderError, RateLimiter, SearchQuery, SubtitleResult
from .providers.registry import PROVIDERS
from .safepaths import PathSecurityError, is_within, require_within
from .scanner import Scanner, ScanAborted
from .settings import Settings
from .subtitles.parser import ParsedSub, parse_bytes
from .translation import TARGETS, DeepL, DeepLError, count_chars, to_srt, translate_cues
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
            ids = self.missing_media_ids(limit=int(s["auto_max_per_run"]))
            if ids:
                self.jobs.submit("batch", f"Download automático ({len(ids)} itens)",
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
        return Arr(name, cfg["url"], self.secrets.get(f"{name}_api_key"), cfg.get("path_mappings", []), self.transport)

    def test_integration(self, name: str) -> dict:
        try:
            svc = self._integration(name)
            if svc is None:
                return {"ok": False, "message": "Integração desativada ou sem URL."}
            return svc.test()
        except IntegrationError as e:
            return {"ok": False, "message": str(e)}

    def _sync_integrations(self, ctx: JobContext | None = None) -> dict:
        res = {"updated": 0}
        lookup = {r["path"]: r["id"] for r in self.db.query("SELECT id,path FROM media_items WHERE present=1")}

        def set_poster(mid: int, new: str | None, source: str) -> None:
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
                        upd(m["path"], imdb_id=m["imdb_id"], tmdb_id=m["tmdb_id"], year=m["year"])
                        if lookup.get(m["path"]):
                            set_poster(lookup[m["path"]], m["poster"], "arr")
                elif name == "sonarr":
                    for e in svc.episodes():
                        upd(e["path"], imdb_id=e["imdb_id"], tmdb_id=e["tmdb_id"], tvdb_id=e["tvdb_id"], episode_title=e["episode_title"])
                        if lookup.get(e["path"]):
                            set_poster(lookup[e["path"]], e["poster"], "arr")
                else:
                    for it in svc.items():
                        mid = lookup.get(it["path"])
                        if mid:
                            upd(it["path"], plex_key=it["rating_key"])
                            set_poster(mid, it["poster"], "plex")
            except IntegrationError as e:
                errors.append(f"{name}: {e}")
        if errors:
            raise IntegrationError("; ".join(errors))
        add_event(self.db, "integration", f"Metadados sincronizados ({res['updated']} atualizações).")
        return res

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
                "name": name, "title": cls.title, "homepage": cls.homepage, "supports_hash": cls.supports_hash,
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

    def search(self, media_id: int, languages: list[str] | None = None) -> dict:
        media = self.media_dict(media_id)
        s = self.settings.all()
        q = self.build_query(media, languages)
        results, errors = [], []
        steps = [("full", "hash + identificação" if q.moviehash else "identificação")]
        if q.moviehash and (q.imdb_id or q.tmdb_id or q.title):
            steps.append(("hash", "só hash do ficheiro"))
        steps.append(("title", "só título, sem ano"))
        tried: list[str] = []
        for name, pcfg in sorted(s["providers"].items(), key=lambda kv: kv[1].get("priority", 99)):
            if not pcfg.get("enabled") or name not in PROVIDERS:
                continue
            p = self.provider(name)
            if not p.configured():
                errors.append({"provider": name, "message": "Fornecedor sem credenciais configuradas.", "auth": True})
                continue
            try:
                for mode, label in steps:
                    if mode == "hash" and not p.supports_hash:
                        continue
                    qm = replace(q, mode=mode)
                    found = p.search(qm)
                    tried.append(f"{name}: {label} → {len(found)}")
                    if not found:
                        continue
                    for r in found:
                        r.found_by = label
                        ev = evaluate_metadata(media, r, hi_policy=s["hi_policy"], prefer_forced=s["prefer_forced"])
                        results.append({"result": r.to_dict(), "evaluation": ev.to_dict()})
                    break
            except ProviderError as e:
                errors.append({"provider": name, "message": str(e), "auth": e.auth, "rate_limited": e.rate_limited,
                               "retry_after": e.retry_after})
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
        return None

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
    def _deepl(self) -> DeepL:
        key = self.secrets.get("deepl_api_key")
        if not key:
            raise ServiceError("API key do DeepL não configurada (Fornecedores → DeepL).", 400)
        return DeepL(key, self.transport)

    def translation_status(self) -> dict:
        cfg = self.settings.get("translation")
        st = {"enabled": bool(cfg["enabled"]), "auto_fallback": bool(cfg["auto_fallback"]), "configured": bool(self.secrets.get("deepl_api_key")),
              "source": self.secrets.source("deepl_api_key"), "targets": list(TARGETS), "usage": None, "error": None, "plan": None}
        if st["configured"]:
            st["plan"] = "free" if self.secrets.get("deepl_api_key").endswith(":fx") else "pro"
            try:
                u = self._deepl().usage()
                st["usage"] = {"used": u.used, "limit": u.limit, "remaining": u.remaining}
            except DeepLError as e:
                st["error"] = str(e)
        return st

    def translate_prepare(self, media_id: int, result: dict, target: str) -> dict:
        """Descarrega a legenda de origem, traduz o texto (tempos intactos) e deixa-a «por confirmar»."""
        if not self.settings.get("translation")["enabled"]:
            raise ServiceError("A tradução automática está desativada (Definições → Idiomas).", 403)
        if target not in TARGETS:
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
            df = self.provider(r.provider).download(r)
            self._count_download(r.provider)
            v = validate_download(df.name, df.data, media)
            from .subtitles.parser import analyze
            from .evaluator import Factor
            n_chars = count_chars(v.parsed.cues)
            usage = deepl.usage()
            if usage.limit and n_chars > usage.remaining:
                raise ServiceError(f"Quota DeepL insuficiente: a tradução precisa de ~{n_chars} caracteres e restam {usage.remaining}.", 429)
            cues, sent = translate_cues(v.parsed.cues, target, src, deepl)
            text = to_srt(cues)
            tv = validate_download("traduzida.srt", text.encode("utf-8"), media)   # revalida o resultado
            s = self.settings.all()
            ev = evaluate_metadata(media, r, hi_policy=s["hi_policy"], prefer_forced=s["prefer_forced"])
            an = analyze(tv.parsed, media.get("duration"), size=len(text))
            ev = evaluate_content(ev, media, tv.parsed, an, self._reference_for(media, target))
            ev.score = max(0, ev.score - 15)
            ev.factors.append(Factor("Tradução automática (DeepL): qualidade inferior a uma legenda humana", -15, "negative"))
            ev.notes.append(f"Traduzida de «{src or 'idioma desconhecido'}» para «{target}»; os tempos são os da legenda original. "
                            "Itálicos e formatação foram removidos. Reveja antes de usar.")
            extra = {"translation": {"engine": "deepl", "source_lang": src, "target": target, "characters": sent,
                                      "source_file": df.name}}
            add_event(self.db, "translation", f"Tradução {src or '?'}→{target} ({sent} caracteres, DeepL)", "info", media_id)
            return self._finish_prepare(did, media, r, target, tv, ev, an, extra)
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
        add_event(self.db, "download", f"Legenda instalada: {os.path.basename(final)}", "info", media["id"])
        if reindex:
            lib = dict(self.db.one("SELECT * FROM libraries WHERE id=?", (media["library_id"],)))
            try:
                self.scanner.scan_library(lib, None, media["dir"], force=False)
            except Exception as e:  # noqa: BLE001
                log.warning("reanálise após instalar: %s", scrub(str(e)))
            if self.settings.get("notify_plex_after_install"):
                try:
                    self.refresh_plex(media["id"])
                except IntegrationError as e:
                    add_event(self.db, "integration", f"Plex não notificado: {e}", "warning", media["id"])
        return self.get_download(did)

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
        return bool(t["enabled"] and t["auto_fallback"] and lang in TARGETS and self.secrets.get("deepl_api_key"))

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
        s = self.settings.all()
        res: dict = {"media_id": m["id"], "searched": False}
        if s["auto_download"] and any(x.get("enabled") for x in s["providers"].values()):
            res["batch"] = self._job_batch(ctx, {"media_ids": [m["id"]], "install": not s["require_confirmation"], "min_tier": s["auto_min_tier"], "auto": True})
            res["searched"] = True
        ctx.step(os.path.basename(real), message="concluído", force=True)
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
