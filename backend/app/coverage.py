"""Cobertura de legendas por item, derivada da BD."""
from __future__ import annotations

from typing import Any

from .db import Database
from .settings import Settings


def usable(sub: dict, embedded_counts: bool) -> bool:
    if not sub["present"] or sub["forced"]:
        return False  # legendas forçadas só traduzem partes estrangeiras: não cobrem o idioma
    if sub["kind"] == "embedded":
        return embedded_counts and sub["lang"] is not None
    return sub["status"] == "ok" and sub["lang"] is not None and sub["association"] != "uncertain"


def compute(db: Database, settings: Settings, media_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    cfg = settings.all()
    embedded_counts = cfg["embedded_counts"]
    primary = [l["code"] for l in cfg["languages"] if l["priority"] == "primary"]
    all_langs = [l["code"] for l in cfg["languages"]]
    if media_ids is None:
        subs = db.query("SELECT * FROM subtitles WHERE media_id IS NOT NULL AND present=1")
        media = db.query("SELECT id FROM media_items WHERE present=1")
    else:
        q = ",".join("?" * len(media_ids)) or "NULL"
        subs = db.query(f"SELECT * FROM subtitles WHERE media_id IN ({q}) AND present=1", media_ids)
        media = [{"id": i} for i in media_ids]
    by_media: dict[int, list[dict]] = {}
    for s in subs:
        by_media.setdefault(s["media_id"], []).append(dict(s))
    out: dict[int, dict[str, Any]] = {}
    for m in media:
        mid = m["id"]
        ss = by_media.get(mid, [])
        covered: dict[str, str] = {}
        for s in ss:
            if usable(s, embedded_counts):
                src = "external" if s["kind"] == "external" else "embedded"
                if covered.get(s["lang"]) != "external":
                    covered[s["lang"]] = src
        fallback: dict[str, str] = {}
        gv = cfg.get("generic_pt_variant")
        if gv in ("pt-PT", "pt-BR") and "pt" in covered and gv in primary and gv not in covered:
            covered[gv] = covered["pt"]          # «pt» sem variante assumido como a variante escolhida (assinalado)
            fallback[gv] = "pt"
        if cfg.get("accept_pt_br_fallback") and "pt-PT" in primary and "pt-PT" not in covered and "pt-BR" in covered:
            covered["pt-PT"] = covered["pt-BR"]          # conta como cobertura, mas fica assinalado
            fallback["pt-PT"] = "pt-BR"
        missing = [l for l in primary if l not in covered]
        out[mid] = {
            "covered": covered,
            "fallback": fallback,
            "missing": missing,
            "missing_secondary": [l for l in all_langs if l not in covered and l not in primary],
            "has_any": bool(ss),
            "n_external": sum(1 for s in ss if s["kind"] == "external"),
            "n_embedded": sum(1 for s in ss if s["kind"] == "embedded"),
            "suspect": any(s["sync_status"] == "suspect" or s["status"] in ("invalid", "empty") for s in ss if s["kind"] == "external"),
            "unverified": any(s["sync_status"] == "unchecked" for s in ss if s["kind"] == "external" and s["status"] == "ok"),
            "unknown_lang": sum(1 for s in ss if s["lang"] is None),
        }
    return out
