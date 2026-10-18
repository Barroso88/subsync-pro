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


class JobError(Exception):
    """Falha esperada de uma tarefa: a mensagem é mostrada tal como está (sem o nome da exceção)."""


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
        self.lanes: dict[str, ThreadPoolExecutor] = {}       # filas de 1 trabalhador: tarefas do mesmo tipo correm UMA de cada vez (ex.: traduções, por causa dos limites)
        self.lane_of: dict[str, str] = {}

    def register(self, jtype: str, fn: Callable[[JobContext, dict], Any], lane: str | None = None) -> None:
        self.handlers[jtype] = fn
        if lane:
            self.lane_of[jtype] = lane
            if lane not in self.lanes:
                self.lanes[lane] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"lane-{lane}")

    def queue_position(self, jid: int) -> int | None:
        """Posição na fila (1 = a seguir ao que está a correr; 0 = a correr/não aplicável) para tipos com fila própria."""
        r = self.db.one("SELECT type,status FROM jobs WHERE id=?", (jid,))
        if not r or r["type"] not in self.lane_of or r["status"] != "queued":
            return None
        lane = self.lane_of[r["type"]]
        types = [t for t, l in self.lane_of.items() if l == lane]
        q = ",".join("?" * len(types))
        return self.db.scalar(f"SELECT COUNT(*) FROM jobs WHERE type IN ({q}) AND status IN ('queued','running') AND id<?", (*types, jid)) or 0

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
        (self.lanes[self.lane_of[jtype]] if jtype in self.lane_of else self.pool).submit(self._run, jid, jtype, params)
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
        except JobError as e:
            self.db.execute("UPDATE jobs SET status='failed', finished_at=?, error=?, current=NULL WHERE id=?", (now(), scrub(str(e))[:500], jid))
            add_event(self.db, "job", f"Tarefa #{jid} ({jtype}) falhou: {e}", "error")
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
        self.db.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'", (now(), jid))   # ainda na fila: cancela já
        return True

    def shutdown(self) -> None:
        for ev in list(self._events.values()):
            ev.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for p in self.lanes.values():
            p.shutdown(wait=False, cancel_futures=True)
