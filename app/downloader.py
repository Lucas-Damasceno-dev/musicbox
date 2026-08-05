"""Fila de downloads do MusicBox (FIFO + thread pool, progresso, sanitização).

O `Downloader` enfileira tarefas (música avulsa ou álbum inteiro), executa o
yt-dlp em `settings.workers` threads daemon, publica progresso via listeners
(registrados pelo main.py para o WebSocket) e persiste o resultado no histórico
SQLite. Um executor pode ser injetado nos testes para validar a lógica sem rede.

Identificadores em inglês; docstrings/comentários em português.
"""

import queue
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .history import History
from .models import DownloadTask, Track
from .ytdlp_client import NetworkError, SearchError, YouTubeMusicClient, _strip_ansi

# Espaço livre mínimo exigido antes de iniciar um download (spec: disco cheio).
_FREE_MIN_BYTES = 50 * 1024 * 1024  # 50 MB

# Formatos aceitos no enqueue/enqueue_album (spec: mp3 | opus).
_VALID_FORMATS = {"mp3", "opus"}

# Sufixos de arquivos de download interrompido (yt-dlp) removidos no startup.
_PARTIAL_SUFFIXES = {".part", ".ytdl"}


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
        self._listeners: list[Callable[[str, str, float, str], None]] = []
        self._stop_flag = threading.Event()
        self._threads: list[threading.Thread] = []
        self._local = threading.local()  # task atual por thread (progress hook)

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        """Sobe `settings.workers` threads daemon que consomem a fila.

        Chamar `start()` de novo após `stop()` recria as threads.
        """
        if any(t.is_alive() for t in self._threads):
            return  # já rodando
        self._stop_flag.clear()
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

    def stop(self) -> None:
        """Sinaliza parada, aguarda a fila drenar e encerra as threads."""
        self._stop_flag.set()
        self._queue.join()  # espera todas as tasks serem consumidas
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
        """Enfileira uma música avulsa e registra no histórico (dedupe por yt_id)."""
        self._validate_format(fmt)
        title = title or yt_id
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
        self._notify(task.task_id, "pending", 0.0, "queued")
        return task

    def enqueue_album(
        self, tracks: list[Track], fmt: str, artist: str, album: str
    ) -> list[DownloadTask]:
        """Enfileira um álbum inteiro: uma única transação SQLite (batch) no histórico."""
        self._validate_format(fmt)
        # Uma transação: monta todas as rows e grava em batch.
        self._history.add_many(
            [
                {
                    "yt_id": t.yt_id,
                    "title": t.title,
                    "artist": artist,
                    "album": album,
                    "format": fmt,
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
            )
            self._register(task)
            self._queue.put(task)
            self._notify(task.task_id, "pending", 0.0, "queued")
            tasks.append(task)
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
            pass

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
        if fmt not in _VALID_FORMATS:
            raise ValueError(f"Formato inválido: {fmt!r} (use 'mp3' ou 'opus')")

    def _register(self, task: DownloadTask) -> None:
        with self._lock:
            self._tasks[task.task_id] = task

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
            try:
                self._process(task)
            except Exception as exc:  # segurança: a thread não deve morrer
                self._fail(task, f"Erro inesperado: {exc}")
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
                self._fail(task, f"Falha ao obter metadados: {exc}")
                return

            if not task.artist:
                task.artist = (metadata.get("artists") or [None])[0] or "Desconhecido"
            if not task.album:
                task.album = metadata.get("album") or "Singles"
            if not task.title or task.title == yt_id:
                task.title = (
                    metadata.get("track") or metadata.get("title") or task.title or yt_id
                )
            self._history.update_meta(yt_id, task.title, task.artist, task.album)

            artist_dir = sanitize_filename(task.artist)
            album_dir = sanitize_filename(task.album)
            title_stem = sanitize_filename(task.title)
            # Número da faixa (flow de álbum) vira prefixo zero-padded: "01 - título".
            stem = f"{task.number:02d} - {title_stem}" if task.number is not None else title_stem
            dest_dir = dest_root / artist_dir / album_dir

            # Execução do yt-dlp (executor injetável).
            temp_dir = dest_root / ".tmp" / task.task_id
            temp_dir.mkdir(parents=True, exist_ok=True)
            try:
                temp_file = Path(
                    self._executor(yt_id, task.format, temp_dir, dest_dir, stem, metadata)
                )
            except Exception as exc:
                self._fail(task, f"Falha no download: {exc}")
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
                    return
                # Existe mas não está done no histórico → sobrescreve com sufixo (2), (3)...
                dest_final = self._suffix_path(dest_final)

            # Move para o destino final (shutil.move resolve filesystems diferentes).
            # O stage "moving" fica visível na task (get/snapshot), mas não gera
            # notify próprio — a sequência de eventos do listener é pending→running→done.
            dest_dir.mkdir(parents=True, exist_ok=True)
            task.stage = "moving"
            shutil.move(str(temp_file), str(dest_final))

            # Conclui.
            task.path = str(dest_final)
            task.status = "done"
            task.progress = 100.0
            task.stage = "done"
            self._notify(task.task_id, "done", 100.0, "done")
            self._history.mark(yt_id, "done", path=str(dest_final))
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self._local.task = None

    def _fail(self, task: DownloadTask, motivo: str) -> None:
        """Marca a task como `failed` com o motivo; não relança (worker não morre).

        O motivo passa por `_strip_ansi` (o yt-dlp imprime erros coloridos que
        apareceriam crus na UI).
        """
        motivo = _strip_ansi(motivo)
        task.error = motivo
        task.status = "failed"
        # stage reflete onde falhou (stage corrente da task).
        self._notify(task.task_id, "failed", task.progress, task.stage)
        self._history.mark(task.yt_id, "failed", error=motivo)

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
        """Hook de progresso do yt-dlp; usa a task atual da thread (thread-local)."""
        task = getattr(self._local, "task", None)
        if task is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                task.progress = min(100.0, downloaded / total * 100)
            task.stage = "extracting"
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
            "noplaylist": True,
            # Download anônimo + client android: única combinação que devolve
            # formatos (2026-08-05); sessão logada é flagada pelo YouTube
            # (player response sem streamingData).
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }
        # Cookies NÃO são usados no download: a sessão logada é flagada pelo
        # YouTube (player response sem streamingData → "Requested format is not
        # available"). O download roda anônimo de propósito.
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(f"https://www.youtube.com/watch?v={yt_id}", download=True)
        # Devolve o primeiro arquivo .mp3/.opus criado em temp_dir (pós-postprocess).
        for path in sorted(temp_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in {".mp3", ".opus"}:
                return path
        raise RuntimeError("yt-dlp não produziu arquivo de áudio")
