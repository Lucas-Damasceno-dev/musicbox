"""Camada HTTP do MusicBox: rotas REST, WebSocket /ws, static e startup.

A factory `create_app` recebe as dependências injetáveis (settings, client,
downloader, history) — requisito para os testes (T6) mockarem o cliente e o
executor. O módulo expõe `app` default no import, usado pelo
`uvicorn app.main:app` no startup real.

Autenticação: se `settings.auth_token` estiver definido (env `MUSICBOX_TOKEN`),
todas as rotas `/api/*` exigem o token — via header `X-MusicBox-Token` ou query
`?token=` (necessário para `<audio>`/downloads, que não enviam header). A rota
`/api/config` é pública e expõe `auth_required` para a UI decidir o fluxo.

Comentários/docstrings em português; identificadores em inglês.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import socket
import threading
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from .config import Settings, load_settings
from .downloader import _PARTIAL_SUFFIXES, VALID_FORMATS, Downloader
from .history import History
from .models import SearchItem, SearchResults
from .playlists import PlaylistStore
from .ytdlp_client import NetworkError, NotFoundError, SearchError, YouTubeMusicClient

# Diretório do frontend estático (index.html servido na raiz "/").
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Limite de conexões SSE simultâneas da busca por streaming (busca consome o
# cliente yt-dlp bloqueante; acima disso as respostas degradam e o YouTube pode
# rate-limitear). Excedeu → 429.
_MAX_SSE_STREAMS = 4
_SSE_SEMAPHORE = threading.BoundedSemaphore(_MAX_SSE_STREAMS)

logger = logging.getLogger("musicbox")

# Logging do app sem pisar no handler do uvicorn (só configura se ninguém já fez).
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class DownloadRequest(BaseModel):
    """Corpo do POST /api/downloads: música (`yt_id`), álbum (`album_id`)
    ou playlist (`playlist_id`).

    `formato` ausente → usa `settings.default_format` (config não fica morta).
    Strings vazias/em-branco contam como ausentes na regra "exatamente um de".
    Campos limitados a 200 chars (inputs de usuário não devem virar queries gigantes).
    """

    yt_id: str | None = Field(default=None, max_length=200)
    album_id: str | None = Field(default=None, max_length=200)
    playlist_id: str | None = Field(default=None, max_length=200)
    formato: str | None = Field(default=None, max_length=200)


class PlaylistCreate(BaseModel):
    """Corpo do POST /api/playlists: `name` (obrigatório, trimado)."""

    name: str = Field(default="", max_length=200)


class PlaylistTrackIn(BaseModel):
    """Corpo do POST /api/playlists/{id}/tracks: `yt_id` (obrigatório)."""

    yt_id: str = Field(default="", max_length=200)


class HistoryMetadataIn(BaseModel):
    """Corpo do POST /api/history/{yt_id}/metadata: metadados opcionais, trimados e limitados.

    Substitui o antigo `body: dict` — inputs sem tipo nem limite. Espaços nas
    bordas são removidos na validação (antes do max_length).
    """

    title: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None
    artist: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None
    album: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None


class _TaskIdsIn(BaseModel):
    """Corpo opcional do POST /api/downloads/pause|resume (lote).

    `task_ids` ausente ou vazio = TODAS as tasks do estado-alvo (paused pausa
    as ativas; resume retoma as pausadas) — contrato do spec de armazenamento.
    """

    task_ids: list[str] | None = None


def _raise_search_error(exc: SearchError, not_found_detail: str) -> None:
    """Mapeia exceções do cliente para HTTP: NotFound→404, rede→503, demais→502."""
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=not_found_detail)
    if isinstance(exc, NetworkError):
        raise HTTPException(status_code=503, detail="Falha de rede ao consultar o YouTube Music")
    raise HTTPException(status_code=502, detail=str(exc))


def _track_sort_key(track: dict) -> tuple[int, str]:
    """Ordena faixas pelo prefixo numérico do nome do arquivo ("NN - título").

    O histórico não guarda o track number como coluna — mas o downloader grava
    os arquivos como "01 - título.ext" — então o número é recuperável do path.
    Faixas sem prefixo numérico vão para o fim (ordenadas por título).
    """
    stem = Path(track["path"]).stem if track.get("path") else ""
    num = None
    dash = stem.find(" - ")
    if dash > 0 and stem[:dash].isdigit():
        num = int(stem[:dash])
    return (num if num is not None else 10**9, str(track.get("title") or "").lower())


def _local_ip() -> str:
    """IP local via socket UDP (spec: conecta em 8.8.8.8:80); fallback 127.0.0.1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# Caracteres de controle que quebrariam a linha EXTINF do .m3u (injeção via
