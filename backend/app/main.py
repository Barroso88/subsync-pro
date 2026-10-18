"""Aplicação FastAPI: API REST em /api, frontend estático na raiz."""

import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import coverage as cov
from .config import SECRET_KEYS, AppConfig
from .db import jloads, rows
from .languages import LANGS, lang_name
from .services import App, ServiceError
from .settings import DEFAULTS
from .util import log, scrub

VERSION = "1.0.0"


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json
        return json.dumps({"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                           "logger": record.name, "msg": scrub(record.getMessage())})


def setup_logging(cfg: AppConfig) -> None:
    h = logging.StreamHandler()
    h.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(cfg.log_level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # URLs podem conter tokens


# ---- autenticação opcional (palavra-passe única) -----------------------------

COOKIE = "subsync_session"


class LoginLimiter:
    def __init__(self):
        self.fails: dict[str, list[float]] = {}

    def check(self, ip: str) -> None:
        now = time.time()
        f = [t for t in self.fails.get(ip, []) if now - t < 600]
        self.fails[ip] = f
        if len(f) >= 8:
            raise HTTPException(429, "Demasiadas tentativas. Tente novamente dentro de alguns minutos.")

    def fail(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.time())


def make_token(secret: bytes, password: str) -> str:
    return hmac.new(secret, b"session:" + password.encode(), hashlib.sha256).hexdigest()


def create_app(cfg: AppConfig | None = None, transport=None) -> FastAPI:
    cfg = cfg or AppConfig()
    setup_logging(cfg)
    core = App(cfg, transport=transport)
    limiter = LoginLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        core.start()
        log.info("SubSync Pro %s iniciado (dados em %s)", VERSION, cfg.data_dir)
        yield
        core.stop()

    app = FastAPI(title="SubSync Pro API", version=VERSION, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.core = core

    @app.exception_handler(ServiceError)
    async def _svc(_: Request, e: ServiceError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, e: Exception):
        log.error("erro não tratado: %s", e, exc_info=True)
        return JSONResponse({"detail": "Erro interno. Consulte os logs."}, status_code=500)

    expected = make_token(core.secrets.session_secret(), cfg.auth_password) if cfg.auth_password else None

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        p = request.url.path
        if expected and p.startswith("/api") and p not in ("/api/health", "/api/auth/login", "/api/auth/status") and not (p.startswith("/api/webhooks/") and p != "/api/webhooks/info"):
            tok = request.cookies.get(COOKIE, "")
            if not hmac.compare_digest(tok, expected):
                return JSONResponse({"detail": "Não autenticado"}, status_code=401)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    api = APIRouter(prefix="/api")

    # ---- saúde / auth ------------------------------------------------------
    @api.get("/health")
    def health():
        core.db.scalar("SELECT 1")
        return {"status": "ok", "version": VERSION}

    class Login(BaseModel):
        password: str

    @api.get("/auth/status")
    def auth_status(request: Request):
        ok = (not expected) or hmac.compare_digest(request.cookies.get(COOKIE, ""), expected)
        return {"auth_required": bool(expected), "authenticated": ok}

    @api.post("/auth/login")
    def login(body: Login, request: Request, response: Response):
        if not expected:
            return {"ok": True}
        ip = request.client.host if request.client else "?"
        limiter.check(ip)
        if not hmac.compare_digest(body.password, cfg.auth_password):
            limiter.fail(ip)
            raise HTTPException(401, "Palavra-passe incorreta")
        response.set_cookie(COOKIE, expected, httponly=True, samesite="strict", max_age=30 * 86400)
        return {"ok": True}

    @api.post("/auth/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ---- meta ---------------------------------------------------------------
    @api.get("/meta")
    def meta():
        s = core.settings.all()
        extra = {e["code"]: e["name"] for e in s.get("extra_languages", [])}
        langs = [{"code": c, "name": n[0]} for c, n in LANGS.items()] + [{"code": c, "name": n} for c, n in extra.items()]
        return {"version": VERSION, "languages": langs, "auth_required": bool(expected),
                "ffprobe": core.scanner.ffprobe_ok, "allowed_roots": cfg.allowed_roots,
                "writable_roots": core.writable_roots()}

    # ---- bibliotecas --------------------------------------------------------
    class LibIn(BaseModel):
        name: str = Field(max_length=80)
        type: str
        path: str = Field(max_length=1024)
        writable: bool = False

    class LibPatch(BaseModel):
        name: str | None = None
        writable: bool | None = None

    @api.get("/libraries")
    def libraries():
        return core.libraries()

    @api.post("/libraries", status_code=201)
    def add_library(b: LibIn):
        return {"id": core.add_library(b.name, b.type, b.path, b.writable)}

    @api.patch("/libraries/{lid}")
    def patch_library(lid: int, b: LibPatch):
        core.update_library(lid, b.name, b.writable)
        return {"ok": True}

    @api.delete("/libraries/{lid}")
    def del_library(lid: int):
        core.delete_library(lid)
        return {"ok": True}

    @api.get("/browse")
    def browse(path: str = ""):
        """Navegador de pastas para escolher bibliotecas (limitado às raízes permitidas)."""
        from .safepaths import PathSecurityError, require_within
        if not path:
            return {"path": "", "dirs": [r for r in cfg.allowed_roots if os.path.isdir(r)], "roots": True}
        try:
            real = require_within(path, cfg.allowed_roots)
        except PathSecurityError:
            raise HTTPException(403, "Fora das pastas permitidas")
        if not os.path.isdir(real):
            raise HTTPException(404, "Pasta inexistente")
        dirs = sorted(os.path.join(real, d.name) for d in os.scandir(real) if d.is_dir(follow_symlinks=False) and not d.name.startswith("."))
        return {"path": real, "dirs": dirs, "writable": os.access(real, os.W_OK), "roots": False}

    # ---- scanner / tarefas ---------------------------------------------------
    class ScanIn(BaseModel):
        library_id: int | None = None
        subpath: str | None = None
        force: bool = False
        force_subs: bool = False

    @api.post("/scan", status_code=202)
    def scan(b: ScanIn):
        return {"job_id": core.start_scan(b.library_id, b.subpath, b.force, force_subs=b.force_subs)}

    @api.post("/media/{mid}/rescan", status_code=202)
    def rescan(mid: int):
        m = core.media_dict(mid)
        return {"job_id": core.start_scan(m["library_id"], m["dir"])}

    @api.post("/revalidate", status_code=202)
    def revalidate():
        return {"job_id": core.jobs.submit("revalidate", "Revalidar legendas existentes", {}, dedupe=True)}

    @api.get("/jobs")
    def jobs(status: str | None = None, type: str | None = None, limit: int = Query(100, le=500), offset: int = 0):
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if type:
            where.append("type=?"); args.append(type)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = core.db.scalar(f"SELECT COUNT(*) FROM jobs {w}", args)
        items = []
        for r in core.db.query(f"SELECT * FROM jobs {w} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset)):
            d = dict(r)
            d["params"] = jloads(d["params"], {})
            d["result"] = jloads(d["result"])
            items.append(d)
        return {"total": total, "items": items}

    @api.get("/jobs/{jid}")
    def job(jid: int):
        r = core.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "Tarefa não encontrada")
        d = dict(r)
        d["params"], d["result"] = jloads(d["params"], {}), jloads(d["result"])
        return d

    @api.post("/jobs/{jid}/cancel")
    def cancel(jid: int):
        return {"cancelled": core.jobs.cancel(jid)}

    @api.post("/jobs/{jid}/retry", status_code=202)
    def retry(jid: int):
        r = core.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r or r["status"] in ("queued", "running"):
            raise HTTPException(409, "Tarefa não pode ser repetida")
        return {"job_id": core.jobs.submit(r["type"], r["title"], jloads(r["params"], {}))}

    # ---- itens de média ------------------------------------------------------
    def _lib_filter(args: list, library_type: str | None) -> str:
        if library_type:
            args.append(library_type)
            return " AND m.library_id IN (SELECT id FROM libraries WHERE type=?)"
        return ""

    def _item_view(m: dict, c: dict | None) -> dict:
        c = c or {}
        d = {k: m[k] for k in ("id", "kind", "title", "year", "series_title", "season", "episode", "episode_title",
                               "file_name", "path", "duration", "width", "height", "video_codec", "fps", "plex_key",
                               "library_id", "imdb_id", "tmdb_id", "radarr_id", "radarr_title") if k in m}
        limit = datetime.now(timezone.utc).timestamp() + 86400
        added = m.get("added_at")
        if added:
            try:
                if datetime.fromisoformat(added.replace("Z", "+00:00")).timestamp() > limit:
                    added = None                                     # data no futuro: não é uma data de adição real
            except ValueError:
                added = None
        if not added and m.get("mtime") and m["mtime"] <= limit:
            added = datetime.fromtimestamp(m["mtime"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d["added_at"] = added
        d["file_title"] = m.get("title")                       # título deduzido do nome do ficheiro
        d["title"] = (m.get("radarr_title") or "").strip() or m.get("title")   # título principal: o do Radarr (ex.: em português), se existir
        d["has_poster"] = bool(m.get("poster"))
        d["covered"] = c.get("covered", {})
        d["missing"] = c.get("missing", [])
        d["fallback"] = c.get("fallback", {})
        d["n_external"], d["n_embedded"] = c.get("n_external", 0), c.get("n_embedded", 0)
        d["suspect"], d["unverified"] = c.get("suspect", False), c.get("unverified", False)
        d["unknown_lang"] = c.get("unknown_lang", 0)
        return d

    # data de adição: Plex > Radarr/Sonarr (ficheiro importado) > data de modificação do ficheiro
    # datas no futuro (ex.: 2098, usadas por vezes para «fixar» filmes no topo) não são datas de adição reais: ignoram-se
    _ADDED = ("CASE WHEN m.added_at IS NOT NULL AND m.added_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now','+1 day') THEN m.added_at "
              "WHEN m.mtime <= CAST(strftime('%s','now') AS REAL) + 86400 THEN strftime('%Y-%m-%dT%H:%M:%SZ', m.mtime, 'unixepoch') ELSE NULL END")

    def _filtered_media(kind, q, lang, status, series, season, sort) -> list[dict]:
        args: list[Any] = []
        where = ["m.present=1"]
        if kind in ("movie", "episode"):
            where.append("m.kind=?"); args.append(kind)
        if series:
            where.append("m.series_title=?"); args.append(series)
        if season is not None:
            where.append("m.season=?"); args.append(season)
        if q:
            like = f"%{q.lower()}%"
            where.append("(lower(m.title) LIKE ? OR lower(coalesce(m.radarr_title,'')) LIKE ? OR lower(m.file_name) LIKE ? OR lower(coalesce(m.series_title,'')) LIKE ? "
                         "OR CAST(m.year AS TEXT)=? OR lower(coalesce(m.episode_title,'')) LIKE ? OR "
                         "EXISTS(SELECT 1 FROM subtitles s WHERE s.media_id=m.id AND s.present=1 AND (lower(coalesce(s.lang,'')) LIKE ? OR lower(s.path) LIKE ?)))")
            args += [like, like, like, like, q.strip(), like, like, like]
        order = {"title": "lower(coalesce(m.series_title, nullif(m.radarr_title,''), m.title)), m.season, m.episode, lower(coalesce(nullif(m.radarr_title,''), m.title))", "year": "m.year DESC, lower(m.title)",
                 "added": f"({_ADDED}) IS NULL, {_ADDED} DESC, m.id DESC", "recent": f"({_ADDED}) IS NULL, {_ADDED} DESC, m.id DESC", "added_old": f"({_ADDED}) IS NULL, {_ADDED} ASC, m.id ASC"}.get(sort, "lower(m.title)")
        rows_ = core.db.query(f"SELECT m.* FROM media_items m WHERE {' AND '.join(where)} ORDER BY {order}", args)
        covs = cov.compute(core.db, core.settings)  # derivado dos dados reais
        out = []
        for r in rows_:
            c = covs.get(r["id"], {})
            covered = c.get("covered", {})
            miss = (lang not in covered) if lang else bool(c.get("missing"))
            nsubs = c.get("n_external", 0) + c.get("n_embedded", 0)
            if status == "genericpt":      # em falta mas com «pt» sem variante identificada
                if not miss or "pt" not in covered:
                    continue
            elif status in ("missing", "none", "other"):
                if not miss or (status == "none" and nsubs) or (status == "other" and not nsubs):
                    continue
            elif status == "has":
                if (lang and lang not in covered) or (not lang and not covered):
                    continue
            elif status == "suspect":
                if not c.get("suspect"):
                    continue
            elif status == "unverified":
                if not c.get("unverified"):
                    continue
            elif lang and lang not in covered:
                continue
            out.append(_item_view(dict(r), c))
        return out

    @api.get("/media")
    def media_list(kind: str | None = None, q: str | None = None, lang: str | None = None,
                   status: str | None = None, series: str | None = None, season: int | None = None,
                   sort: str = "title", limit: int = Query(60, le=500), offset: int = 0):
        """status: genericpt | missing | none (sem nenhuma legenda) | other (só noutros idiomas) | has | suspect | unverified"""
        out = _filtered_media(kind, q, lang, status, series, season, sort)
        return {"total": len(out), "items": out[offset: offset + limit]}

    @api.get("/media/ids")
    def media_ids(kind: str | None = None, q: str | None = None, lang: str | None = None, status: str | None = None,
                  series: str | None = None, season: int | None = None):
        """Ids de TODOS os itens que cumprem o filtro (para «selecionar todos»)."""
        return {"ids": [i["id"] for i in _filtered_media(kind, q, lang, status, series, season, "title")][:6000]}

    @api.get("/media/{mid}")
    def media_detail(mid: int):
        m = core.media_dict(mid)
        c = cov.compute(core.db, core.settings, [mid]).get(mid)
        subs = []
        for s in core.db.query("SELECT * FROM subtitles WHERE media_id=? AND present=1 ORDER BY kind, lang", (mid,)):
            d = dict(s)
            d["issues"] = jloads(d["issues"], [])
            d.pop("content_hash", None)
            subs.append(d)
        dls = [core.get_download(r["id"]) for r in core.db.query("SELECT id FROM downloads WHERE media_id=? ORDER BY id DESC LIMIT 20", (mid,))]
        v = _item_view(m, c)
        v.update({"dir": m["dir"], "size": m["size"], "probe_status": m["probe_status"], "subtitles": subs, "downloads": dls,
                  "moviehash": m["moviehash"], "library_writable": any(l["effective_write"] and l["id"] == m["library_id"] for l in core.libraries())})
        return v

    @api.get("/media/{mid}/poster")
    def poster(mid: int):
        r = core.fetch_poster(mid)
        if not r:
            raise HTTPException(404, core.poster_error or "Capa indisponível")
        return Response(r[0], media_type=r[1], headers={"Cache-Control": "private, max-age=86400", "X-Poster-Source": core.poster_source})

    @api.get("/series")
    def series_list(q: str | None = None):
        args: list[Any] = []
        w = ""
        if q:
            w = "AND lower(series_title) LIKE ?"
            args.append(f"%{q.lower()}%")
        covs = cov.compute(core.db, core.settings)
        out: dict[str, dict] = {}
        for r in core.db.query(f"SELECT id,series_title,season,poster,year FROM media_items WHERE present=1 AND kind='episode' {w} ORDER BY lower(series_title)", args):
            s = out.setdefault(r["series_title"], {"title": r["series_title"], "year": r["year"], "episodes": 0, "missing": 0,
                                                    "seasons": set(), "poster_id": None})
            s["episodes"] += 1
            s["seasons"].add(r["season"])
            s["missing"] += 1 if covs.get(r["id"], {}).get("missing") else 0
            if r["poster"] and not s["poster_id"]:
                s["poster_id"] = r["id"]
        res = []
        for s in out.values():
            s["seasons"] = len([x for x in s["seasons"] if x is not None])
            res.append(s)
        return res

    @api.get("/series/episodes")
    def series_episodes(title: str):
        items = core.db.query("SELECT * FROM media_items WHERE present=1 AND kind='episode' AND series_title=? ORDER BY season, episode", (title,))
        covs = cov.compute(core.db, core.settings, [r["id"] for r in items])
        return [_item_view(dict(r), covs.get(r["id"])) for r in items]

    # ---- legendas: pesquisa / download -----------------------------------------
    class SearchIn(BaseModel):
        languages: list[str] | None = None

    @api.post("/media/{mid}/search")
    def search(mid: int, b: SearchIn | None = None):
        return core.search(mid, b.languages if b else None)

    class PrepareIn(BaseModel):
        result: dict
        language: str | None = None

    @api.post("/media/{mid}/prepare")
    def prepare(mid: int, b: PrepareIn):
        return core.prepare(mid, b.result, b.language)

    class TranslateIn(BaseModel):
        result: dict
        target: str

    class BazarrReq(BaseModel):
        language: str

    class BazarrBatch(BaseModel):
        media_ids: list[int]
        languages: list[str] | None = None

    @api.post("/media/{mid}/bazarr")
    def bazarr_one(mid: int, b: BazarrReq):
        return core.bazarr_request(mid, b.language)

    @api.post("/batch/bazarr", status_code=202)
    def bazarr_batch(b: BazarrBatch):
        return {"job_id": core.start_bazarr_batch(b.media_ids, b.languages)}

    @api.get("/translation")
    def translation_status():
        return core.translation_status()

    @api.get("/translation/models")
    def translation_models():
        return core.gemini_models()

    @api.post("/translation/test")
    def translation_test():
        return core.translation_test()

    @api.post("/media/{mid}/translate/start", status_code=202)
    def translate_start(mid: int, b: TranslateIn):
        return {"job_id": core.start_translate(mid, b.result, b.target)}

    @api.post("/media/{mid}/translate")
    def translate(mid: int, b: TranslateIn):
        return core.translate_prepare(mid, b.result, b.target)

    class InstallIn(BaseModel):
        policy: str = "fail"

    @api.post("/downloads/{did}/install")
    def install(did: int, b: InstallIn):
        return core.install(did, b.policy)

    @api.post("/downloads/{did}/discard")
    def discard(did: int):
        core.discard(did)
        return {"ok": True}

    @api.get("/downloads")
    def downloads(status: str | None = None, provider: str | None = None, q: str | None = None,
                  limit: int = Query(100, le=500), offset: int = 0):
        where, args = [], []
        if status:
            where.append("d.status=?"); args.append(status)
        if provider:
            where.append("d.provider=?"); args.append(provider)
        if q:
            where.append("(lower(coalesce(m.title,'')) LIKE ? OR lower(coalesce(d.release,'')) LIKE ? OR lower(coalesce(d.dest_path,'')) LIKE ?)")
            args += [f"%{q.lower()}%"] * 3
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = core.db.scalar(f"SELECT COUNT(*) FROM downloads d LEFT JOIN media_items m ON m.id=d.media_id {w}", args)
        ids = [r["id"] for r in core.db.query(f"SELECT d.id FROM downloads d LEFT JOIN media_items m ON m.id=d.media_id {w} ORDER BY d.id DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
        return {"total": total, "items": [core.get_download(i) for i in ids]}

    @api.get("/downloads/{did}")
    def download(did: int):
        return core.get_download(did)

    class BatchPreviewIn(BaseModel):
        media_ids: list[int]
        languages: list[str] | None = None

    class BatchIn(BatchPreviewIn):
        install: bool = False
        min_tier: str = "probable"

    @api.post("/batch/preview")
    def batch_preview(b: BatchPreviewIn):
        return core.batch_preview(b.media_ids, b.languages)

    @api.post("/batch", status_code=202)
    def batch(b: BatchIn):
        return {"job_id": core.start_batch(b.media_ids, b.languages, b.install, b.min_tier)}

    # ---- webhooks do Radarr/Sonarr (autenticados por basic auth ou X-Webhook-Token; não usam a sessão da UI) ----
    def _webhook_auth(request: Request) -> None:
        import base64
        tok = core.webhook_token()
        given = request.headers.get("x-webhook-token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                given = base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)[-1]
            except Exception:  # noqa: BLE001
                given = ""
        ip = request.client.host if request.client else "?"
        limiter.check(ip)
        if not given or not hmac.compare_digest(given, tok):
            limiter.fail(ip)
            raise HTTPException(401, "Token de webhook inválido")

    @api.post("/webhooks/{source}", status_code=202)
    async def webhook(source: str, request: Request):
        if source not in ("radarr", "sonarr"):
            raise HTTPException(404)
        _webhook_auth(request)
        if int(request.headers.get("content-length") or 0) > 1_000_000:
            raise HTTPException(413, "Payload demasiado grande")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "JSON inválido")
        if not isinstance(payload, dict):
            raise HTTPException(400, "JSON inválido")
        return core.handle_webhook(source, payload)

    @api.get("/webhooks/info")
    def webhook_info(request: Request):
        base = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
        return {"token": core.webhook_token(), "username": "subsync",
                "urls": {"radarr": f"{base}/api/webhooks/radarr", "sonarr": f"{base}/api/webhooks/sonarr"}}

    # ---- fornecedores / integrações / definições -------------------------------
    @api.get("/providers")
    def providers():
        return core.provider_status()

    @api.post("/providers/{name}/test")
    def provider_test(name: str):
        return core.provider(name).test_connection()

    class SecretsIn(BaseModel):
        values: dict[str, str]

    @api.put("/secrets")
    def set_secrets(b: SecretsIn):
        for k, v in b.values.items():
            if k not in SECRET_KEYS:
                raise HTTPException(400, f"Segredo desconhecido: {k}")
            core.secrets.set(k, v.strip())
        core._providers.clear()
        core.jobs  # noqa
        from .providers.opensubtitles import OpenSubtitles
        OpenSubtitles._token = None
        return {"ok": True}

    @api.get("/integrations")
    def integrations():
        s = core.settings.all()
        out = {}
        for n, key in (("plex", "plex_token"), ("radarr", "radarr_api_key"), ("sonarr", "sonarr_api_key"), ("bazarr", "bazarr_api_key")):
            out[n] = {**s[n], "secret_set": bool(core.secrets.get(key)), "secret_source": core.secrets.source(key), "secret_key": key}
        return out

    @api.post("/integrations/{name}/test")
    def integration_test(name: str):
        if name not in ("plex", "radarr", "sonarr", "bazarr"):
            raise HTTPException(404)
        return core.test_integration(name)

    @api.post("/integrations/sync", status_code=202)
    def integrations_sync():
        return {"job_id": core.jobs.submit("sync_integrations", "Sincronizar metadados (Plex/Radarr/Sonarr)", {}, dedupe=True)}

    @api.get("/settings")
    def get_settings():
        return core.settings.all()

    @api.put("/settings")
    def put_settings(body: dict[str, Any]):
        for k, v in body.items():
            if k not in DEFAULTS:
                raise HTTPException(400, f"Definição desconhecida: {k}")
            _validate_setting(k, v)
        core.settings.update(body)
        core._providers.clear()
        return core.settings.all()

    def _validate_setting(k: str, v: Any) -> None:
        from .integrations.common import IntegrationError, validate_service_url
        from .languages import LANGS as L
        if k == "languages":
            extra = {e["code"] for e in core.settings.get("extra_languages")}
            if not isinstance(v, list) or any(not isinstance(i, dict) or i.get("priority") not in ("primary", "secondary")
                                               or (i.get("code") not in L and i.get("code") not in extra) for i in v):
                raise HTTPException(400, "Lista de idiomas inválida")
        elif k in ("plex", "radarr", "sonarr", "bazarr"):
            if v.get("url"):
                try:
                    validate_service_url(v["url"])
                except IntegrationError as e:
                    raise HTTPException(400, str(e))
        elif k == "translation":
            if not isinstance(v, dict) or not isinstance(v.get("source_languages", []), list):
                raise HTTPException(400, "Definições de tradução inválidas")
            if v.get("engine", "deepl") not in ("deepl", "gemini") or not isinstance(v.get("gemini_model", ""), str) or len(v.get("gemini_model", "")) > 80:
                raise HTTPException(400, "Motor ou modelo de tradução inválido")
        elif k == "generic_pt_variant" and v not in ("", "pt-PT", "pt-BR"):
            raise HTTPException(400, "generic_pt_variant inválido")
        elif k == "hi_policy" and v not in ("include", "exclude", "only"):
            raise HTTPException(400, "hi_policy inválido")
        elif k == "scan_interval_minutes" and (not isinstance(v, int) or v < 0 or v > 10080):
            raise HTTPException(400, "Intervalo inválido")

    # ---- dashboard / atividade -------------------------------------------------
    @api.get("/dashboard")
    def dashboard():
        db = core.db
        covs = cov.compute(db, core.settings)
        s = core.settings.all()
        total = len(covs)
        movies = db.scalar("SELECT COUNT(*) FROM media_items WHERE present=1 AND kind='movie'")
        episodes = db.scalar("SELECT COUNT(*) FROM media_items WHERE present=1 AND kind='episode'")
        series = db.scalar("SELECT COUNT(DISTINCT series_title) FROM media_items WHERE present=1 AND kind='episode'")
        with_subs = sum(1 for v in covs.values() if v["covered"])
        missing_pref = sum(1 for v in covs.values() if v["missing"])
        suspect = sum(1 for v in covs.values() if v["suspect"])
        unverified = sum(1 for v in covs.values() if v["unverified"])
        # mesmo cálculo da «cobertura por idioma» (coverage.compute): cartões e barras coincidem sempre
        kinds = {r["id"]: r["kind"] for r in db.query("SELECT id,kind FROM media_items WHERE present=1")}
        by_variant = {}
        for k in ("pt-PT", "pt-BR"):
            ids = [i for i, c in covs.items() if k in c["covered"]]
            by_variant[k] = {
                "total": len(ids), "pct": round(100 * len(ids) / total, 1) if total else 0,
                "movies": sum(1 for i in ids if kinds.get(i) == "movie"), "episodes": sum(1 for i in ids if kinds.get(i) == "episode"),
                "assumed": sum(1 for i in ids if k in covs[i].get("fallback", {})),     # incluídos por assunção (≈pt / ≈pt-BR)
            }
        by_lang = []
        for l in s["languages"]:
            have = sum(1 for v in covs.values() if l["code"] in v["covered"])
            by_lang.append({"code": l["code"], "name": lang_name(l["code"]), "priority": l["priority"], "have": have,
                            "total": total, "pct": round(100 * have / total, 1) if total else 0})
        per_day = rows(db.query("SELECT substr(installed_at,1,10) AS day, COUNT(*) AS n FROM downloads WHERE status='installed' "
                                "AND installed_at >= date('now','-29 day') GROUP BY day ORDER BY day"))
        recent = rows(db.query("SELECT * FROM events ORDER BY id DESC LIMIT 12"))
        return {
            "totals": {"movies": movies, "series": series, "episodes": episodes, "files": total,
                       "with_subtitles": with_subs, "missing_preferred": missing_pref,
                       "suspect": suspect, "unverified": unverified,
                       "downloads": db.scalar("SELECT COUNT(*) FROM downloads WHERE status='installed'"),
                       "libraries": db.scalar("SELECT COUNT(*) FROM libraries")},
            "coverage_by_language": by_lang, "by_variant": by_variant, "downloads_per_day": per_day, "recent": recent,
            "last_scan_at": db.scalar("SELECT MAX(last_scan_at) FROM libraries", default=None),
        }

    @api.get("/sidebar")
    def sidebar():
        covs = cov.compute(core.db, core.settings)
        langs = [l["code"] for l in core.settings.get("languages")]
        return {"missing": sum(1 for v in covs.values() if v["missing"]),
                "missing_by_lang": {c: sum(1 for v in covs.values() if c not in v["covered"]) for c in langs},
                "duplicate_subtitles": core.duplicate_count(),
                "empty_subtitles": core.db.scalar("SELECT COUNT(*) FROM subtitles WHERE kind='external' AND status='empty' AND present=1"),
                "active_jobs": core.db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"),
                "pending_downloads": core.db.scalar("SELECT COUNT(*) FROM downloads WHERE status='prepared'")}

    class ToolIn(BaseModel):
        action: str
        params: dict = {}

    class ApplyIn(ToolIn):
        base_hash: str

    class RestoreIn(BaseModel):
        force: bool = False

    @api.get("/subtitles/{sid:int}/cues")
    def sub_cues(sid: int, head: int = Query(30, le=200), tail: int = Query(30, le=200)):
        return core.sub_cues(sid, head, tail)

    @api.post("/subtitles/{sid:int}/preview")
    def sub_preview(sid: int, b: ToolIn):
        return core.sub_preview(sid, b.action, b.params)

    @api.post("/subtitles/{sid:int}/apply")
    def sub_apply(sid: int, b: ApplyIn):
        return core.sub_apply(sid, b.action, b.params, b.base_hash)

    @api.post("/subtitles/{sid:int}/delete")
    def sub_delete(sid: int):
        return core.sub_delete(sid)

    @api.get("/subtitle-edits")
    def subtitle_edits(media_id: int):
        return core.edits_for(media_id)

    @api.post("/subtitle-edits/{eid}/restore")
    def restore_edit(eid: int, b: RestoreIn):
        return core.restore_edit(eid, b.force)

    class DupItem(BaseModel):
        keep: int
        remove: list[int] = Field(max_length=50)

    class ResolveDupIn(BaseModel):
        items: list[DupItem] = Field(max_length=1000)

    @api.get("/subtitles/duplicates")
    def duplicates(q: str | None = None, limit: int = Query(100, le=500), offset: int = 0):
        g = core.duplicate_groups(q)
        return {"total": len(g), "removable": sum(len(x["files"]) - 1 for x in g if x["deletable"] and not x["lang_conflict"]), "conflicts": sum(1 for x in g if x["lang_conflict"]), "groups": g[offset: offset + limit]}

    @api.get("/subtitles/duplicates/all")
    def duplicates_all(q: str | None = None):
        """Propostas (ficar/remover) de TODOS os grupos apagáveis, para «selecionar todos»."""
        return {"items": [{"keep": next(f["id"] for f in x["files"] if f["keep"]), "remove": [f["id"] for f in x["files"] if not f["keep"]]}
                          for x in core.duplicate_groups(q) if x["deletable"] and not x["lang_conflict"]][:1000]}

    @api.post("/subtitles/duplicates/resolve")
    def resolve_dups(b: ResolveDupIn):
        return core.resolve_duplicates([i.model_dump() for i in b.items])

    class ConvertBatchIn(BaseModel):
        ids: list[int] = Field(max_length=5000)
        fix_mojibake: bool = True
        keep_original: bool = False

    @api.post("/conversions/scan", status_code=202)
    def convert_scan():
        return {"job_id": core.start_convert_scan()}

    @api.post("/conversions/apply", status_code=202)
    def convert_apply(b: ConvertBatchIn):
        return {"job_id": core.start_convert_batch(b.ids, {"fix_mojibake": b.fix_mojibake, "keep_original": b.keep_original})}

    class DeleteEmptyIn(BaseModel):
        ids: list[int] = Field(max_length=5000)

    @api.get("/subtitles/empty")
    def empty_subs(q: str | None = None, lang: str | None = None, limit: int = Query(100, le=500), offset: int = 0):
        return core.empty_subtitles(q, lang, limit, offset)

    @api.get("/subtitles/empty/ids")
    def empty_subs_ids(q: str | None = None, lang: str | None = None):
        return {"ids": core.empty_subtitle_ids(q, lang)}

    @api.post("/subtitles/empty/delete")
    def delete_empty(b: DeleteEmptyIn):
        return core.delete_empty_subtitles(b.ids)

    @api.get("/events")
    def events(limit: int = Query(50, le=500)):
        return rows(core.db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    app.include_router(api)

    # ---- frontend estático -------------------------------------------------------
    static = cfg.static_dir
    if static and static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")   # ficheiros com hash no nome

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404)
            f = (static / full_path).resolve()
            if full_path and f.is_file() and static.resolve() in f.parents:
                return FileResponse(f, headers={"Cache-Control": "public, max-age=3600"})
            # a página principal nunca fica em cache: garante que o browser carrega sempre o JS mais recente
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
