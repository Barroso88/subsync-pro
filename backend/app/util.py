from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .db import Database

log = logging.getLogger("subsync")

_SECRET_RE = re.compile(r"(api[_-]?key|token|password|authorization|x-plex-token)(=|:|\"\s*:\s*\")\s*[^\s&\"',]+", re.I)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrub(s: str) -> str:
    """Remove segredos de texto destinado a logs/erros."""
    s = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", s)
    return re.sub(r"X-Plex-Token=[^&\s]+", "X-Plex-Token=***", s)


def add_event(db: Database, kind: str, message: str, level: str = "info", media_id: int | None = None) -> None:
    db.execute("INSERT INTO events(ts,level,kind,message,media_id) VALUES(?,?,?,?,?)",
               (now(), level, kind, scrub(message)[:500], media_id))
