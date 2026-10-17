"""Scanner incremental de bibliotecas. Só lê; nunca modifica ficheiros multimédia."""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffprobe as fp
from .db import Database
from .jobs import JobContext
from .languages import detect_language, normalize_lang
from .naming import SUB_EXTS, TEXT_SUB_EXTS, VIDEO_EXTS, MediaId, identify, parse_episode_tag, parse_subtitle_name
from .settings import Settings
from .subtitles.parser import MAX_SUB_BYTES, analyze, parse_bytes
from .util import add_event, log, now

SUB_DIRS = {"subs", "subtitles", "sub", "legendas", "subtitulos"}


class ScanAborted(Exception):
    pass


def _is_sub_dir(name: str) -> bool:
    return name.lower() in SUB_DIRS


def walk_library(root: str, ctx: JobContext | None = None):
    """Gera (dir, videos, subs) onde subs inclui as de subpastas Subs/."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@")) and d != "lost+found")
        if ctx:
            ctx.check_cancel()
        p = Path(dirpath)
        if _is_sub_dir(p.name):
            continue  # tratadas a partir da pasta-mãe
        videos, subs = [], []
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith("."):
                continue
            if ext in VIDEO_EXTS:
                videos.append(p / fn)
            elif ext in SUB_EXTS:
                subs.append(p / fn)
        for d in dirnames:
            if _is_sub_dir(d):
                for dp, _dn, fns in os.walk(p / d):
                    for fn in sorted(fns):
                        if os.path.splitext(fn)[1].lower() in SUB_EXTS and not fn.startswith("."):
                            subs.append(Path(dp) / fn)
        if videos or subs:
            yield p, videos, subs


def associate(video_paths: list[Path], sub: Path) -> tuple[Path | None, str]:
    """Escolhe o vídeo ao qual a legenda pertence. Devolve (vídeo, certeza)."""
    if not video_paths:
        return None, "uncertain"
    name = parse_subtitle_name(sub)
    base = name.stem_match.lower()
    for v in video_paths:
        if v.stem.lower() == base and v.parent == sub.parent:
            return v, "certain"
    for v in video_paths:
        if v.stem.lower() == base:
            return v, "certain"
    for v in video_paths:
        if base.startswith(v.stem.lower()) or v.stem.lower().startswith(base) and len(base) > 6:
            return v, "probable"
    tag = parse_episode_tag(sub.stem)
    if tag:
        hits = [v for v in video_paths if (t := parse_episode_tag(v.stem)) and t[:2] == tag[:2]]
        if len(hits) == 1:
            return hits[0], "probable"
        return None, "uncertain"
    if len(video_paths) == 1:
        return video_paths[0], "probable"
    return None, "uncertain"


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Scanner:
    def __init__(self, db: Database, settings: Settings, ffprobe_bin: str = "ffprobe", workers: int = 4):
        self.db, self.settings, self.ffprobe_bin, self.workers = db, settings, ffprobe_bin, workers
        self.ffprobe_ok = True

    # ------------------------------------------------------------------
    def scan_library(self, lib: dict, ctx: JobContext | None = None, subpath: str | None = None, force: bool = False) -> dict:
        root = lib["path"]
        if not os.path.isdir(root):
            raise ScanAborted(f"Pasta da biblioteca inacessível: {root} (share desmontada?)")
        scan_root = root
        if subpath:
            from .safepaths import require_within
            scan_root = require_within(subpath, [root])
        stats = {"videos": 0, "new": 0, "changed": 0, "unchanged": 0, "removed": 0, "subs": 0, "errors": 0, "probe_errors": 0}
        seen_media: set[str] = set()
        seen_subs: set[tuple[str, int | None]] = set()
        dirs = list(walk_library(scan_root, ctx))
        total_videos = sum(len(v) for _, v, _ in dirs)
        if ctx:
            ctx.set_total(total_videos or len(dirs))
        known = {r["path"]: r for r in self.db.query("SELECT * FROM media_items WHERE library_id=?", (lib["id"],))}
        lib_path = Path(root)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for d, videos, subs in dirs:
                if ctx:
                    ctx.check_cancel()
                self._scan_dir(lib, lib_path, d, videos, subs, known, seen_media, seen_subs, stats, pool, force, ctx)

        # removidos: só dentro da área analisada, e só se a árvore não parece desmontada
        prefix = scan_root.rstrip("/") + "/"
        gone = [p for p in known if (p == scan_root or p.startswith(prefix)) and p not in seen_media and known[p]["present"]]
        in_scope = [p for p in known if p.startswith(prefix)]
        if in_scope and not seen_media and len(gone) == len(in_scope) and len(in_scope) > 5:
            raise ScanAborted("A pasta aparenta estar vazia mas a BD tem itens: abortado para evitar apagar o inventário.")
        with self.db.tx() as c:
            for p in gone:
                c.execute("UPDATE media_items SET present=0 WHERE id=?", (known[p]["id"],))
                c.execute("UPDATE subtitles SET present=0 WHERE media_id=?", (known[p]["id"],))
            # legendas externas que desapareceram
            for r in c.execute("SELECT id,path,stream_index,kind FROM subtitles WHERE library_id=? AND present=1 AND path LIKE ?",
                               (lib["id"], prefix + "%")).fetchall():
                if (r["path"], r["stream_index"]) not in seen_subs and (r["kind"] == "external" or r["path"] in gone):
                    c.execute("UPDATE subtitles SET present=0 WHERE id=?", (r["id"],))
            c.execute("UPDATE libraries SET last_scan_at=? WHERE id=?", (now(), lib["id"]))
        stats["removed"] = len(gone)
        add_event(self.db, "scan", f"Biblioteca «{lib['name']}»: {stats['videos']} vídeos ({stats['new']} novos, "
                  f"{stats['changed']} alterados, {stats['removed']} removidos), {stats['subs']} legendas externas.")
        return stats

    # ------------------------------------------------------------------
    def _scan_dir(self, lib, lib_path, d, videos, subs, known, seen_media, seen_subs, stats, pool, force, ctx):
        # 1) vídeos
        to_probe = []
        media_by_path: dict[str, int] = {}
        for v in videos:
            try:
                st = v.stat()
            except OSError as e:
                stats["errors"] += 1
                log.warning("stat falhou %s: %s", v, e)
                continue
            sp = str(v)
            seen_media.add(sp)
            stats["videos"] += 1
            old = known.get(sp)
            mid = identify(v, lib_path, lib["type"])
            unchanged = old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1 and old["present"] and not force
            if unchanged:
                stats["unchanged"] += 1
                media_by_path[sp] = old["id"]
                # reprocessar se o ffprobe nunca correu e agora está disponível
                if old["probe_status"] == "pending" and self.ffprobe_ok:
                    to_probe.append((sp, old["id"]))
                if ctx:
                    ctx.step(v.name)
                continue
            stats["changed" if old else "new"] += 1
            media_id = self._upsert_media(lib, v, st, mid, old)
            media_by_path[sp] = media_id
            to_probe.append((sp, media_id))
            if ctx:
                ctx.step(v.name)
        # 2) ffprobe em paralelo
        probe_results = {}
        if to_probe and self.ffprobe_ok:
            futs = {sp: pool.submit(fp.probe, sp, self.ffprobe_bin) for sp, _ in to_probe}
            for sp, f in futs.items():
                probe_results[sp] = f.result()
            if all((not r.ok and r.error == "ffprobe não encontrado") for r in probe_results.values()):
                self.ffprobe_ok = False
                add_event(self.db, "scan", "ffprobe não encontrado: duração e legendas embutidas indisponíveis.", "warning")
        for sp, media_id in to_probe:
            r = probe_results.get(sp)
            if r is None:
                continue
            self._store_probe(lib, sp, media_id, r, stats)
        # 3) legendas externas
        all_vids = list(videos)
        for s in subs:
            sp = str(s)
            seen_subs.add((sp, None))
            stats["subs"] += 1
            try:
                self._scan_sub(lib, s, all_vids, media_by_path, force)
            except OSError as e:
                stats["errors"] += 1
                log.warning("legenda %s: %s", s, e)
        self._flag_duplicates(list(media_by_path.values()))

    def _upsert_media(self, lib, v: Path, st, mid: MediaId, old) -> int:
        with self.db.tx() as c:
            if old:
                c.execute(
                    "UPDATE media_items SET kind=?,title=?,year=?,series_title=?,season=?,episode=?,episode_title=?,size=?,mtime=?,"
                    "present=1,scanned_at=?,probe_status='pending',moviehash=NULL,dir=?,file_name=? WHERE id=?",
                    (mid.kind, mid.title, mid.year, mid.series_title, mid.season, mid.episode, mid.episode_title,
                     st.st_size, st.st_mtime, now(), str(v.parent), v.name, old["id"]))
                return old["id"]
            cur = c.execute(
                "INSERT INTO media_items(library_id,kind,path,dir,file_name,title,year,series_title,season,episode,episode_title,"
                "size,mtime,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lib["id"], mid.kind, str(v), str(v.parent), v.name, mid.title, mid.year, mid.series_title, mid.season,
                 mid.episode, mid.episode_title, st.st_size, st.st_mtime, now()))
            return cur.lastrowid

    def _store_probe(self, lib, sp: str, media_id: int, r: fp.ProbeResult, stats) -> None:
        with self.db.tx() as c:
            if not r.ok:
                stats["probe_errors"] += 1
                c.execute("UPDATE media_items SET probe_status=? WHERE id=?", (f"error", media_id))
                return
            c.execute("UPDATE media_items SET duration=?,video_codec=?,width=?,height=?,fps=?,probe_status='ok' WHERE id=?",
                      (r.duration, r.video_codec, r.width, r.height, r.fps, media_id))
            c.execute("UPDATE subtitles SET present=0 WHERE kind='embedded' AND path=?", (sp,))
            for s in r.subs:
                c.execute(
                    "INSERT INTO subtitles(media_id,library_id,kind,path,stream_index,format,lang,lang_source,forced,hi,text_based,status,association,present) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'ok','certain',1) ON CONFLICT(path,stream_index) DO UPDATE SET "
                    "media_id=excluded.media_id,format=excluded.format,lang=excluded.lang,lang_source=excluded.lang_source,"
                    "forced=excluded.forced,hi=excluded.hi,text_based=excluded.text_based,present=1",
                    (media_id, lib["id"], "embedded", sp, s.index, s.codec, s.lang, "stream" if s.lang else "unknown",
                     int(s.forced), int(s.hi), int(s.text_based)))
        # recalcular verificação temporal das externas desse vídeo com a duração nova
        self._recheck_external_sync(media_id)

    def _recheck_external_sync(self, media_id: int) -> None:
        m = self.db.one("SELECT duration FROM media_items WHERE id=?", (media_id,))
        if not m or not m["duration"]:
            return
        from .subtitles.parser import analyze_duration, ParsedSub, Cue
        for s in self.db.query("SELECT id,last_end_ms,first_start_ms,issues,status FROM subtitles WHERE media_id=? AND kind='external' AND status='ok'", (media_id,)):
            if s["last_end_ms"] is None:
                continue
            ps = ParsedSub("srt", [Cue(s["first_start_ms"] or 0, s["last_end_ms"], "x")])
            issues, sync, detail = analyze_duration(ps, m["duration"])
            import json
            base = [i for i in json.loads(s["issues"]) if i["code"] not in ("beyond_video", "truncated", "late_start")]
            base += [i.to_dict() for i in issues]
            self.db.execute("UPDATE subtitles SET issues=?, sync_status=CASE WHEN sync_status='aligned' THEN 'aligned' ELSE ? END, sync_detail=? WHERE id=?",
                            (json.dumps(base), sync, detail, s["id"]))

    def _scan_sub(self, lib, s: Path, videos: list[Path], media_by_path: dict[str, int], force: bool) -> None:
        import json
        st = s.stat()
        sp = str(s)
        v, assoc = associate(videos, s)
        media_id = media_by_path.get(str(v)) if v else None
        if v and media_id is None:
            r = self.db.one("SELECT id FROM media_items WHERE path=?", (str(v),))
            media_id = r["id"] if r else None
        name = parse_subtitle_name(s)
        old = self.db.one("SELECT * FROM subtitles WHERE path=? AND stream_index IS NULL AND kind='external'", (sp,))
        ext = s.suffix.lower()
        if old and old["size"] == st.st_size and abs((old["mtime"] or 0) - st.st_mtime) < 1 and not force:
            self.db.execute("UPDATE subtitles SET media_id=?, association=?, present=1 WHERE id=?",
                            (media_id, assoc if media_id else "uncertain", old["id"]))
            if media_id:
                self._recheck_external_sync(media_id)
            return
        text_based = ext in TEXT_SUB_EXTS
        parsed = None
        detected = None
        chash = None
        issues = []
        analysis = None
        dur = None
        if media_id:
            r = self.db.one("SELECT duration FROM media_items WHERE id=?", (media_id,))
            dur = r["duration"] if r else None
        if text_based:
            if st.st_size > MAX_SUB_BYTES:
                data = b""
                issues_extra = [{"code": "too_large", "severity": "error", "message": "Ficheiro demasiado grande para análise."}]
            else:
                data = s.read_bytes()
                issues_extra = []
            chash = _sha1(data) if data else None
            parsed = parse_bytes(data, ext) if data else None
            analysis = analyze(parsed, dur, size=st.st_size)
            issues = [i.to_dict() for i in analysis.issues] + issues_extra
            if parsed and parsed.cues:
                detected, conf = detect_language(parsed.text)
        else:
            # SUB/IDX: formato de imagem (VobSub) ou MicroDVD; não analisamos conteúdo
            analysis = None
        lang = name.lang
        lang_source = "filename" if lang else "unknown"
        if not lang and detected:
            lang, lang_source = detected, "content"
        if name.lang and detected and normalize_lang(detected) and not _compatible(name.lang, detected):
            issues.append({"code": "language_mismatch", "severity": "warning",
                           "message": f"O nome indica «{name.lang}» mas o conteúdo parece «{detected}»."})
        if media_id is None:
            issues.append({"code": "unassociated", "severity": "warning", "message": "Não foi possível associar a legenda a um vídeo com segurança."})
        elif assoc != "certain":
            issues.append({"code": "weak_association", "severity": "info", "message": "Associação ao vídeo baseada em heurística; confirme."})
        if v and media_id and not s.stem.lower().startswith(v.stem.lower()):
            issues.append({"code": "name_mismatch", "severity": "info", "message": "O nome do ficheiro não corresponde ao do vídeo (o Plex pode ignorá-la)."})
        if not lang and text_based:
            issues.append({"code": "unknown_language", "severity": "info", "message": "Idioma desconhecido."})
        vals = (media_id, lib["id"], "external", sp, None, ext.lstrip("."), lang, lang_source, detected,
                int(name.forced), int(name.hi), int(text_based), st.st_size, st.st_mtime,
                analysis.cue_count if analysis else None, analysis.first_start_ms if analysis else None,
                analysis.last_end_ms if analysis else None, analysis.status if analysis else ("ok" if not text_based else "invalid"),
                json.dumps(issues), assoc if media_id else "uncertain",
                analysis.sync_status if analysis else "unchecked", analysis.sync_detail if analysis else None, chash)
        with self.db.tx() as c:
            if old:
                c.execute("UPDATE subtitles SET media_id=?,library_id=?,kind=?,path=?,stream_index=?,format=?,lang=?,lang_source=?,detected_lang=?,"
                          "forced=?,hi=?,text_based=?,size=?,mtime=?,cue_count=?,first_start_ms=?,last_end_ms=?,status=?,issues=?,association=?,"
                          "sync_status=?,sync_detail=?,content_hash=?,present=1 WHERE id=?", vals + (old["id"],))
            else:
                c.execute("INSERT INTO subtitles(media_id,library_id,kind,path,stream_index,format,lang,lang_source,detected_lang,forced,hi,"
                          "text_based,size,mtime,cue_count,first_start_ms,last_end_ms,status,issues,association,sync_status,sync_detail,content_hash,present) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)", vals)

    def _flag_duplicates(self, media_ids: list[int]) -> None:
        import json
        for mid in media_ids:
            rows = self.db.query("SELECT id,lang,forced,hi,content_hash,issues FROM subtitles WHERE media_id=? AND kind='external' AND present=1", (mid,))
            groups: dict[tuple, list] = {}
            for r in rows:
                key = ("h", r["content_hash"]) if r["content_hash"] else ("l", r["lang"], r["forced"], r["hi"])
                groups.setdefault(key, []).append(r)
            for key, g in groups.items():
                dup = len(g) > 1 and (key[0] == "h" or key[1] is not None)
                for r in g:
                    issues = [i for i in json.loads(r["issues"]) if i["code"] != "duplicate"]
                    if dup:
                        issues.append({"code": "duplicate", "severity": "warning",
                                       "message": "Legenda duplicada (mesmo conteúdo, ou mesmo idioma/tipo para o mesmo vídeo)."})
                    self.db.execute("UPDATE subtitles SET issues=? WHERE id=?", (json.dumps(issues), r["id"]))


def _compatible(named: str, detected: str) -> bool:
    if named == detected:
        return True
    return named.split("-")[0] == detected.split("-")[0] and ("-" not in detected or "-" not in named or named == "pt" or detected == "pt")
