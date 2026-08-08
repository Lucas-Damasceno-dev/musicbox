"""Histórico de downloads do MusicBox (persistência SQLite, stdlib, zero-config).

A tabela `downloads` guarda um registro por `yt_id` (UNIQUE), usado para:
- dedupe ao re-enfileirar (INSERT OR REPLACE atualiza a linha);
- evitar re-baixar músicas já concluídas (`is_downloaded`).
Identificadores em inglês; docstrings/comentários em português.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("musicbox.history")

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
    error TEXT,
    cover_url TEXT
);
"""

_COLUMNS = "id, yt_id, title, artist, album, format, date, status, path, error, cover_url"

# Fila de downloads persistida (Fase 5 — resiliência): sobrevive a restart do
# servidor. `task_id` é a chave (tasks de downloads em memória); `retry_count`
# guarda quantos retries de rede já foram agendados (restore agenda os restantes).
_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_queue (
    task_id TEXT PRIMARY KEY,
    yt_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    artist TEXT DEFAULT '',
    album TEXT DEFAULT '',
    format TEXT DEFAULT '',
    status TEXT NOT NULL,
    progress REAL DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

_QUEUE_COLUMNS = (
    "task_id, yt_id, title, artist, album, format, status, progress, retry_count, created_at"
)


class History:
    """Persistência do histórico de downloads em SQLite.

    Cada chamada pública abre e fecha sua própria conexão — evita compartilhar
    conexão entre threads (thread-safe o suficiente para 2 workers) e dispensa
    PRAGMA foreign_keys (tabela única). Erros de sqlite propagam normalmente.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Abre o banco e cria a tabela `downloads` se não existir.

        Migração leve: bancos criados antes da coluna `cover_url` recebem um
        `ALTER TABLE` — a coluna é nova (player com capa) e não pode quebrar o
        histórico existente do usuário.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_QUEUE_SCHEMA)  # idempotente (Fase 5 — fila persistida)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
            if "cover_url" not in cols:
                conn.execute("ALTER TABLE downloads ADD COLUMN cover_url TEXT")

    def _connect(self) -> sqlite3.Connection:
        """Abre uma conexão nova com acesso às colunas por nome (sqlite3.Row).

        WAL + busy_timeout: leituras não bloqueiam escritas e threads concorrentes
        (workers de download) aguardam em vez de falhar com `database is locked`.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _now() -> str:
        """Data/hora UTC em ISO 8601 (segundos), formato ordenável por texto.

        UTC (timezone-aware) em vez de `datetime.now()` naive: timestamps são
        ambíguos sem fuso e quebram a ordenação quando a máquina troca de fuso.
        O isoformat UTC continua lexicograficamente ordenável (mesma forma
        `YYYY-MM-DDTHH:MM:SS`), então o ORDER BY date DESC existente segue válido.
        """
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def add(
        self,
        yt_id: str,
        title: str,
        artist: str | None,
        album: str | None,
        fmt: str,
        status: str = "pending",
        path: str | None = None,
        cover_url: str | None = None,
    ) -> int:
        """Insere ou substitui (dedupe por `yt_id`) um registro. Retorna o id da linha."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR REPLACE INTO downloads "
                    "(yt_id, title, artist, album, format, date, status, path, cover_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (yt_id, title, artist, album, fmt, self._now(), status, path, cover_url),
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
                    "(yt_id, title, artist, album, format, date, status, path, cover_url) "
                    "VALUES (:yt_id, :title, :artist, :album, :format, :date, :status, :path, :cover_url)",
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
                            "cover_url": e.get("cover_url"),
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

    def get_many(self, yt_ids: list[str]) -> dict[str, dict]:
        """Busca em lote por `yt_id` (uma conexão, `WHERE yt_id IN (...)`).

        Retorna um dict `{yt_id: registro}`; ids inexistentes ficam de fora.
        Lista vazia é no-op (sem SQL gerado). Evita o N+1 de abrir uma conexão
        por faixa nos exports/playlists.
        """
        if not yt_ids:
            return {}
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in yt_ids)
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM downloads WHERE yt_id IN ({placeholders})",
                tuple(yt_ids),
            ).fetchall()
            return {row["yt_id"]: dict(row) for row in rows}
        finally:
            conn.close()

    def list(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Retorna registros mais recentes primeiro (date DESC, empate id DESC), limitado.

        `offset` permite paginar o histórico completo (ex.: operações internas
        que varrem tudo em lotes de 1000 sem perder o que passou do limite).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM downloads ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
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
        cover_url: str | None = None,
    ) -> None:
        """Atualiza title/artist/album (e `cover_url`, quando informado) da linha.

        Não toca status/path/date. Usado pelo downloader após resolver os
        metadados do YouTube Music: o INSERT do enqueue guarda placeholders
        (ex.: title=yt_id no download avulso) e este UPDATE simples substitui
        sem REPLACE — preserva o status/path/date já gravados.
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE downloads SET title = ?, artist = ?, album = ?, "
                    "cover_url = COALESCE(?, cover_url) WHERE yt_id = ?",
                    (title, artist, album, cover_url, yt_id),
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

    def delete(self, yt_id: str) -> bool:
        """Remove o registro do `yt_id`. Retorna True se uma linha foi apagada."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM downloads WHERE yt_id = ?", (yt_id,))
                return cur.rowcount > 0
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

    def update_tags(self, yt_id: str, title: str, artist: str, album: str) -> bool:
        """Atualiza metadados no SQLite e nas tags de mídia (ID3/Ogg) via Mutagen se o arquivo existir."""
        record = self.get(yt_id)
        if not record:
            return False
        self.update_meta(yt_id, title, artist, album)
        file_path = record.get("path")
        if file_path and Path(file_path).exists():
            try:
                import mutagen
                from mutagen.easyid3 import EasyID3
                from mutagen.mp3 import MP3
                from mutagen.oggopus import OggOpus

                p = Path(file_path)
                if p.suffix.lower() == ".mp3":
                    try:
                        audio = EasyID3(str(p))
                    except mutagen.id3.ID3NoHeaderError:
                        mp3 = MP3(str(p))
                        mp3.add_tags()
                        mp3.save()
                        audio = EasyID3(str(p))
                    audio["title"] = title
                    audio["artist"] = artist
                    audio["album"] = album
                    audio.save()
                elif p.suffix.lower() == ".opus":
                    audio = OggOpus(str(p))
                    audio["title"] = [title]
                    audio["artist"] = [artist]
                    audio["album"] = [album]
                    audio.save()
            except Exception as exc:
                # Atualização do arquivo físico é melhor-esforço; a do DB é garantida.
                # Mas falha silenciosa esconde problemas (ex.: arquivo read-only) —
                # registra em warning para diagnóstico sem derrubar a rota.
                logger.warning(
                    "Falha ao atualizar tags de %s (yt_id=%s): %s", file_path, yt_id, exc
                )
        return True

    # ------------------------------------------------- fila persistida (Fase 5)

    def queue_upsert(self, row: dict) -> None:
        """INSERT OR REPLACE de uma linha da `download_queue` (melhor esforço).

        `created_at` é preenchido com o agora-UTC se ausente. Falha de sqlite só
        loga em warning — persistir a fila nunca pode quebrar o download.
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO download_queue ({_QUEUE_COLUMNS}) "
                    "VALUES (:task_id, :yt_id, :title, :artist, :album, :format, "
                    ":status, :progress, :retry_count, :created_at)",
                    {
                        "task_id": row["task_id"],
                        "yt_id": row.get("yt_id") or "",
                        "title": row.get("title") or "",
                        "artist": row.get("artist") or "",
                        "album": row.get("album") or "",
                        "format": row.get("format") or "",
                        "status": row.get("status") or "pending",
                        "progress": row.get("progress") or 0.0,
                        "retry_count": row.get("retry_count") or 0,
                        "created_at": row.get("created_at") or self._now(),
                    },
                )
        except sqlite3.Error as exc:
            logger.warning("Falha ao persistir fila (%s): %s", row.get("task_id"), exc)
        finally:
            conn.close()

    def queue_update_status(self, task_id: str, status: str, progress: float | None = None) -> None:
        """Atualiza `status` (e `progress`, quando informado) de uma linha da fila."""
        conn = self._connect()
        try:
            with conn:
                if progress is None:
                    conn.execute(
                        "UPDATE download_queue SET status = ? WHERE task_id = ?",
                        (status, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE download_queue SET status = ?, progress = ? WHERE task_id = ?",
                        (status, progress, task_id),
                    )
        except sqlite3.Error as exc:
            logger.warning("Falha ao atualizar fila (%s → %s): %s", task_id, status, exc)
        finally:
            conn.close()

    def queue_load(self) -> list[dict]:
        """Retorna a fila persistida em ordem de criação; [] se erro ou vazia."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_QUEUE_COLUMNS} FROM download_queue ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def queue_delete(self, task_id: str) -> None:
        """Remove uma linha da `download_queue` (task concluída/cancelada)."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM download_queue WHERE task_id = ?", (task_id,))
        except sqlite3.Error as exc:
            logger.warning("Falha ao remover da fila (%s): %s", task_id, exc)
        finally:
            conn.close()
