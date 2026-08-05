"""Camada HTTP do MusicBox: rotas REST, WebSocket /ws, static e startup.

A factory `create_app` recebe as dependências injetáveis (settings, client,
downloader, history) — requisito para os testes (T6) mockarem o cliente e o
executor. O módulo expõe `app` default no import, usado pelo
`uvicorn app.main:app` no startup real.

Comentários/docstrings em português; identificadores em inglês.
"""

import asyncio
import socket
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, load_settings
from .downloader import Downloader
from .history import History
from .models import SearchItem, SearchResults
from .ytdlp_client import NetworkError, NotFoundError, SearchError, YouTubeMusicClient

# Diretório do frontend estático (index.html chega na Task 7).
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Formatos aceitos no POST /api/downloads (mesmo conjunto do downloader).
_VALID_FORMATS = {"mp3", "opus"}


class DownloadRequest(BaseModel):
    """Corpo do POST /api/downloads: música avulsa (`yt_id`) ou álbum (`album_id`).

    `formato` ausente → usa `settings.default_format` (config não fica morta).
    Strings vazias/em-branco em `yt_id`/`album_id` contam como ausentes na
    regra "exatamente um de yt_id/album_id".
    """

    yt_id: str | None = None
    album_id: str | None = None
    formato: str | None = None


def _raise_search_error(exc: SearchError, not_found_detail: str) -> None:
    """Mapeia exceções do cliente para HTTP: NotFound→404, rede→503, demais→502."""
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=not_found_detail)
    if isinstance(exc, NetworkError):
        raise HTTPException(status_code=503, detail="Falha de rede ao consultar o YouTube Music")
    raise HTTPException(status_code=502, detail=str(exc))


