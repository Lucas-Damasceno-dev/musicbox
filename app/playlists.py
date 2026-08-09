"""Playlists do usuário no MusicBox (persistência SQLite, stdlib, zero-config).

Tabelas:
- `playlists` (id, name, created_at)
- `playlist_tracks` (playlist_id, yt_id, position) — FK com ON DELETE CASCADE

Cada chamada pública abre e fecha a própria conexão (mesmo padrão do History).
A junção com metadados do histórico é feita pelo main.py (bases separadas).

Identificadores em inglês; docstrings/comentários em português.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    yt_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, yt_id)
);
"""


class PlaylistStore:
    """Persistência de playlists em SQLite (CRUD + faixas, dedupe por yt_id)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)  # duas tabelas → executescript

    def _connect(self) -> sqlite3.Connection:
        """Abre conexão nova com acesso por nome de coluna (sqlite3.Row)."""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")  # CASCADE do DELETE de playlist
        return conn

    @staticmethod
    def _now() -> str:
        """Timestamp UTC em ISO 8601 (segundos), ordenável lexicograficamente."""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def create(self, name: str) -> dict:
        """Cria uma playlist e devolve o registro (com track_count=0)."""
        now = self._now()  # um único timestamp para o INSERT e o retorno
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO playlists (name, created_at) VALUES (?, ?)",
                    (name, now),
                )
                pid = int(cur.lastrowid)
            return {"id": pid, "name": name, "track_count": 0, "created_at": now}
        finally:
            conn.close()

    def list_all(self) -> list[dict]:
        """Todas as playlists, mais recentes primeiro, com contagem de faixas.

        Nome não é `list` de propósito: um método `list` sombrearia o builtin
        dentro da classe (e quebraria anotações como `-> list[dict]`).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT p.id, p.name, p.created_at, "
                "(SELECT COUNT(*) FROM playlist_tracks t "
                "WHERE t.playlist_id = p.id) AS track_count "
                "FROM playlists p ORDER BY p.id DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get(self, playlist_id: int) -> dict | None:
        """Retorna a playlist pelo id (com track_count), ou None."""
        # Nota: `get` não colide com builtin (dict.get) porque é método de instância.
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT p.id, p.name, p.created_at, "
                "(SELECT COUNT(*) FROM playlist_tracks t "
                "WHERE t.playlist_id = p.id) AS track_count "
                "FROM playlists p WHERE p.id = ?",
                (playlist_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def delete(self, playlist_id: int) -> bool:
        """Apaga a playlist (faixas somem via CASCADE). True se apagou algo."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
                return cur.rowcount > 0
        finally:
            conn.close()

    def add_track(self, playlist_id: int, yt_id: str) -> bool:
        """Adiciona uma faixa (dedupe por yt_id). False se já estava na playlist."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO playlist_tracks (playlist_id, yt_id, position) "
                    "VALUES (?, ?, COALESCE((SELECT MAX(position) + 1 FROM playlist_tracks "
                    "WHERE playlist_id = ?), 1))",
                    (playlist_id, yt_id, playlist_id),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def remove_track(self, playlist_id: int, yt_id: str) -> bool:
        """Remove uma faixa da playlist. True se removeu algo."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = ? AND yt_id = ?",
                    (playlist_id, yt_id),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def track_ids(self, playlist_id: int) -> list[str]:
        """Ids das faixas na ordem de inserção (position ASC)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT yt_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC",
                (playlist_id,),
            ).fetchall()
            return [row["yt_id"] for row in rows]
        finally:
            conn.close()
