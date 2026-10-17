"""Gestor de tarefas em background (threads), persistido na BD.

- Estado/progresso visíveis na API e sobrevivem ao reinício (tarefas a meio ficam `interrupted`).
- Cancelamento cooperativo via JobContext.check_cancel().
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .db import Database
from .util import add_event, log, now, scrub


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, mgr: "JobManager", job_id: int):
        self.mgr, self.id = mgr, job_id
        self.cancel_event = mgr._events[job_id]
        self._last = 0.0
        self.progress = 0
        self.total = 0
        self.started = time.monotonic()

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def set_total(self, total: int) -> None:
        self.total += total   # acumula (um scan pode percorrer várias bibliotecas)
        self.mgr.db.execute("UPDATE jobs SET total=? WHERE id=?", (self.total, self.id))

    def step(self, current: str | None = None, inc: int = 1, message: str | None = None, force: bool = False) -> None:
        self.check_cancel()
        self.progress += inc
        t = time.monotonic()
        if force or t - self._last > 0.4:
            self._last = t
            rate = self.progress / max(t - self.started, 0.001)
            msg = message if message is not None else f"{rate:.1f} itens/s"
            self.mgr.db.execute("UPDATE jobs SET progress=?, current=?, message=? WHERE id=?",
                                (self.progress, current, msg, self.id))


class JobManager:
    def __init__(self, db: Database, max_workers: int = 2):
        self.db = db
        self.pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="job")
        self._events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.handlers: dict[str, Callable[[JobContext, dict], Any]] = {}

    def register(self, jtype: str, fn: Callable[[JobContext, dict], Any]) -> None:
        self.handlers[jtype] = fn

    def mark_interrupted(self) -> None:
        self.db.execute(
            "UPDATE jobs SET status='interrupted', finished_at=?, error='Interrompida pelo reinício' "
            "WHERE status IN ('queued','running')", (now(),))

    def submit(self, jtype: str, title: str, params: dict | None = None, *, dedupe: bool = False) -> int:
        params = params or {}
        if dedupe:
            ex = self.db.one("SELECT id FROM jobs WHERE type=? AND status IN ('queued','running') AND params=?",
                             (jtype, json.dumps(params, sort_keys=True)))
            if ex:
                return ex["id"]
        cur = self.db.execute(
            "INSERT INTO jobs(type,title,status,params,created_at) VALUES(?,?,?,?,?)",
            (jtype, title, "queued", json.dumps(params, sort_keys=True), now()))
        jid = cur.lastrowid
        with self._lock:
            self._events[jid] = threading.Event()
        self.pool.submit(self._run, jid, jtype, params)
        return jid

    def _run(self, jid: int, jtype: str, params: dict) -> None:
        ev = self._events[jid]
        if ev.is_set():
            self.db.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (now(), jid))
            return
        self.db.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (now(), jid))
        ctx = JobContext(self, jid)
        try:
            result = self.handlers[jtype](ctx, params)
            self.db.execute(
                "UPDATE jobs SET status='done', progress=MAX(progress,total), finished_at=?, result=?, current=NULL WHERE id=?",
                (now(), json.dumps(result, default=str) if result is not None else None, jid))
        except JobCancelled:
            self.db.execute("UPDATE jobs SET status='cancelled', finished_at=?, current=NULL WHERE id=?", (now(), jid))
        except Exception as e:  # noqa: BLE001 - registamos tudo
            log.error("job %s (%s) falhou: %s", jid, jtype, scrub(traceback.format_exc()))
            self.db.execute("UPDATE jobs SET status='failed', finished_at=?, error=?, current=NULL WHERE id=?",
                            (now(), scrub(f"{type(e).__name__}: {e}")[:500], jid))
            add_event(self.db, "job", f"Tarefa #{jid} ({jtype}) falhou: {e}", "error")
        finally:
            with self._lock:
                self._events.pop(jid, None)

    def cancel(self, jid: int) -> bool:
        with self._lock:
            ev = self._events.get(jid)
        if not ev:
            return False
        ev.set()
        self.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
        return True

    def shutdown(self) -> None:
        for ev in list(self._events.values()):
            ev.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