def _local_ip() -> str:
    """IP local via socket UDP (spec: conecta em 8.8.8.8:80); fallback 127.0.0.1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _reset_orphan_tasks(history: History) -> None:
    """Startup: registros órfãos (`status == 'running'` de execução anterior) voltam a pending.

    Spec T4/T5: "task volta pending". Tasks vivem em memória e somem no restart;
    o histórico com `running` indica que o processo morreu no meio de um download.
    """
    for record in history.list(limit=1000):
        if record.get("status") == "running":
            history.mark(record["yt_id"], "pending")


def create_app(
    settings: Settings,
    client: YouTubeMusicClient,
    downloader: Downloader,
    history: History | None = None,
) -> FastAPI:
    """Cria o app FastAPI com as dependências injetáveis (factory usada nos testes).

    `history` é opcional e usado no startup (reset de órfãos); se ausente, usa
    o histórico interno do downloader (mesma instância, evitando duplicação).
    """
    if history is None:
        history = downloader._history  # noqa: SLF001 — mesmo pacote, sem API pública

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: descarta .part, reseta órfãos, sobe workers, imprime IP/ffmpeg.
        downloader.cleanup_partials()
        _reset_orphan_tasks(history)
        downloader.start()
        print(f"MusicBox em http://{_local_ip()}:{settings.port}")
        if not settings.has_ffmpeg:
            print("AVISO: ffmpeg não encontrado no PATH — conversões de áudio (ex.: mp3) podem falhar.")
        yield
        # Shutdown: drena a fila e encerra os workers.
        downloader.stop()

    fastapi_app = FastAPI(title="MusicBox", version="0.1.0", lifespan=lifespan)

    # Frontend estático (index.html chega na T7); check_dir=False tolera diretório ausente.
    fastapi_app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    @fastapi_app.get("/", response_model=None)
    def index() -> FileResponse | PlainTextResponse:
        """Serve o index.html do frontend; 503/plain enquanto a T7 não chega."""
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html")
        return PlainTextResponse(
            "Frontend ainda não disponível (index.html chega na Task 7).",
            status_code=503,
        )

    @fastapi_app.get("/api/config")
    def get_config() -> dict:
        """Config leve para a UI: `has_ffmpeg`, `local_ip` e `server_url`."""
        local_ip = _local_ip()
        return {
            "has_ffmpeg": settings.has_ffmpeg,
            "local_ip": local_ip,
            "server_url": f"http://{local_ip}:{settings.port}",
            "default_format": settings.default_format,
        }

    @fastapi_app.get("/api/search")
    def search(q: str) -> dict:
        """Busca artistas/álbuns no YouTube Music. `q` ausente/vazio → 422."""
        if not q.strip():
            raise HTTPException(status_code=422, detail="Parâmetro 'q' é obrigatório")
        try:
            results: SearchResults = client.search(q.strip())
        except SearchError as exc:
            _raise_search_error(exc, "Nenhum resultado encontrado para a busca.")
        return {
            "artists": [asdict(item) for item in results.artists],
            "albums": [asdict(item) for item in results.albums],
        }

    @fastapi_app.get("/api/artists/{artist_name}/albums")
    def artist_albums(artist_name: str) -> list[dict]:
        """Álbuns de um artista pelo NOME (adaptação aprovada — não usa browse id)."""
        try:
            items: list[SearchItem] = client.artist_albums(artist_name)
        except SearchError as exc:
            _raise_search_error(exc, "Nenhum álbum encontrado para o artista.")
        return [asdict(item) for item in items]

    @fastapi_app.get("/api/albums/{browse_id}/tracks")
    def album_tracks(browse_id: str) -> dict:
        """Faixas de um álbum pelo browse_id (Album serializado com asdict)."""
        try:
            album = client.album_tracks(browse_id)
        except SearchError as exc:
            _raise_search_error(exc, "Álbum não encontrado.")
        return asdict(album)

    @fastapi_app.post("/api/downloads", status_code=202)
    def post_downloads(body: DownloadRequest) -> dict:
        """Enfileira um download: música avulsa (`yt_id`) ou álbum inteiro (`album_id`)."""
        formato = body.formato or settings.default_format
        if formato not in _VALID_FORMATS:
            raise HTTPException(status_code=422, detail="formato deve ser 'mp3' ou 'opus'")
        # Strings vazias/em-branco contam como ausentes (regra "exatamente um").
        yt_id = (body.yt_id or "").strip() or None
        album_id = (body.album_id or "").strip() or None
        if (yt_id is None) == (album_id is None):
            raise HTTPException(
                status_code=422,
                detail="Informe exatamente um de 'yt_id' ou 'album_id'",
            )
        if yt_id is not None:
            try:
                task = downloader.enqueue(yt_id, formato)
            except ValueError as exc:  # fmt inválido (defensivo — já validado acima)
                raise HTTPException(status_code=422, detail=str(exc))
            return {"task": task.to_dict()}
        # Modo álbum: busca as faixas e enfileira tudo em uma transação.
        try:
            album = client.album_tracks(album_id)
        except SearchError as exc:
            _raise_search_error(exc, "Álbum não encontrado.")
        if not album.tracks:
            raise HTTPException(status_code=404, detail="Álbum sem faixas para download")
        tasks = downloader.enqueue_album(album.tracks, formato, album.artist, album.title)
        return {"tasks": [task.to_dict() for task in tasks]}

    @fastapi_app.get("/api/downloads")
    def list_downloads() -> list[dict]:
        """Snapshot das tasks em memória (status/progresso/stage ao vivo)."""
        return [task.to_dict() for task in downloader.snapshot()]

    @fastapi_app.get("/api/history")
    def get_history() -> list[dict]:
        """Histórico persistido (colunas: id, yt_id, title, artist, album, format, ...)."""
        return history.list(limit=100)

    @fastapi_app.post("/api/history/{yt_id}/metadata")
    def update_history_metadata(yt_id: str, body: dict) -> dict:
        """Atualiza metadados (título, artista, álbum) no banco e nas tags do arquivo de mídia."""
        title = (body.get("title") or "").strip()
        artist = (body.get("artist") or "").strip()
        album = (body.get("album") or "").strip()
        if not title:
            raise HTTPException(status_code=422, detail="Título é obrigatório")
        success = history.update_tags(yt_id, title, artist, album)
        if not success:
            raise HTTPException(status_code=404, detail="Registro não encontrado")
        return {"status": "ok", "yt_id": yt_id, "title": title, "artist": artist, "album": album}

    @fastapi_app.post("/api/downloads/retry-failed")
    def retry_failed_downloads() -> dict:
        """Re-enfileira todas as faixas com status 'failed' no histórico."""
        records = history.list(limit=1000)
        retried = []
        for r in records:
            if r.get("status") == "failed" and r.get("yt_id"):
                try:
                    task = downloader.enqueue(
                        r["yt_id"],
                        r.get("format") or settings.default_format,
                        r.get("title"),
                        r.get("artist"),
                        r.get("album"),
                    )
                    retried.append(task.to_dict())
                except Exception:
                    pass
        return {"retried_count": len(retried), "tasks": retried}

    @fastapi_app.get("/api/export.m3u")
    def export_m3u() -> Response:
        """Gera e retorna um arquivo .m3u de playlist com todas as faixas concluídas."""
        from fastapi.responses import Response

        records = history.list(limit=1000)
        lines = ["#EXTM3U\n"]
        local_ip = _local_ip()
        server_url = f"http://{local_ip}:{settings.port}"

        for r in records:
            if r.get("status") in ("done", "skipped") and r.get("path"):
                p = Path(r["path"])
                rel = p.relative_to(settings.musicbox_dir) if p.is_relative_to(settings.musicbox_dir) else p
                url_path = "/".join(urllib.parse.quote(part) for part in str(rel).split("/"))
                title = r.get("title") or p.stem
                artist = r.get("artist") or "Desconhecido"
                lines.append(f"#EXTINF:-1,{artist} - {title}\n")
                lines.append(f"{server_url}/api/library/{url_path}\n")

        content = "".join(lines)
        return Response(
            content=content,
            media_type="audio/x-mpegurl",
            headers={"Content-Disposition": 'attachment; filename="musicbox_playlist.m3u"'},
        )

    @fastapi_app.get("/api/library/{rel_path:path}")
    def library_file(rel_path: str) -> FileResponse:
        """Serve um arquivo baixado, validando que está DENTRO de musicbox_dir."""
        musicbox_dir = settings.musicbox_dir.resolve()
        candidate = (musicbox_dir / rel_path).resolve()
        # Path traversal: `resolve()` + `is_relative_to` bloqueiam `..` fora da raiz.
        if not candidate.is_relative_to(musicbox_dir):
            raise HTTPException(status_code=404, detail="Caminho inválido")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado")
        suffix = candidate.suffix.lower()
        media_type = (
            "audio/mpeg"
            if suffix == ".mp3"
            else "audio/ogg"
            if suffix == ".opus"
            else "application/octet-stream"
        )
        return FileResponse(candidate, filename=candidate.name, media_type=media_type)

    @fastapi_app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        """WebSocket de progresso: snapshot ao conectar + updates do downloader.

        O listener é chamado de threads do downloader; o envio é agendado no
        event loop com `run_coroutine_threadsafe`. Um listener por conexão,
        removido ao desconectar ou se o envio falhar (cliente morreu).
        """
        await websocket.accept()
        await websocket.send_json(
            {"type": "snapshot", "tasks": [task.to_dict() for task in downloader.snapshot()]}
        )
        loop = asyncio.get_running_loop()

        def listener(task_id: str, status: str, progress: float, stage: str) -> None:
            """Chamado de thread do downloader; agenda o envio no event loop."""

            async def _send() -> None:
                await websocket.send_json(
                    {
                        "type": "update",
                        "task_id": task_id,
                        "status": status,
                        "progress": progress,
                        "stage": stage,
                    }
                )

            future = asyncio.run_coroutine_threadsafe(_send(), loop)
            future.add_done_callback(_on_send_done)

        def _on_send_done(future: "asyncio.Future[None]") -> None:
            # Envio falhou (cliente morreu) → remove o listener sem derrubar o handler.
            if future.cancelled():
                return
            if future.exception() is not None:
                _remove_listener()

        def _remove_listener() -> None:
            downloader.remove_listener(listener)

        downloader.add_listener(listener)
        try:
            while True:
                # Mantém a conexão viva; mensagens do cliente são ignoradas.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            _remove_listener()

    return fastapi_app


def _build_default_app() -> FastAPI:
    """Monta o app default com as configurações reais (env > .env > defaults)."""
    settings = load_settings()
    history = History(settings.musicbox_dir / "history.db")
    client = YouTubeMusicClient(
        timeout=settings.socket_timeout,
        retries=settings.retries,
        cookies_file=settings.cookies_file,
        cookies_from_browser=settings.cookies_from_browser,
    )
    downloader = Downloader(settings, history, client)
    return create_app(settings, client, downloader, history)


# App default usado pelo `uvicorn app.main:app` (startup real via lifespan).
app = _build_default_app()