# título/artista com \n). Inclui C0 (\x00-\x1f) e DEL (\x7f).
_M3U_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_m3u_text(text: str) -> str:
    """Substitui quebras de linha e caracteres de controle por espaço (linha EXTINF segura)."""
    return _M3U_CONTROL_RE.sub(" ", text)


def _iter_history_all(history: History, batch: int = 1000) -> list[dict]:
    """Retorna TODO o histórico (paginação em lotes), para operações internas.

    `history.list` ordena por date DESC/id DESC — como as operações internas
    (reset de órfãos, export, retry) não escrevem no banco durante a varredura,
    a paginação por offset é estável e cobre registros além dos 1000 primeiros.
    """
    records: list[dict] = []
    offset = 0
    while True:
        chunk = history.list(limit=batch, offset=offset)
        if not chunk:
            break
        records.extend(chunk)
        offset += len(chunk)
    return records


def _m3u_lines(
    records: list[dict], server_url: str, token_suffix: str, musicbox_dir: Path
) -> list[str]:
    """Linhas do .m3u para os registros baixados (status done/skipped com path).

    URL servida por `/api/library/...`; `token_suffix` é `?token=...` apenas com
    auth ativa (nunca vaza token com auth desligada). Título/artista passam por
    `_sanitize_m3u_text` para não quebrar a linha EXTINF.
    """
    lines = ["#EXTM3U\n"]
    for r in records:
        if r.get("status") in ("done", "skipped") and r.get("path"):
            p = Path(r["path"])
            rel = p.relative_to(musicbox_dir) if p.is_relative_to(musicbox_dir) else p
            url_path = "/".join(urllib.parse.quote(part) for part in str(rel).split("/"))
            title = _sanitize_m3u_text(r.get("title") or p.stem)
            artist = _sanitize_m3u_text(r.get("artist") or "Desconhecido")
            lines.append(f"#EXTINF:-1,{artist} - {title}\n")
            lines.append(f"{server_url}/api/library/{url_path}{token_suffix}\n")
    return lines


def _storage_stats(musicbox_dir: Path) -> dict:
    """Estatísticas de armazenamento do servidor sob `musicbox_dir`.

    Um walk único separando por sufixo (`_PARTIAL_SUFFIXES` = `.part`/`.ytdl`):
    arquivos completos contam na `library_size`; downloads interrompidos contam
    em `partials_size`/`partials_count`. `disk` vem de `shutil.disk_usage`
    (total/used/free). Barato o suficiente para chamada sob demanda.
    """
    if not musicbox_dir.exists():
        musicbox_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(musicbox_dir)
    library_size = 0
    partials_size = 0
    partials_count = 0
    for p in musicbox_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in _PARTIAL_SUFFIXES:
            partials_size += p.stat().st_size
            partials_count += 1
        else:
            library_size += p.stat().st_size
    return {
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        "library_size": library_size,
        "partials_size": partials_size,
        "partials_count": partials_count,
    }


def _reset_orphan_tasks(history: History) -> None:
    """Startup: registros órfãos (`status == 'running'` de execução anterior) voltam a pending.

    Spec T4/T5: "task volta pending". Tasks vivem em memória e somem no restart;
    o histórico com `running` indica que o processo morreu no meio de um download.
    Varre o histórico completo em lotes (não só os primeiros 1000).
    """
    for record in _iter_history_all(history):
        if record.get("status") == "running":
            history.mark(record["yt_id"], "pending")


