"""Histórico de downloads do MusicBox (persistência SQLite, stdlib, zero-config).

A tabela `downloads` guarda um registro por `yt_id` (UNIQUE), usado para:
- dedupe ao re-enfileirar (INSERT OR REPLACE atualiza a linha);
- evitar re-baixar músicas já concluídas (`is_downloaded`).
Identificadores em inglês; docstrings/comentários em português.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# Schema da tabela de histórico.
# Observação: o brief original não incluía a coluna `error`, mas a API `mark(..., error=...)`
# exige persistir o motivo da falha — coluna adicionada para o contrato funcionar.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    yt_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    artist TEXT,
    album TEXT,
    format TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    path TEXT,
    error TEXT
);
"""

_COLUMNS = "id, yt_id, title, artist, album, format, date, status, path, error"


class History:
    """Persistência do histórico de downloads em SQLite.

    Cada chamada pública abre e fecha sua própria conexão — evita compartilhar
    conexão entre threads (thread-safe o suficiente para 2 workers) e dispensa
    PRAGMA foreign_keys (tabela única). Erros de sqlite propagam normalmente.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Abre o banco e cria a tabela `downloads` se não existir."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """Abre uma conexão nova com acesso às colunas por nome (sqlite3.Row)."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        """Data/hora local em ISO 8601 (segundos), formato ordenável por texto."""
        return datetime.now().isoformat(timespec="seconds")

    def add(
        self,
        yt_id: str,
        title: str,
        artist: str | None,
        album: str | None,
        fmt: str,
        status: str = "pending",
        path: str | None = None,
    ) -> int:
        """Insere ou substitui (dedupe por `yt_id`) um registro. Retorna o id da linha."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR REPLACE INTO downloads "
                    "(yt_id, title, artist, album, format, date, status, path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (yt_id, title, artist, album, fmt, self._now(), status, path),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    def add_many(self, entries: list[dict]) -> None:
        """Grava vários registros em uma única transação (batch de álbum inteiro).

        Cada dict deve conter as chaves: `yt_id`, `title`, `artist`, `album`, `format`.
        Chaves opcionais: `status`, `path`. Lista vazia é no-op.
        """
        if not entries:
            return
        conn = self._connect()
        try:
            with conn:  # BEGIN/commit único; rollback automático em exceção
                conn.executemany(
                    "INSERT OR REPLACE INTO downloads "
                    "(yt_id, title, artist, album, format, date, status, path) "
                    "VALUES (:yt_id, :title, :artist, :album, :format, :date, :status, :path)",
                    [
                        {
                            "yt_id": e["yt_id"],
                            "title": e["title"],
                            "artist": e.get("artist"),
                            "album": e.get("album"),
                            "format": e["format"],
                            "date": self._now(),
                            "status": e.get("status", "pending"),
                            "path": e.get("path"),
                        }
                        for e in entries
                    ],
                )
        finally:
            conn.close()

    def get(self, yt_id: str) -> dict | None:
        """Retorna o registro do `yt_id` como dict, ou None se não existir."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM downloads WHERE yt_id = ?",
                (yt_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def list(self, limit: int = 100) -> list[dict]:
        """Retorna registros mais recentes primeiro (date DESC, empate id DESC), limitado."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM downloads ORDER BY date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark(
        self,
        yt_id: str,
        status: str,
        path: str | None = None,
        error: str | None = None,
    ) -> None:
        """Atualiza `status` da linha do `yt_id`; `path`/`error` só quando informados."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE downloads SET status = ?, "
                    "path = COALESCE(?, path), error = COALESCE(?, error) "
                    "WHERE yt_id = ?",
                    (status, path, error, yt_id),
                )
        finally:
            conn.close()

    def update_meta(
        self,
        yt_id: str,
        title: str,
        artist: str | None,
        album: str | None,
    ) -> None:
        """Atualiza APENAS title/artist/album da linha (não toca status/path/date).

        Usado pelo downloader após resolver os metadados do YouTube Music: o
        INSERT do enqueue guarda placeholders (ex.: title=yt_id no download
        avulso) e este UPDATE simples substitui sem REPLACE — preserva o
        status/path/date já gravados.
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE downloads SET title = ?, artist = ?, album = ? WHERE yt_id = ?",
                    (title, artist, album, yt_id),
                )
        finally:
            conn.close()

    def is_downloaded(self, yt_id: str) -> bool:
        """True se existe registro com `status == 'done'` para o `yt_id`."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE yt_id = ? AND status = 'done'",
                (yt_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def count(self) -> int:
        """Número total de registros no histórico."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()
            return int(row[0])
        finally:
            conn.close()
