"""SQLite com WAL, uma ligação por thread e migrações SQL versionadas.

Migrações: ficheiros `migrations/NNN_nome.sql` aplicados por ordem, cada um
numa transação; a versão fica em `PRAGMA user_version`. Sem ORM para manter a
imagem pequena; os SQL usam apenas funcionalidade padrão (portável para PostgreSQL
com pequenas adaptações).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._conns: list[sqlite3.Connection] = []

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
            self._conns.append(c)
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transação de escrita (serializada entre threads)."""
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.tx() as c:
            return c.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.one(sql, params)
        return row[0] if row and row[0] is not None else default

    def migrate(self) -> int:
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        c = self.conn()
        current = c.execute("PRAGMA user_version").fetchone()[0]
        for f in files:
            version = int(f.name.split("_")[0])
            if version <= current:
                continue
            script = f.read_text("utf-8")
            with self._write_lock:
                c.executescript("BEGIN IMMEDIATE;\n" + script + f"\nPRAGMA user_version={version};\nCOMMIT;")
            current = version
        return current

    def close(self) -> None:
        for c in self._conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._conns.clear()
        self._local = threading.local()


def rows(rs: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rs]


def jloads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
