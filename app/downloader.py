"""Fila de downloads do MusicBox (FIFO + thread pool, progresso, sanitização).

O `Downloader` enfileira tarefas (música avulsa ou álbum inteiro), executa o
yt-dlp em `settings.workers` threads daemon, publica progresso via listeners
(registrados pelo main.py para o WebSocket) e persiste o resultado no histórico
SQLite. Um executor pode ser injetado nos testes para validar a lógica sem rede.

Identificadores em inglês; docstrings/comentários em português.
"""

import logging
import queue
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .history import History
from .lyrics import fetch_lrc
from .models import DownloadTask, Track
from .ytdlp_client import (
    NetworkError,
    SearchError,
    YouTubeMusicClient,
    _strip_ansi,
    is_network_error,
)

# Espaço livre mínimo exigido antes de iniciar um download (spec: disco cheio).
_FREE_MIN_BYTES = 50 * 1024 * 1024  # 50 MB

# Retry automático de falhas de rede (Fase 5): até 3 tentativas com backoff
# 5s → 30s → 2min. A task fica `failed` com `_retry_ts` no futuro; o scheduler
# re-enfileira quando o tempo chega (o `.part` do yt-dlp retoma do ponto).
_RETRY_DELAYS = (5.0, 30.0, 120.0)
_RETRY_SCHEDULER_INTERVAL = 1.0  # segundos entre varreduras do scheduler

# Formatos aceitos no enqueue/enqueue_album (spec: mp3 | opus).
# Exportado: main.py usa o mesmo conjunto no POST /api/downloads.
VALID_FORMATS = {"mp3", "opus"}

# Sufixos de arquivos de download interrompido (yt-dlp) removidos no startup.
_PARTIAL_SUFFIXES = {".part", ".ytdl"}

# Teto de tasks em memória: acima dele, as tasks terminais mais antigas são
# podadas (as ativas — pending/running — nunca são removidas).
_TASKS_CAP = 300

# Status terminais: a task não executa mais (poda, dedupe e stop usam isso).
# `paused` entra aqui: uma task pausada não conta como "ativa" no dedupe
# (_find_active) nem é preservada pelo stop; a retomada re-enfileira a MESMA task.
_TERMINAL_STATUSES = ("done", "failed", "skipped", "cancelled", "paused")

logger = logging.getLogger("musicbox")


class _CancelledError(Exception):
    """Levantada no progress hook quando o usuário cancela um download em execução."""


def sanitize_filename(nome: str) -> str:
    """Sanitiza um nome de arquivo para uso seguro em qualquer sistema de arquivos.

    Remove caracteres inválidos no Windows (``<>:"/\\|?*``) e separadores de
    caminho, remove caracteres de controle (incl. ``\\x00``), remove espaços e
    pontos nas pontas, trunca em ~180 caracteres e devolve ``"sem_nome"`` se vazio.
    """
    if not nome:
        return "sem_nome"
    # caracteres de controle (incl. \x00 e \x7f) são removidos
    nome = "".join(ch for ch in nome if ch >= " " and ch != "\x7f")
    # caracteres inválidos no Windows + separadores de caminho
    nome = "".join(ch for ch in nome if ch not in '<>:"/\\|?*')
    nome = nome.strip(" .")  # espaços/pontos finais (e iniciais) removidos
    return nome[:180] or "sem_nome"