def create_app(
    settings: Settings,
    client: YouTubeMusicClient,
    downloader: Downloader,
    history: History | None = None,
    playlists: PlaylistStore | None = None,
) -> FastAPI:
    """Cria o app FastAPI com as dependências injetáveis (factory usada nos testes).

    `history` é opcional e usado no startup (reset de órfãos); se ausente, usa
    o histórico interno do downloader (mesma instância, evitando duplicação).
    `playlists` é opcional; se ausente, cria o store padrão em musicbox_dir.
    """
    if history is None:
        history = downloader._history  # noqa: SLF001 — mesmo pacote, sem API pública
    if playlists is None:
        playlists = PlaylistStore(settings.musicbox_dir / "playlists.db")

    def _token_matches(conn: Request | WebSocket) -> bool:
        """True se a conexão traz o token correto (ou a auth está desativada).

        Aceita Request (header `X-MusicBox-Token`) e WebSocket/query (ambos têm
        `headers` e `query_params`) — o `?token=` cobre `<audio>` e downloads.
        """
        if not settings.auth_token:
            return True
        token = conn.headers.get("X-MusicBox-Token") or conn.query_params.get("token")
        if not token:
            return False
        return secrets.compare_digest(token, settings.auth_token)

    def require_auth(request: Request) -> None:
        """Dependência FastAPI: bloqueia /api/* sem token válido (401)."""
        if not _token_matches(request):
            raise HTTPException(status_code=401, detail="Token de acesso ausente ou inválido")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: descarta .part, reseta órfãos, sobe workers, imprime IP/ffmpeg.
        downloader.cleanup_partials()
        _reset_orphan_tasks(history)
        downloader.start()
        logger.info("MusicBox em http://%s:%s", _local_ip(), settings.port)
        if settings.auth_token:
            logger.info("Autenticação por token ativada (MUSICBOX_TOKEN)")
        else:
            # Auth desativada (padrão) + app alcançável na LAN/0.0.0.0 = API aberta.
            # O host de bind é decisão do uvicorn, então o aviso é incondicional
            # quando não há token — mais seguro que calar em algum cenário.
            logger.warning(
                "API sem autenticação — defina MUSICBOX_TOKEN para proteger "
                "o servidor (http://%s:%s) quando exposto na LAN/0.0.0.0.",
                _local_ip(),
                settings.port,
            )
        if not settings.has_ffmpeg:
            logger.warning(
                "ffmpeg não encontrado no PATH — conversões de áudio (ex.: mp3) podem falhar."
            )
        yield
        # Shutdown: drena a fila e encerra os workers.
        downloader.stop()

    fastapi_app = FastAPI(title="MusicBox", version="0.1.0", lifespan=lifespan)

    # Allowlist de Origins para o guard de CSRF/drive-by (abaixo). Fonte: env
    # `ALLOWED_ORIGINS` (separada por vírgula) ou `settings.allowed_origins` se a
    # config expuser o campo no futuro. Vazio = só o próprio host da request.
    _allowed_origins = getattr(settings, "allowed_origins", None) or os.environ.get(
        "ALLOWED_ORIGINS", ""
    )
    if isinstance(_allowed_origins, str):
        _allowed_origins = {o.strip().lower() for o in _allowed_origins.split(",") if o.strip()}
    else:
        _allowed_origins = {str(o).lower() for o in _allowed_origins}

    def _origin_allowed(origin: str, request: Request) -> bool:
        """True se a Origin bate com o host da request (mesmo site) ou está na allowlist.

        Proteção contra CSRF/drive-by em métodos que escrevem (POST/PUT/PATCH/DELETE):
        um navegador de outra origem não consegue disparar mutações na API.
        Requisições sem header Origin (curl, scripts, apps nativos) seguem livres —
        quem precisar de proteção de verdade usa o token (`MUSICBOX_TOKEN`).
        """
        try:
            parts = urllib.parse.urlsplit(origin)
            origin_host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if not origin_host:
            return False
        if origin_host == (request.url.hostname or "").lower():
            # Porta: compara apenas quando a Origin especificou e a request tem.
            if parts.port is not None and request.url.port is not None:
                return parts.port == request.url.port
            return True
        return origin_host in _allowed_origins

    @fastapi_app.middleware("http")
    async def origin_guard(request: Request, call_next):
        """Bloqueia mutações vindas de Origins estranhas (403) — nunca 401/redirect."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin and not _origin_allowed(origin, request):
                return Response(
                    status_code=403,
                    content="Origin não permitida",
                    media_type="text/plain",
                )
        return await call_next(request)

    # Frontend estático (PWA em app/static); check_dir=False tolera diretório ausente.
    fastapi_app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    @fastapi_app.get("/", response_model=None)
    def index() -> FileResponse | PlainTextResponse:
        """Serve o index.html do frontend (PWA); 503/plain se o arquivo não existir."""
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html")
        return PlainTextResponse(
            "Frontend não encontrado (app/static/index.html ausente).",
            status_code=503,
        )

    @fastapi_app.get("/api/config")
    def get_config() -> dict:
        """Config leve para a UI: `has_ffmpeg`, `local_ip`, `server_url` e `auth_required`.

        Rota pública de propósito: sem ela a UI não saberia que precisa de token.
        Nada sensível é exposto (o token em si nunca sai daqui).
        """
        local_ip = _local_ip()
        return {
            "has_ffmpeg": settings.has_ffmpeg,
            "local_ip": local_ip,
            "server_url": f"http://{local_ip}:{settings.port}",
            "default_format": settings.default_format,
            "auth_required": settings.auth_token is not None,
        }

    @fastapi_app.get("/api/search", dependencies=[Depends(require_auth)])
    def search(q: str, limit: int = 10) -> dict:
        """Busca artistas/álbuns/músicas/playlists no YouTube Music.

        `q` vazio → 422; `limit` (1..40) controla quantos itens por seção o
        cliente expande — mais resultados custam requisições extras ao YouTube
        (títulos flat), por isso a UI oferece "Carregar mais".
        """
        if not q.strip():
            raise HTTPException(status_code=422, detail="Parâmetro 'q' é obrigatório")
        if not 1 <= limit <= 40:
            raise HTTPException(status_code=422, detail="'limit' deve estar entre 1 e 40")
        try:
            results: SearchResults = client.search(q.strip(), limit)
        except SearchError as exc:
            _raise_search_error(exc, "Nenhum resultado encontrado para a busca.")
        return {
            "artists": [asdict(item) for item in results.artists],
            "albums": [asdict(item) for item in results.albums],
            "songs": [asdict(item) for item in results.songs],
            "playlists": [asdict(item) for item in results.playlists],
        }

    @fastapi_app.get("/api/search/stream", dependencies=[Depends(require_auth)])
    async def search_stream(q: str, limit: int = 10) -> StreamingResponse:
        """SSE da busca: emite cada seção assim que ela resolve (~11–20s no total).

        Eventos: `section` (data: {kind: [items]}) por seção, `done` ao final,
        `error` (data: {detail}) se a busca falhar. A busca roda em thread
        (yt-dlp é bloqueante) e o callback entrega as seções ao event loop.
        Máx. 4 streams simultâneos (429 acima); heartbeat `: ping` a cada ~15s.
        """
        if not q.strip():
            raise HTTPException(status_code=422, detail="Parâmetro 'q' é obrigatório")
        if not 1 <= limit <= 40:
            raise HTTPException(status_code=422, detail="'limit' deve estar entre 1 e 40")
        # Limite de streams simultâneos: cada um consome o cliente bloqueante.
        if not _SSE_SEMAPHORE.acquire(blocking=False):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Muitos streams de busca simultâneos (máx. {_MAX_SSE_STREAMS}). "
                    "Tente novamente em instantes."
                ),
            )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # Flag de cancelamento: setada quando o cliente desconecta/termina; a
        # thread de busca para de enfileirar e sai (sem thread órfã presa).
        cancel_event = threading.Event()

        def _on_section(kind: str, items: list[SearchItem]) -> None:
            if cancel_event.is_set():
                return
            loop.call_soon_threadsafe(
                queue.put_nowait,
                ("section", kind, [asdict(item) for item in items]),
            )

        def _run() -> None:
            try:
                client.search(q.strip(), limit, on_section=_on_section)
                if not cancel_event.is_set():
                    loop.call_soon_threadsafe(queue.put_nowait, ("done",))
            except SearchError as exc:
                if not cancel_event.is_set():
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            except Exception as exc:  # segurança: nunca deixa a thread morrer muda
                if not cancel_event.is_set():
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

        threading.Thread(target=_run, daemon=True).start()

        async def _events():
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # Heartbeat: mantém proxies/navegadores vivos sem evento ~15s.
                        yield ": ping\n\n"
                        continue
                    if msg[0] == "section":
                        _, kind, items = msg
                        yield (
                            "event: section\ndata: "
                            f"{json.dumps({kind: items}, ensure_ascii=False)}\n\n"
                        )
                    elif msg[0] == "done":
                        yield "event: done\ndata: {}\n\n"
                        break
                    else:
                        yield (
                            "event: error\ndata: "
                            f"{json.dumps({'detail': msg[1]}, ensure_ascii=False)}\n\n"
                        )
                        break
            finally:
                # Cliente desconectou ou stream terminou → cancela a busca e
                # libera a vaga do semáforo (sem thread órfã, sem leak).
                cancel_event.set()
                _SSE_SEMAPHORE.release()

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},  # SSE não pode ser cacheado
        )

    @fastapi_app.get("/api/browse", dependencies=[Depends(require_auth)])
    def browse_library() -> list[dict]:
        """Biblioteca navegável: artistas → álbuns → faixas (só registros com arquivo).

        Consome o histórico (done/skipped com path) e agrupa em árvore. A tela
        Biblioteca do frontend usa esta estrutura para navegar como um app de música.
        """
        artists: dict[str, dict] = {}
        for r in history.list(limit=1000):
            if r.get("status") not in ("done", "skipped") or not r.get("path"):
                continue
            artist = r.get("artist") or "Desconhecido"
            album = r.get("album") or "Singles"
            artist_entry = artists.setdefault(artist, {"name": artist, "albums": {}})
            album_entry = artist_entry["albums"].setdefault(
                album, {"name": album, "cover_url": None, "tracks": []}
            )
            album_entry["cover_url"] = album_entry["cover_url"] or r.get("cover_url")
            album_entry["tracks"].append(
                {
                    "yt_id": r["yt_id"],
                    "title": r.get("title") or "Sem título",
                    "artist": artist,
                    "album": album,
                    "path": r["path"],
                    "cover_url": r.get("cover_url"),
                    "format": r.get("format"),
                }
            )
        result = []
        for artist_entry in artists.values():
            albums = sorted(
                artist_entry["albums"].values(),
                key=lambda a: str(a["name"]).lower(),
            )
            # Faixas na ordem do álbum (prefixo numérico do arquivo), não da data.
            for album_entry in albums:
                album_entry["tracks"].sort(key=_track_sort_key)
            result.append({"name": artist_entry["name"], "albums": albums})
        result.sort(key=lambda a: str(a["name"]).lower())
        return result

    @fastapi_app.get("/api/artists/{artist_name}/albums", dependencies=[Depends(require_auth)])
    def artist_albums(artist_name: str) -> list[dict]:
        """Álbuns de um artista pelo NOME (adaptação aprovada — não usa browse id)."""
        try:
            items: list[SearchItem] = client.artist_albums(artist_name)
        except SearchError as exc:
            _raise_search_error(exc, "Nenhum álbum encontrado para o artista.")
        return [asdict(item) for item in items]

    @fastapi_app.get("/api/albums/{browse_id}/tracks", dependencies=[Depends(require_auth)])
    def album_tracks(browse_id: str) -> dict:
        """Faixas de um álbum/playlist pelo id (Album serializado com asdict)."""
        try:
            album = client.album_tracks(browse_id)
        except SearchError as exc:
            _raise_search_error(exc, "Álbum/playlist não encontrado.")
        return asdict(album)

    @fastapi_app.post("/api/downloads", status_code=202, dependencies=[Depends(require_auth)])
    def post_downloads(body: DownloadRequest) -> dict:
        """Enfileira um download: música avulsa (`yt_id`), álbum (`album_id`) ou
        playlist (`playlist_id` — id PL/VL/OLAK resolve pelo mesmo fluxo do álbum)."""
        formato = body.formato or settings.default_format
        if formato not in VALID_FORMATS:
            raise HTTPException(status_code=422, detail="formato deve ser 'mp3' ou 'opus'")
        # Strings vazias/em-branco contam como ausentes (regra "exatamente um").
        yt_id = (body.yt_id or "").strip() or None
        album_id = (body.album_id or "").strip() or None
        playlist_id = (body.playlist_id or "").strip() or None
        filled = sum(x is not None for x in (yt_id, album_id, playlist_id))
        if filled != 1:
            raise HTTPException(
                status_code=422,
                detail="Informe exatamente um de 'yt_id', 'album_id' ou 'playlist_id'",
            )
        if yt_id is not None:
            try:
                task = downloader.enqueue(yt_id, formato)
            except ValueError as exc:  # fmt inválido (defensivo — já validado acima)
                raise HTTPException(status_code=422, detail=str(exc))
            return {"task": task.to_dict()}
        # Modo álbum/playlist: busca as faixas e enfileira tudo em uma transação.
        target_id = album_id or playlist_id
        not_found_msg = "Álbum não encontrado." if album_id else "Playlist não encontrada."
        try:
            album = client.album_tracks(target_id)
        except SearchError as exc:
            _raise_search_error(exc, not_found_msg)
        if not album.tracks:
            raise HTTPException(status_code=404, detail="Sem faixas para download")
        tasks = downloader.enqueue_album(album.tracks, formato, album.artist, album.title)
        return {"tasks": [task.to_dict() for task in tasks]}

    @fastapi_app.get("/api/downloads", dependencies=[Depends(require_auth)])
    def list_downloads() -> list[dict]:
        """Snapshot das tasks em memória (status/progresso/stage ao vivo)."""
        return [task.to_dict() for task in downloader.snapshot()]

    @fastapi_app.delete("/api/downloads/{task_id}", dependencies=[Depends(require_auth)])
    def cancel_download(task_id: str) -> dict:
        """Cancela uma tarefa pendente ou em execução (404 se inexistente/terminal)."""
        if not downloader.cancel(task_id):
            raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        return {"status": "cancelled", "task_id": task_id}

    @fastapi_app.get("/api/storage", dependencies=[Depends(require_auth)])
    def get_storage() -> dict:
        """Estatísticas de armazenamento do servidor (disco + biblioteca + .part)."""
        return _storage_stats(settings.musicbox_dir)

    @fastapi_app.post("/api/storage/cleanup", dependencies=[Depends(require_auth)])
    def cleanup_storage() -> dict:
        """Remove `.part`/`.ytdl` órfãos e reporta quantos/quanto foram limpos."""
        before = _storage_stats(settings.musicbox_dir)
        downloader.cleanup_partials()
        after = _storage_stats(settings.musicbox_dir)
        return {
            "removed": before["partials_count"] - after["partials_count"],
            "freed_bytes": before["partials_size"] - after["partials_size"],
        }

    @fastapi_app.post("/api/downloads/pause", dependencies=[Depends(require_auth)])
    def pause_downloads(body: _TaskIdsIn | None = None) -> dict:
        """Pausa downloads em lote (task_ids opcional; ausente/vazio = todas as ativas)."""
        return {"paused": downloader.pause(body.task_ids or None if body else None)}

    @fastapi_app.post("/api/downloads/resume", dependencies=[Depends(require_auth)])
    def resume_downloads(body: _TaskIdsIn | None = None) -> dict:
        """Retoma downloads pausados em lote (task_ids opcional; ausente/vazio = todos)."""
        return {"resumed": downloader.resume(body.task_ids or None if body else None)}

    @fastapi_app.post("/api/downloads/{task_id}/pause", dependencies=[Depends(require_auth)])
    def pause_download(task_id: str) -> dict:
        """Pausa uma task individual (404 se inexistente; 409 se não estiver ativa)."""
        task = downloader.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task não encontrada")
        if task.status in ("done", "failed", "skipped", "cancelled", "paused"):
            raise HTTPException(status_code=409, detail=f"estado incompatível: {task.status}")
        downloader.pause([task_id])
        return {"task_id": task_id, "status": "paused"}

    @fastapi_app.post("/api/downloads/{task_id}/resume", dependencies=[Depends(require_auth)])
    def resume_download(task_id: str) -> dict:
        """Retoma uma task pausada (404 se inexistente; 409 se não estiver paused)."""
        task = downloader.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task não encontrada")
        if task.status != "paused":
            raise HTTPException(status_code=409, detail=f"estado incompatível: {task.status}")
        downloader.resume([task_id])
        return {"task_id": task_id, "status": "pending"}

    @fastapi_app.get("/api/history", dependencies=[Depends(require_auth)])
    def get_history() -> list[dict]:
        """Histórico persistido (colunas: id, yt_id, title, artist, album, format, ...)."""
        return history.list(limit=100)

    @fastapi_app.post("/api/history/{yt_id}/metadata", dependencies=[Depends(require_auth)])
    def update_history_metadata(yt_id: str, body: HistoryMetadataIn) -> dict:
        """Atualiza metadados (título, artista, álbum) no banco e nas tags do arquivo de mídia."""
        title = (body.title or "").strip()
        artist = (body.artist or "").strip()
        album = (body.album or "").strip()
        if not title:
            raise HTTPException(status_code=422, detail="Título é obrigatório")
        success = history.update_tags(yt_id, title, artist, album)
        if not success:
            raise HTTPException(status_code=404, detail="Registro não encontrado")
        return {"status": "ok", "yt_id": yt_id, "title": title, "artist": artist, "album": album}

    @fastapi_app.delete("/api/history/{yt_id}", dependencies=[Depends(require_auth)])
    def delete_history(yt_id: str) -> dict:
        """Remove o registro do histórico E o arquivo de mídia (se existir e
        dentro de musicbox_dir)."""
        record = history.get(yt_id)
        if not record:
            raise HTTPException(status_code=404, detail="Registro não encontrado")
        file_path = record.get("path")
        if file_path:
            root = settings.musicbox_dir.resolve()
            try:
                candidate = Path(file_path).resolve()
                if candidate.is_relative_to(root):
                    if candidate.is_file():
                        candidate.unlink()
                    # Remove também a letra (`.lrc`) irmã, se existir (missing_ok:
                    # faixa sem letra não vira erro).
                    candidate.with_suffix(".lrc").unlink(missing_ok=True)
            except OSError:
                pass  # falha ao apagar o arquivo não impede a remoção do registro
        history.delete(yt_id)
        return {"status": "ok", "yt_id": yt_id}

    @fastapi_app.get("/api/library/{yt_id}/lyrics", dependencies=[Depends(require_auth)])
    def get_lyrics(yt_id: str) -> PlainTextResponse:
        """Serve a letra (`.lrc`) baixada junto com a faixa; 404 sem letra.

        Registrada ANTES de `/api/library/{rel_path:path}` — o path converter
        também casaria `{yt_id}/lyrics` como caminho relativo.
        """
        record = history.get(yt_id)
        if not record or not record.get("path"):
            raise HTTPException(status_code=404, detail="Sem letra")
        lrc = Path(record["path"]).with_suffix(".lrc")
        if not lrc.is_file():
            raise HTTPException(status_code=404, detail="Sem letra")
        return PlainTextResponse(lrc.read_text(encoding="utf-8"))

    @fastapi_app.get("/api/playlists", dependencies=[Depends(require_auth)])
    def list_playlists() -> list[dict]:
        """Todas as playlists do usuário (com contagem de faixas)."""
        return playlists.list_all()

    @fastapi_app.post("/api/playlists", status_code=201, dependencies=[Depends(require_auth)])
    def create_playlist(body: PlaylistCreate) -> dict:
        """Cria uma playlist com `name` (obrigatório, trimado)."""
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Nome da playlist é obrigatório")
        return playlists.create(name)

    @fastapi_app.delete("/api/playlists/{playlist_id}", dependencies=[Depends(require_auth)])
    def delete_playlist(playlist_id: int) -> dict:
        """Apaga uma playlist (faixas somem em cascata)."""
        if not playlists.delete(playlist_id):
            raise HTTPException(status_code=404, detail="Playlist não encontrada")
        return {"status": "ok", "playlist_id": playlist_id}

    @fastapi_app.get("/api/playlists/{playlist_id}", dependencies=[Depends(require_auth)])
    def get_playlist(playlist_id: int) -> dict:
        """Playlist com as faixas (metadados vindos do histórico, quando baixadas)."""
        pl = playlists.get(playlist_id)
        if not pl:
            raise HTTPException(status_code=404, detail="Playlist não encontrada")
        yt_ids = playlists.track_ids(playlist_id)
        recs = history.get_many(yt_ids)  # uma conexão para todas as faixas
        tracks = []
        for yt_id in yt_ids:
            rec = recs.get(yt_id) or {}
            playable = rec.get("status") in ("done", "skipped") and rec.get("path")
            tracks.append(
                {
                    "yt_id": yt_id,
                    "title": rec.get("title") or yt_id,
                    "artist": rec.get("artist"),
                    "album": rec.get("album"),
                    "path": rec.get("path") if playable else None,
                    "cover_url": rec.get("cover_url"),
                    "status": rec.get("status") or "pending",
                }
            )
        pl["tracks"] = tracks
        return pl

    @fastapi_app.post(
        "/api/playlists/{playlist_id}/tracks",
        status_code=201,
        dependencies=[Depends(require_auth)],
    )
    def add_playlist_track(playlist_id: int, body: PlaylistTrackIn) -> dict:
        """Adiciona uma faixa (`yt_id`) à playlist (dedupe por yt_id)."""
        if not playlists.get(playlist_id):
            raise HTTPException(status_code=404, detail="Playlist não encontrada")
        yt_id = body.yt_id.strip()
        if not yt_id:
            raise HTTPException(status_code=422, detail="'yt_id' é obrigatório")
        playlists.add_track(playlist_id, yt_id)
        return {"status": "ok", "playlist_id": playlist_id, "yt_id": yt_id}

    @fastapi_app.delete(
        "/api/playlists/{playlist_id}/tracks/{yt_id}",
        dependencies=[Depends(require_auth)],
    )
    def remove_playlist_track(playlist_id: int, yt_id: str) -> dict:
        """Remove uma faixa da playlist."""
        if not playlists.get(playlist_id):
            raise HTTPException(status_code=404, detail="Playlist não encontrada")
        playlists.remove_track(playlist_id, yt_id)
        return {"status": "ok", "playlist_id": playlist_id, "yt_id": yt_id}

    @fastapi_app.get(
        "/api/playlists/{playlist_id}/export.m3u",
        dependencies=[Depends(require_auth)],
    )
    def export_playlist_m3u(playlist_id: int) -> Response:
        """Exporta a playlist como .m3u (URLs do /api/library, prontas para players externos).

        Compartilha `_m3u_lines` com o export global (sem duplicação); o token
        só entra na URL quando a auth está ativa.
        """
        pl = playlists.get(playlist_id)
        if not pl:
            raise HTTPException(status_code=404, detail="Playlist não encontrada")
        yt_ids = playlists.track_ids(playlist_id)
        records = list(history.get_many(yt_ids).values())  # uma conexão, não uma por faixa
        server_url = f"http://{_local_ip()}:{settings.port}"
        token_suffix = (
            f"?token={urllib.parse.quote(settings.auth_token)}" if settings.auth_token else ""
        )
        return Response(
            content="".join(_m3u_lines(records, server_url, token_suffix, settings.musicbox_dir)),
            media_type="audio/x-mpegurl",
            headers={"Content-Disposition": f'attachment; filename="playlist_{playlist_id}.m3u"'},
        )

    @fastapi_app.post("/api/downloads/retry-failed", dependencies=[Depends(require_auth)])
    def retry_failed_downloads() -> dict:
        """Re-enfileira todas as faixas com status 'failed' no histórico.

        Erros individuais não são engolidos: aparecem em `errors` (e no log)
        para o cliente saber o que não foi re-enfileirado.
        """
        records = _iter_history_all(history)  # cobre o histórico completo, não só 1000
        retried = []
        errors = []
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
                except Exception as exc:  # falha isolada não derruba o retry dos demais
                    logger.warning("Falha ao re-enfileirar %s: %s", r.get("yt_id"), exc)
                    errors.append({"yt_id": r.get("yt_id"), "error": str(exc)})
        return {"retried_count": len(retried), "tasks": retried, "errors": errors}

    @fastapi_app.get("/api/export.m3u", dependencies=[Depends(require_auth)])
    def export_m3u() -> Response:
        """Gera e retorna um arquivo .m3u de playlist com todas as faixas concluídas."""
        # Varre o histórico completo em lotes — `limit=1000` fixo esconderia dados.
        records = _iter_history_all(history)
        server_url = f"http://{_local_ip()}:{settings.port}"
        token_suffix = (
            f"?token={urllib.parse.quote(settings.auth_token)}" if settings.auth_token else ""
        )
        return Response(
            content="".join(_m3u_lines(records, server_url, token_suffix, settings.musicbox_dir)),
            media_type="audio/x-mpegurl",
            headers={"Content-Disposition": 'attachment; filename="musicbox_playlist.m3u"'},
        )

    @fastapi_app.get("/api/library/{rel_path:path}", dependencies=[Depends(require_auth)])
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

        Autenticação via query `?token=` (WebSocket do navegador não envia
        header customizado); com auth ativa e token ausente/errado → 4401.
        O listener é chamado de threads do downloader; o envio é agendado no
        event loop com `run_coroutine_threadsafe`. Um listener por conexão,
        removido ao desconectar ou se o envio falhar (cliente morreu).
        """
        await websocket.accept()
        if not _token_matches(websocket):
            await websocket.close(code=4401)
            return
        await websocket.send_json(
            {"type": "snapshot", "tasks": [task.to_dict() for task in downloader.snapshot()]}
        )
        loop = asyncio.get_running_loop()

        # Fila de updates a enviar. Se acumular (flood de notificações), coalesce:
        # descarta as pendentes e mantém só o estado mais recente — a UI quer o
        # último progresso, não os N intermediários. Envio em task própria para o
        # erro nunca propagar para o loop da conexão.
        send_queue: asyncio.Queue = asyncio.Queue()
        sender_failed = asyncio.Event()  # setado quando o envio falha (cliente morto)

        def listener(task_id: str, status: str, progress: float, stage: str) -> None:
            """Chamado de thread do downloader; enfileira o update no event loop."""

            async def _enqueue() -> None:
                try:
                    if send_queue.qsize() > 50:
                        while not send_queue.empty():
                            send_queue.get_nowait()
                    send_queue.put_nowait(
                        {
                            "type": "update",
                            "task_id": task_id,
                            "status": status,
                            "progress": progress,
                            "stage": stage,
                        }
                    )
                except Exception:  # nunca propaga para o loop da conexão
                    pass

            try:
                future = asyncio.run_coroutine_threadsafe(_enqueue(), loop)
            except RuntimeError:
                return  # event loop já encerrado (conexão fechada)
            future.add_done_callback(_on_enqueue_done)

        def _on_enqueue_done(future: "asyncio.Future[None]") -> None:
            if future.cancelled():
                return
            exc = future.exception()
            if exc is not None:
                logger.debug("Falha ao enfileirar update WS: %s", exc)

        async def _sender() -> None:
            """Consome a fila e envia; em falha, sinaliza para fechar a conexão."""
            while True:
                msg = await send_queue.get()
                try:
                    await websocket.send_json(msg)
                except Exception:
                    sender_failed.set()
                    return

        def _remove_listener() -> None:
            downloader.remove_listener(listener)

        downloader.add_listener(listener)
        sender_task = asyncio.create_task(_sender())
        try:
            while True:
                # receive com timeout (30s) → heartbeat; mensagens do cliente ignoradas.
                recv_task = asyncio.create_task(
                    asyncio.wait_for(websocket.receive_text(), timeout=30)
                )
                failed_task = asyncio.create_task(sender_failed.wait())
                done, pending = await asyncio.wait(
                    {recv_task, failed_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if failed_task in done:
                    break  # envio falhou (cliente morreu) → encerra a conexão
                try:
                    await recv_task  # recebeu mensagem ou TimeoutError
                except asyncio.TimeoutError:
                    # Heartbeat ~30s: mantém proxies vivos; se o ping falhar
                    # (cliente morto), a exceção derruba o loop e o finally limpa.
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass  # erro de envio/heartbeat com cliente morto → encerra limpo
        finally:
            sender_task.cancel()
            _remove_listener()

    return fastapi_app


def _build_default_app() -> FastAPI:
    """Monta o app default com as configurações reais (env > .env > defaults)."""
    settings = load_settings()
    history = History(settings.musicbox_dir / "history.db")
    client = YouTubeMusicClient(
        timeout=settings.socket_timeout,
        retries=settings.retries,
        cache_path=settings.musicbox_dir / "search_cache.db",
    )
    downloader = Downloader(settings, history, client)
    playlists = PlaylistStore(settings.musicbox_dir / "playlists.db")
    return create_app(settings, client, downloader, history, playlists)


# App default usado pelo `uvicorn app.main:app` (startup real via lifespan).
app = _build_default_app()