class Downloader:
    """Fila FIFO de downloads com thread pool e publicação de progresso.

    `main.py` (T5) chama `start()` no startup, `enqueue`/`enqueue_album` via API,
    registra um listener com `add_listener` para o WebSocket, `cleanup_partials()`
    no startup e `stop()` no shutdown. Tasks vivem em memória (somem no restart);
    o histórico SQLite é a persistência.
    """

    def __init__(
        self,
        settings: Settings,
        history: History,
        client: YouTubeMusicClient,
        executor: Callable[..., Path] | None = None,
    ) -> None:
        self._settings = settings
        self._history = history
        self._client = client
        self._executor = executor or self._default_executor
        self._queue: queue.Queue[DownloadTask] = queue.Queue()
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        # task_ids atualmente NA fila (ou já desenfileirados e em processamento).
        # Usado pelo resume para não re-enfileirar uma task pausada que ainda está
        # pendente na fila (evita 2ª cópia → download duplicado com workers>1).
        self._queued: set[str] = set()
        # Serializa check+add+put do enqueue/enqueue_album: dois POSTs simultâneos
        # do mesmo yt_id não podem criar 2 tasks (race de dedupe).
        self._enqueue_lock = threading.Lock()
        self._listeners: list[Callable[[str, str, float, str], None]] = []
        self._stop_flag = threading.Event()
        self._threads: list[threading.Thread] = []
        self._local = threading.local()  # task atual por thread (progress hook)
        # Scheduler de retry (Fase 5): daemon thread que re-enfileira tasks
        # `failed` por rede quando `_retry_ts` vence.
        self._retry_scheduler_stop = threading.Event()
        self._retry_scheduler_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        """Sobe `settings.workers` threads daemon que consomem a fila.

        Antes dos workers: restaura a fila persistida (`download_queue`) e sobe
        o scheduler de retry. Chamar `start()` de novo após `stop()` recria as
        threads.
        """
        if any(t.is_alive() for t in self._threads):
            return  # já rodando
        self._stop_flag.clear()
        self._retry_scheduler_stop.clear()
        # Restaura tasks persistidas (pending/downloading/paused/failed-com-retry)
        # ANTES dos workers — eles consomem a fila restaurada.
        self._restore_queue()
        self._threads = [
            threading.Thread(
                target=self._worker_loop,
                name=f"downloader-{i}",
                daemon=True,
            )
            for i in range(max(1, self._settings.workers))
        ]
        for thread in self._threads:
            thread.start()
        self._retry_scheduler_thread = threading.Thread(
            target=self._retry_scheduler_loop,
            name="downloader-retry-scheduler",
            daemon=True,
        )
        self._retry_scheduler_thread.start()

    def stop(self) -> None:
        """Sinaliza parada, aguarda a fila drenar (com timeout) e encerra as threads.

        Encerra o scheduler de retry primeiro (Event + join curto); depois marca
        as tasks ativas com `cancel_requested` para os workers abortarem e drena
        a fila com timeout (rede/ffmpeg presos não travam o shutdown).
        """
        self._retry_scheduler_stop.set()
        if self._retry_scheduler_thread is not None:
            self._retry_scheduler_thread.join(timeout=2)
            self._retry_scheduler_thread = None
        self._stop_flag.set()
        with self._lock:
            for task in self._tasks.values():
                if task.status not in _TERMINAL_STATUSES:
                    task.cancel_requested = True
        # `queue.Queue.join()` não aceita timeout (Python 3.12); a condição
        # interna all_tasks_done.wait(timeout) também não serve (perde o notify
        # se a fila já drenou). Polling leve do contador com deadline: retorna
        # quando a fila drena OU após 10s — shutdown não trava se um worker
        # estiver preso em rede/ffmpeg.
        deadline = time.monotonic() + 10
        while self._queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        for thread in self._threads:
            thread.join(timeout=5)

    def enqueue(
        self,
        yt_id: str,
        fmt: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> DownloadTask:
        """Enfileira uma música avulsa e registra no histórico (dedupe por yt_id).

        Dedupe em memória sob `_enqueue_lock`: dois POSTs simultâneos do mesmo
        `yt_id` com a task ainda ativa (pending/running) devolvem a MESMA task —
        uma única linha no histórico e uma única execução do yt-dlp.
        """
        self._validate_format(fmt)
        title = title or yt_id
        with self._enqueue_lock:
            existing = self._find_active(yt_id)
            if existing is not None:
                return existing
            # Já baixado (status done): mantém a linha do histórico intacta para o
            # worker detectar o skip; senão insere/replace com status pending.
            if not self._history.is_downloaded(yt_id):
                self._history.add(yt_id, title, artist, album, fmt)
            task = DownloadTask(
                task_id=uuid.uuid4().hex[:8],
                yt_id=yt_id,
                title=title,
                format=fmt,
                artist=artist,
                album=album,
            )
            self._register(task)
            self._queue.put(task)
            self._queued.add(task.task_id)
            self._queue_persist(task)
        self._notify(task.task_id, "pending", 0.0, "queued")
        return task

    def enqueue_album(
        self, tracks: list[Track], fmt: str, artist: str, album: str
    ) -> list[DownloadTask]:
        """Enfileira um álbum inteiro: uma única transação SQLite (batch) no histórico.

        Sob `_enqueue_lock`: faixas com task ativa (pending/running) são ignoradas
        (race de dedupe — POSTs simultâneos não criam tasks duplicadas).
        """
        self._validate_format(fmt)
        with self._enqueue_lock:
            tracks = [t for t in tracks if self._find_active(t.yt_id) is None]
            if not tracks:
                return []
            # Uma transação: monta todas as rows e grava em batch.
            self._history.add_many(
                [
                    {
                        "yt_id": t.yt_id,
                        "title": t.title,
                        "artist": artist,
                        "album": album,
                        "format": fmt,
                        "cover_url": t.cover_url,
                    }
                    for t in tracks
                ]
            )
            tasks: list[DownloadTask] = []
            for track in tracks:
                task = DownloadTask(
                    task_id=uuid.uuid4().hex[:8],
                    yt_id=track.yt_id,
                    title=track.title,
                    format=fmt,
                    artist=artist,
                    album=album,
                    number=track.number,
                    cover_url=track.cover_url,
                )
                self._register(task)
                self._queue.put(task)
                self._queued.add(task.task_id)
                self._queue_persist(task)
                tasks.append(task)
        for task in tasks:
            self._notify(task.task_id, "pending", 0.0, "queued")
        return tasks

    def get(self, task_id: str) -> DownloadTask | None:
        """Retorna a task pelo id (None se não existir em memória)."""
        with self._lock:
            return self._tasks.get(task_id)

    def snapshot(self) -> list[DownloadTask]:
        """Retorna as tasks em ordem de criação (dict preserva a ordem de inserção)."""
        with self._lock:
            return list(self._tasks.values())

    def add_listener(self, fn: Callable[[str, str, float, str], None]) -> None:
        """Registra um callback ``fn(task_id, status, progress, stage)``."""
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[str, str, float, str], None]) -> None:
        """Remove um listener registrado (o WebSocket usa ao desconectar).

        Idempotente: remover um listener já removido é no-op. A iteração dos
        listeners em `_notify` usa cópia da lista, então remoção concorrente
        com notificação é segura.
        """
        try:
            self._listeners.remove(fn)
        except ValueError:
            logger.debug("remove_listener: listener não registrado: %r", fn)

    def cancel(self, task_id: str) -> bool:
        """Cancela uma task pendente ou em execução; False se inexistente/terminal.

        Pendente: o worker descarta ao pegar da fila. Em execução: o flag
        `cancel_requested` faz o progress hook do yt-dlp abortar o download.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in ("done", "failed", "skipped", "cancelled"):
                return False
            task.status = "cancelled"
            task.stage = "cancelled"
            task.cancel_requested = True
            self._notify(task.task_id, "cancelled", task.progress, "cancelled")
            self._history.mark(task.yt_id, "cancelled")
            self._history.queue_delete(task.task_id)
            return True

    def pause(self, task_ids: list[str] | None = None) -> list[str]:
        """Pausa tasks pending/running (todas ativas se task_ids None). Preserva o
        .part (resume nativo do yt-dlp retoma do ponto). `cancel_requested` aborta
        o download em andamento via progress hook; o estado fica `paused`, distinto
        de `cancelled`. Tarefas terminais (done/failed/skipped/cancelled/paused)
        são ignoradas — não entram na lista retornada."""
        paused: list[str] = []
        with self._lock:
            targets = [
                t
                for t in self._tasks.values()
                if t.status in ("pending", "running")
                and (task_ids is None or t.task_id in task_ids)
            ]
            for task in targets:
                task.status = "paused"
                task.stage = "paused"
                task.cancel_requested = True  # aborta download em andamento (progress hook)
                paused.append(task.task_id)
                self._history.mark(task.yt_id, "paused")
                self._history.queue_update_status(task.task_id, "paused")
        for task_id in paused:
            self._notify(task_id, "paused", 0.0, "paused")
        return paused

    def resume(self, task_ids: list[str] | None = None) -> list[str]:
        """Retoma tasks paused — ou `failed` com retry de rede pendente — re-enfileirando
        a MESMA task (sem duplicata; o yt-dlp continua do `.part` preservado).

        O scheduler de retry chama este método quando `_retry_ts` vence (a task
        está `failed` com `_retry_ts` no passado): volta a `pending`/`queued`,
        limpa `cancel_requested` e `_retry_ts` (retry consumido) e re-enfileira.
        Só re-enfileira se a task não está mais na fila (`_queued`): uma task
        ainda pendente na fila não ganha uma 2ª cópia (evita download duplicado/
        concorrente com `workers > 1`)."""
        resumed: list[str] = []
        with self._enqueue_lock, self._lock:
            targets = [
                t
                for t in self._tasks.values()
                if (t.status == "paused" or (t.status == "failed" and t._retry_ts is not None))
                and (task_ids is None or t.task_id in task_ids)
            ]
            for task in targets:
                task.status = "pending"
                task.stage = "queued"
                task.cancel_requested = False
                task.progress = 0.0
                task._retry_ts = None  # retry consumido ao re-enfileirar
                resumed.append(task.task_id)
                if task.task_id not in self._queued:
                    self._queue.put(task)  # MESMA task: snapshot sem duplicata
                    self._queued.add(task.task_id)
                self._history.queue_update_status(task.task_id, "pending")
        for task_id in resumed:
            self._notify(task_id, "pending", 0.0, "queued")
        return resumed

    def cleanup_partials(self) -> None:
        """Remove arquivos `.part`/`.ytdl` (downloads interrompidos) sob `musicbox_dir`.

        Chamado pelo main.py no startup. Tasks em memória somem no restart e
        voltam `pending`; o histórico só é alterado se o processo morreu após
        marcar `running` — aqui apenas removemos os arquivos (decisão do T5).
        """
        root = self._settings.musicbox_dir
        if not root.exists():
            return
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in _PARTIAL_SUFFIXES:
                try:
                    path.unlink()
                except OSError:
                    pass  # arquivo em uso/lock — deixa para a próxima execução

    # ------------------------------------------------------------- internals

    @staticmethod
    def _validate_format(fmt: str) -> None:
        if fmt not in VALID_FORMATS:
            raise ValueError(f"Formato inválido: {fmt!r} (use 'mp3' ou 'opus')")

    def _register(self, task: DownloadTask) -> None:
        """Registra a task em memória; poda tasks terminais se o teto for excedido.

        O dict preserva a ordem de inserção: iterar == ordem de criação, então a
        poda remove sempre as tasks terminais mais antigas. Tasks ativas
        (pending/running) nunca são removidas.
        """
        with self._lock:
            self._tasks[task.task_id] = task
            if len(self._tasks) > _TASKS_CAP:
                for task_id in list(self._tasks):
                    if len(self._tasks) <= _TASKS_CAP:
                        break
                    if self._tasks[task_id].status in _TERMINAL_STATUSES:
                        del self._tasks[task_id]

    def _find_active(self, yt_id: str) -> DownloadTask | None:
        """Devolve a task ativa (não terminal) do `yt_id`, ou None."""
        with self._lock:
            for task in self._tasks.values():
                if task.yt_id == yt_id and task.status not in _TERMINAL_STATUSES:
                    return task
        return None

    def _notify(self, task_id: str, status: str, progress: float, stage: str) -> None:
        """Notifica todos os listeners; um listener quebrado não derruba o worker."""
        for fn in list(self._listeners):
            try:
                fn(task_id, status, progress, stage)
            except Exception:
                pass

    def _worker_loop(self) -> None:
        while True:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                # Só sai com a fila vazia: com o flag de parada setado e items
                # ainda na fila, o worker drena o restante antes de encerrar.
                # Sem isso, `stop()` → `queue.join()` penduraria (tasks órfãs).
                if self._stop_flag.is_set():
                    return
                continue
            # Task cancelada/pausada antes de o worker pegá-la: descarta sem
            # processar. A decisão é atômica com a remoção de `_queued` (mesmo
            # lock do resume): evita re-enfileirar duplicado numa corrida.
            with self._enqueue_lock:
                self._queued.discard(task.task_id)
                if task.status in ("cancelled", "paused"):
                    self._queue.task_done()
                    continue
            try:
                self._process(task)
            except Exception as exc:  # segurança: a thread não deve morrer
                if task.status == "cancelled":
                    # Cancelamento em andamento: exceção genérica do yt-dlp (ou
                    # _CancelledError engolida pelo executor) NÃO vira "failed".
                    self._cancel(task)
                elif task.status == "paused":
                    pass  # mantém paused (não _fail, não _cancel)
                elif getattr(task, "cancel_requested", False):
                    self._cancel(task)
                else:
                    self._schedule_retry_or_fail(task, exc, "Erro inesperado")
            finally:
                self._queue.task_done()

    def _process(self, task: DownloadTask) -> None:
        """Skip rápido: já baixado (histórico done) e arquivo presente no disco."""
        if self._history.is_downloaded(task.yt_id):
            record = self._history.get(task.yt_id)
            existing = record.get("path") if record else None
            if existing and Path(existing).exists():
                task.status = "skipped"
                task.stage = "done"
                task.path = existing
                task.progress = 100.0
                self._notify(task.task_id, "skipped", 100.0, "done")
                self._history.mark(task.yt_id, "skipped", path=existing)
                self._history.queue_delete(task.task_id)
                return
        self._run(task)

    def _run(self, task: DownloadTask) -> None:
        """Executa o download de uma task (núcleo: disco → metadados → yt-dlp → move)."""
        yt_id = task.yt_id
        self._local.task = task  # para o progress hook saber qual task é esta
        temp_dir: Path | None = None
        try:
            task.status = "running"
            task.stage = "extracting"
            task.progress = 0.0
            self._notify(task.task_id, "running", 0.0, "extracting")
            self._history.mark(yt_id, "running")
            self._history.queue_update_status(task.task_id, "downloading")

            # Pré-checagem: espaço em disco e permissão de escrita.
            dest_root = self._settings.musicbox_dir
            try:
                dest_root.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(dest_root).free
            except OSError as exc:
                self._fail(task, f"Sem permissão para criar diretório: {exc}")
                return
            if free < _FREE_MIN_BYTES:
                self._fail(task, "Disco sem espaço suficiente")
                return

            # Metadados: resolve título/artista/álbum da task. Valores explícitos
            # informados no enqueue (UI com data-title ou enqueue_album) vencem; o
            # metadata do YouTube Music preenche lacunas — o enqueue avulso grava
            # title=yt_id como placeholder e aqui vira o título real. Persistido via
            # update_meta (UPDATE simples — não toca status/path/date do histórico).
            try:
                metadata = self._client.track_metadata(yt_id)
            except (SearchError, NetworkError) as exc:
                self._schedule_retry_or_fail(task, exc, "Falha ao obter metadados")
                return

            if not task.artist:
                task.artist = (metadata.get("artists") or [None])[0] or "Desconhecido"
            if not task.album:
                task.album = metadata.get("album") or "Singles"
            if not task.title or task.title == yt_id:
                task.title = metadata.get("track") or metadata.get("title") or task.title or yt_id
            # Capa: o metadata do YouTube Music é a fonte padrão; o enqueue de
            # álbum já traz a capa da faixa (não sobrescreve).
            task.cover_url = task.cover_url or metadata.get("thumbnail")
            self._history.update_meta(
                yt_id, task.title, task.artist, task.album, cover_url=task.cover_url
            )

            # Cancelado/pausado durante a fase de metadados: aborta antes do
            # executor (paused mantém o estado — o resume re-enfileira).
            if getattr(task, "cancel_requested", False):
                if task.status == "paused":
                    return  # mantém paused
                self._cancel(task)
                return

            artist_dir = sanitize_filename(task.artist)
            album_dir = sanitize_filename(task.album)
            title_stem = sanitize_filename(task.title)
            # Número da faixa (flow de álbum) vira prefixo zero-padded: "01 - título".
            stem = f"{task.number:02d} - {title_stem}" if task.number is not None else title_stem
            dest_dir = dest_root / artist_dir / album_dir

            # Execução do yt-dlp (executor injetável). O cancelamento em execução
            # derruba o executor via progress hook (_CancelledError).
            temp_dir = dest_root / ".tmp" / task.task_id
            temp_dir.mkdir(parents=True, exist_ok=True)
            try:
                temp_file = Path(
                    self._executor(yt_id, task.format, temp_dir, dest_dir, stem, metadata)
                )
            except _CancelledError:
                if task.status == "paused":
                    return  # pausado: mantém paused — o .part fica no temp_dir
                self._cancel(task)
                return
            except Exception as exc:
                if task.status == "paused":
                    return  # mantém paused (não vira failed/cancelled)
                if getattr(task, "cancel_requested", False):
                    # Cancelamento durante o download + exceção genérica do
                    # yt-dlp: trata como cancelamento (não sobrescreve com failed).
                    self._cancel(task)
                else:
                    self._schedule_retry_or_fail(task, exc, "Falha no download")
                return
            if task.status == "paused":
                return  # pausado entre o fim do executor e o move: .part preservado
            if getattr(task, "cancel_requested", False):
                self._cancel(task)  # cancelado entre o fim do executor e o move
                return
            if not temp_file.exists():
                self._fail(task, "Executor não produziu arquivo de áudio")
                return
            ext = temp_file.suffix or (".mp3" if task.format == "mp3" else ".opus")

            # Arquivo final já existe?
            dest_final = dest_dir / f"{stem}{ext}"
            if dest_final.exists():
                if self._history.is_downloaded(yt_id):
                    # Já baixado → skip (mantém o arquivo existente).
                    task.status = "skipped"
                    task.stage = "done"
                    task.path = str(dest_final)
                    task.progress = 100.0
                    self._notify(task.task_id, "skipped", 100.0, "done")
                    self._history.mark(yt_id, "skipped", path=str(dest_final))
                    self._history.queue_delete(task.task_id)
                    return
                # Existe mas não está done no histórico → sobrescreve com sufixo (2), (3)...
                dest_final = self._suffix_path(dest_final)

            # Move para o destino final (shutil.move resolve filesystems diferentes).
            # O stage "moving" fica visível na task (get/snapshot), mas não gera
            # notify próprio — a sequência de eventos do listener é pending→running→done.
            dest_dir.mkdir(parents=True, exist_ok=True)
            task.stage = "moving"
            shutil.move(str(temp_file), str(dest_final))

            # Letra (LRC) ao lado do áudio: busca na LRCLIB e grava como
            # `dest_final.with_suffix(".lrc")`. Falha/ausência NUNCA bloqueia o
            # download (o áudio já está no destino) — só loga em debug.
            try:
                lrc = fetch_lrc(task.artist or "", task.title or "", task.album)
                if lrc:
                    dest_final.with_suffix(".lrc").write_text(lrc, encoding="utf-8")
            except Exception as exc:
                logger.debug("Falha ao buscar/gravar letra de %s: %s", yt_id, exc)

            # Conclui.
            task.path = str(dest_final)
            task.status = "done"
            task.progress = 100.0
            task.stage = "done"
            self._notify(task.task_id, "done", 100.0, "done")
            self._history.mark(yt_id, "done", path=str(dest_final))
            self._history.queue_delete(task.task_id)
        finally:
            # Paused e failed-com-retry-preservam o temp_dir: o `.part` do yt-dlp
            # fica lá para o resume nativo (pause manual OU retry automático de
            # rede) retomar do ponto exato — mesmo outtmpl/URL/formato.
            if temp_dir is not None and task.status != "paused" and task._retry_ts is None:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self._local.task = None

    def _cancel(self, task: DownloadTask) -> None:
        """Marca a task como `cancelled` (estado terminal, sem arquivo final)."""
        task.status = "cancelled"
        task.stage = "cancelled"
        self._notify(task.task_id, "cancelled", task.progress, "cancelled")
        self._history.mark(task.yt_id, "cancelled")
        self._history.queue_delete(task.task_id)
        logger.info("Download cancelado: %s", task.yt_id)

    def _fail(self, task: DownloadTask, motivo: str) -> None:
        """Marca a task como `failed` definitivo com o motivo; não relança.

        O motivo passa por `_strip_ansi` (o yt-dlp imprime erros coloridos que
        apareceriam crus na UI). Task `paused` não é sobrescrita (a pausa pode
        ocorrer durante uma fase que falha — ex.: metadados — e o estado de
        pausa deve prevalecer). `failed` definitivo não tem retry pendente.
        """
        if task.status == "paused":
            return  # mantém paused
        motivo = _strip_ansi(motivo)
        task.error = motivo
        task.status = "failed"
        task._retry_ts = None  # definitivo: sem novo agendamento de retry
        # stage reflete onde falhou (stage corrente da task).
        self._notify(task.task_id, "failed", task.progress, task.stage)
        self._history.mark(task.yt_id, "failed", error=motivo)
        self._history.queue_update_status(task.task_id, "failed", task.progress)
        logger.warning("Download falhou (%s): %s", task.yt_id, motivo)

    def _queue_persist(self, task: DownloadTask) -> None:
        """Persiste a task na `download_queue` (upsert, melhor esforço).

        `created_at` é preenchido pelo history (agora-UTC) quando ausente.
        """
        self._history.queue_upsert(
            {
                "task_id": task.task_id,
                "yt_id": task.yt_id,
                "title": task.title or "",
                "artist": task.artist or "",
                "album": task.album or "",
                "format": task.format,
                "status": task.status,
                "progress": task.progress,
                "retry_count": task._retry_count,
            }
        )

    def _schedule_retry_or_fail(
        self, task: DownloadTask, exc: BaseException, contexto: str
    ) -> None:
        """Falha por rede com retry disponível → agenda; senão `_fail` definitivo.

        Erro de rede (mesma classificação de `ytdlp_client.is_network_error`) com
        `_retry_count < 3`: a task fica `failed` com `_retry_ts` no futuro (5s/30s/
        120s) e o scheduler a re-enfileira quando o tempo vence. Erros não-rede
        (4xx/SearchError) e a 3ª tentativa esgotada caem em `_fail` imediato.
        """
        if is_network_error(exc) and task._retry_count < len(_RETRY_DELAYS):
            task.status = "failed"
            task.stage = "failed"
            task._retry_count += 1
            task._retry_ts = time.monotonic() + _RETRY_DELAYS[task._retry_count - 1]
            task.error = "network"
            self._queue_persist(task)  # persistir também o retry_count (restore)
            self._history.mark(task.yt_id, "failed", error="network")
            self._notify(task.task_id, "failed", task.progress, "failed")
            logger.warning(
                "Download %s falhou por rede (%s) — tentativa %d/%d, retry em %.0fs",
                task.yt_id,
                contexto,
                task._retry_count,
                len(_RETRY_DELAYS),
                _RETRY_DELAYS[task._retry_count - 1],
            )
            return
        self._fail(task, f"{contexto}: {exc}")

    def _retry_scheduler_loop(self) -> None:
        """Daemon: re-enfileira tasks `failed` por rede quando `_retry_ts` vence.

        Varre a cada `_RETRY_SCHEDULER_INTERVAL` (1s); para cada task `failed`
        com `_retry_ts` no passado chama `resume([task_id])` — o resume volta a
        `pending`/queued e o `.part` do yt-dlp continua o download do ponto exato.
        """
        while not self._retry_scheduler_stop.is_set():
            with self._lock:
                due = [
                    t.task_id
                    for t in self._tasks.values()
                    if t.status == "failed"
                    and t._retry_ts is not None
                    and time.monotonic() >= t._retry_ts
                ]
            for task_id in due:
                self.resume([task_id])
            self._retry_scheduler_stop.wait(_RETRY_SCHEDULER_INTERVAL)

    def _restore_queue(self) -> None:
        """Restaura a fila persistida (`download_queue`) no startup.

        `pending`/`downloading` → recria a task e enfileira (o `.part` garante o
        resume do ponto exato); `paused` → recria pausada (sem enfileirar);
        `failed` com retry pendente (0 < retry_count < 3) → recria `failed` com
        `_retry_ts` = agora (o scheduler agenda na 1ª varredura). Demais status
        (done/skipped/cancelled/failed definitivo) → remove da fila.
        """
        for row in self._history.queue_load():
            # Task já registrada em memória (enqueue antes do start) — in-memory
            # wins; restaurar recriaria uma duplicata que sobrescreveria a task
            # real e quebraria cancel()/pause() nas referências existentes.
            if row["task_id"] in self._tasks:
                continue
            status = row.get("status")
            task = DownloadTask(
                task_id=row["task_id"],
                yt_id=row["yt_id"],
                title=row.get("title") or row["yt_id"],
                format=row.get("format") or self._settings.default_format,
                artist=row.get("artist") or None,
                album=row.get("album") or None,
            )
            task._retry_count = int(row.get("retry_count") or 0)
            task.progress = float(row.get("progress") or 0.0)
            if status in ("pending", "downloading"):
                task.status = "pending"
                task.stage = "queued"
                self._register(task)
                if task.task_id not in self._queued:
                    self._queue.put(task)
                    self._queued.add(task.task_id)
            elif status == "paused":
                task.status = "paused"
                task.stage = "paused"
                task.cancel_requested = True
                self._register(task)
            elif status == "failed" and 0 < task._retry_count < len(_RETRY_DELAYS):
                task.status = "failed"
                task.stage = "failed"
                task.error = "network"
                task._retry_ts = time.monotonic()  # retry imediato na 1ª varredura
                self._register(task)
            else:
                self._history.queue_delete(row["task_id"])

    @staticmethod
    def _suffix_path(path: Path) -> Path:
        """Devolve `"nome (2).ext"`, `"nome (3).ext"`, ... — o primeiro que não existe."""
        n = 2
        while True:
            candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
            if not candidate.exists():
                return candidate
            n += 1

    def _progress_hook(self, d: dict) -> None:
        """Hook de progresso do yt-dlp; usa a task atual da thread (thread-local).

        Cancelamento em execução: o yt-dlp chama o hook durante o download —
        levantar `_CancelledError` aqui aborta a extração na hora.
        """
        task = getattr(self._local, "task", None)
        if task is None:
            return
        if getattr(task, "cancel_requested", False):
            raise _CancelledError()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                task.progress = min(100.0, downloaded / total * 100)
            task.stage = "extracting"
            # Rate-limit de notificação: o yt-dlp chama o hook dezenas de vezes
            # por segundo — no máximo ~5 notificações/s por task (o `progress` da
            # task é atualizado SEMPRE; só o notify é limitado).
            now = time.monotonic()
            if now - getattr(task, "_last_progress_ts", 0.0) >= 0.2:
                task._last_progress_ts = now
                self._notify(task.task_id, "running", task.progress, "extracting")
        elif status == "finished":
            task.stage = "converting"  # durante o postprocess (FFmpeg)
            self._notify(task.task_id, "running", task.progress, "converting")

    def _default_executor(
        self,
        yt_id: str,
        fmt: str,
        temp_dir: Path,
        dest_dir: Path,
        dest_filename_stem: str,
        metadata: dict,
    ) -> Path:
        """Executa o yt-dlp para extrair/converter a faixa em `temp_dir`.

        Importa yt-dlp de forma lazy (módulo pesado); só é usado quando nenhum
        executor é injetado (testes injetam um fake). `dest_dir`/`dest_filename_stem`
        fazem parte do contrato do executor, mas aqui o outtmpl aponta para
        `temp_dir` e o arquivo final é movido pelo `_run`. Erros de download
        propagam e viram `failed` na task.
        """
        from yt_dlp import YoutubeDL

        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(temp_dir / "%(title)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": fmt,
                    "preferredquality": "0" if fmt == "mp3" else None,
                },
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
            "progress_hooks": [self._progress_hook],
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": self._settings.socket_timeout,
            "retries": self._settings.retries,
            "retry_delay": 3,
            "noplaylist": True,
            # Download anônimo + client android: única combinação que devolve
            # formatos (2026-08-05); sessão logada é flagada pelo YouTube
            # (player response sem streamingData).
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(f"https://www.youtube.com/watch?v={yt_id}", download=True)
        # Devolve o primeiro arquivo .mp3/.opus criado em temp_dir (pós-postprocess).
        for path in sorted(temp_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in {".mp3", ".opus"}:
                return path
        raise RuntimeError("yt-dlp não produziu arquivo de áudio")
