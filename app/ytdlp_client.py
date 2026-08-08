"""Cliente do YouTube Music via yt-dlp: busca, álbum→faixas e metadados de download.

Adaptado ao yt-dlp 2026.07.04 (decisão aprovada em 2026-08-04):
- `ytmsearch:` foi removido → busca via URL `https://music.youtube.com/search?q=<query>`
  (`YoutubeMusicSearchURLIE`).
- A busca usa as seções nativas do extractor via parâmetro `sp` (`#albums`/`#artists`),
  o que elimina a heurística de classificação por URL (entries flat têm id MPRE/UC/OLAK).
- **Entries flat NÃO trazem título** (só `id`/`url`) no yt-dlp 2026.07.04 — obter título
  custa 1-2 requisições por item (álbum: browse MPRE → redirect → playlist OLAK). Por isso
  `search()` expande no máximo `max_results` itens por seção (a "1 request só" não é
  possível com títulos; expansão completa custaria ~10s por álbum).
- Páginas de artista NÃO são suportadas → `artist_albums` = busca filtrada a álbuns.
- Álbum: faixas sem `track_number` (numerar por posição) e sem ano (year=None).

Identificadores em inglês; docstrings/comentários em português.
"""

import json
import re
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import yt_dlp

from .models import Album, SearchItem, SearchResults, Track


class SearchError(Exception):
    """Erro genérico de busca/metadados no YouTube Music."""


class NotFoundError(SearchError):
    """Busca sem resultados (→ HTTP 404)."""


class NetworkError(SearchError):
    """Falha de rede após esgotar as tentativas (→ HTTP 503)."""


# Seções de busca do YoutubeMusicSearchURLIE (mesmos valores de yt_dlp _SECTIONS).
_SEARCH_SECTIONS = {
    "albums": "EgWKAQIYAWoKEAoQAxAEEAkQBQ==",
    "artists": "EgWKAQIgAWoKEAoQAxAEEAkQBQ==",
    "songs": "EgWKAQIIAWoKEAoQAxAEEAkQBQ==",
    "playlists": "EgeKAQQoADgBagwQAxAJEAQQDhAKEAU==",  # "featured playlists"
}

# kind canônico do SearchItem por seção (contrato em models.py).
_SECTION_KIND = {
    "albums": "album",
    "artists": "artist",
    "songs": "song",
    "playlists": "playlist",
}

# Ordem canônica de emissão das seções (o SSE entrega nesta ordem).
_SECTION_ORDER = ("songs", "albums", "artists", "playlists")

# Título de álbum/playlist do YT Music vem "mangled": prefixo "Album - " e
# sufixo "(N Songs)" (case-insensitive — playlists usam "songs" minúsculo).
_ALBUM_TITLE_PREFIX = re.compile(r"^\s*Album\s*-\s*")
_ALBUM_TITLE_SUFFIX = re.compile(r"\s*\(\d+ songs?\)\s*$", re.IGNORECASE)

# Marcadores de falha de rede/timeout presentes em mensagens de exceção do yt-dlp.
_NETWORK_TOKENS = (
    "timed out",
    "timeout",
    "connection",
    "unreachable",
    "temporary failure",
    "name or service not known",
    "not known",
    "getaddrinfo",
    "reset by peer",
)

# Códigos de escape ANSI (CSI): cores/estilos (`\x1b[...m`), cursor/limpeza
# (`\x1b[K`, `\x1b[H`, `\x1b[2J`, ...). O yt-dlp imprime erros coloridos que
# apareciam crus na UI (ex.: "Sign in to confirm you're not a bot").
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# TTL do cache de busca (memória e disco), em segundos.
_CACHE_TTL = 600

# Workers para resolver títulos de entries flat em paralelo (cada título
# custa 1-2 requisições ao YouTube; em série isso dominava o tempo da busca).
_RESOLVE_WORKERS = 4

# TTL do cache de TÍTULOS resolvidos por URL (títulos de álbum/canal/playlist
# raramente mudam e as URLs de browse MPRE/UC/OLAK/PL/VL são estáveis — 7 dias
# corta resolves repetidos entre buscas parecidas e sessões. Medido: ~9 de 25
# resolves são duplicatas dentro de uma busca).
_TITLE_TTL = 604800  # 7 dias (antes 1h = 3600s)


def _results_from_json(data: str) -> SearchResults:
    """Reconstrói um `SearchResults` (com `SearchItem`s) do JSON do cache em disco."""
    raw = json.loads(data)
    return SearchResults(
        artists=[SearchItem(**item) for item in raw.get("artists", [])],
        albums=[SearchItem(**item) for item in raw.get("albums", [])],
        songs=[SearchItem(**item) for item in raw.get("songs", [])],
        playlists=[SearchItem(**item) for item in raw.get("playlists", [])],
    )


def _strip_ansi(text: str) -> str:
    """Remove códigos de escape ANSI de uma string (cores, cursor e limpeza)."""
    return _ANSI_ESCAPE.sub("", text)


def _is_network_error(exc: BaseException) -> bool:
    """True se a exceção (ou sua cadeia de causas/mensagem) indica falha de rede ou timeout.

    Erros HTTP 4xx do YouTube (400/403/404/429...) são PERMANENTES — repetir não
    resolve e só atrasa a resposta — por isso NÃO contam como falha de rede e
    não disparam o retry com backoff do app. 5xx continua sendo transitório (rede).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # HTTPError do yt-dlp expõe `.status`; urllib.error.HTTPError expõe `.code`
        # (e herda de OSError — por isso o check de 4xx vem ANTES do isinstance).
        status = getattr(current, "status", None)
        if status is None:
            status = getattr(current, "code", None)
        if isinstance(status, int) and 400 <= status < 500:
            return False
        if isinstance(current, (TimeoutError, ConnectionError, OSError)):
            return True
        message = str(current).lower()
        if any(token in message for token in _NETWORK_TOKENS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _clean_album_title(title: str) -> str:
    """Remove o prefixo 'Album - ' e o sufixo '(N Songs)' do título do álbum."""
    cleaned = _ALBUM_TITLE_PREFIX.sub("", title)
    cleaned = _ALBUM_TITLE_SUFFIX.sub("", cleaned)
    return cleaned.strip()


def _normalize_artists(artists: object) -> list[str]:
    """Normaliza o campo `artists` do yt-dlp para lista de strings.

    Aceita lista de strings, lista de dicts com chave `name` ou valor único;
    ausente/inválido → lista vazia.
    """
    if not artists:
        return []
    if isinstance(artists, list):
        names: list[str] = []
        for item in artists:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return names
    return [str(artists)]


def _upgrade_thumbnail(url: str | None) -> str | None:
    """Eleva a resolução da URL da thumbnail para HD (600x600 px)."""
    if not url:
        return None
    upgraded = re.sub(r"=w\d+-h\d+", "=w600-h600", url)
    upgraded = re.sub(r"=s\d+", "=s600", upgraded)
    return upgraded


def _thumbnail_of(entry: dict) -> str | None:
    """Extrai a URL da thumbnail de um entry em alta resolução."""
    thumbnail = entry.get("thumbnail")
    if thumbnail:
        return _upgrade_thumbnail(str(thumbnail))
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        last = thumbnails[-1]
        if isinstance(last, dict) and last.get("url"):
            return _upgrade_thumbnail(str(last["url"]))
    return None


class YouTubeMusicClient:
    """Wrapper fino do yt-dlp para o YouTube Music."""

    def __init__(
        self,
        timeout: int = 25,
        retries: int = 2,
        cache_path: Path | str | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self._opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": timeout,
            "skip_download": True,
            "noplaylist": False,
            # Retry INTERNO do yt-dlp (default 3) reduzido para 1: num 4xx/rate-limit
            # do YouTube o extractor repete a requisição até 3× antes de falhar —
            # multiplica a demora de cada seção de busca. O retry do app (_extract)
            # só dispara em falha de rede real (backoff) e fica intacto.
            "extractor_retries": 1,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }
        # Cache de busca: memória (sempre) + disco SQLite (se `cache_path`). O
        # banco sobrevive a restarts — justo onde a busca é cara (~11–20s).
        self._cache: dict[str, tuple[float, SearchResults | str]] = {}
        self._cache_lock = threading.Lock()
        self._cache_db: sqlite3.Connection | None = None
        if cache_path is not None:
            db_path = Path(cache_path)
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_db = sqlite3.connect(str(db_path), check_same_thread=False)
                self._cache_db.execute(
                    "CREATE TABLE IF NOT EXISTS search_cache "
                    "(key TEXT PRIMARY KEY, expires REAL NOT NULL, data TEXT NOT NULL)"
                )
                self._cache_db.commit()
            except sqlite3.Error:
                self._cache_db = None  # cache em disco é melhor-esforço
        # Pool de threads para resolver títulos de entries flat em paralelo
        # (cada título custa 1-2 requisições; compartilhado entre as seções —
        # o atexit do concurrent.futures encerra os threads no exit do processo).
        self._resolve_pool = ThreadPoolExecutor(max_workers=_RESOLVE_WORKERS)

    def _cache_get(self, key: str) -> SearchResults | None:
        """Devolve o resultado em cache (memória ou disco) ou None se expirado."""
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
            if self._cache_db is not None:
                try:
                    row = self._cache_db.execute(
                        "SELECT expires, data FROM search_cache WHERE key = ?", (key,)
                    ).fetchone()
                except sqlite3.Error:
                    return None
                if row is not None and row[0] > now:
                    result = _results_from_json(row[1])
                    self._cache[key] = (row[0], result)
                    return result
        return None

    def _cache_set(self, key: str, result: SearchResults) -> None:
        """Grava o resultado no cache (memória + disco, quando disponível)."""
        expires = time.time() + _CACHE_TTL
        with self._cache_lock:
            self._cache[key] = (expires, result)
            if self._cache_db is not None:
                try:
                    self._cache_db.execute(
                        "INSERT OR REPLACE INTO search_cache (key, expires, data) "
                        "VALUES (?, ?, ?)",
                        (key, expires, json.dumps(asdict(result), ensure_ascii=False)),
                    )
                    # Pruning leve: expira o que já passou do TTL (evita crescer sem limite).
                    self._cache_db.execute(
                        "DELETE FROM search_cache WHERE expires < ?", (time.time(),)
                    )
                    self._cache_db.commit()
                except sqlite3.Error:
                    pass  # falha no disco não derruba a busca

    def _extract(self, url: str, extract_flat: bool = False) -> dict:
        """Extrai info via yt-dlp com retry em falhas de rede (backoff progressivo)."""
        opts = dict(self._opts)
        opts["extract_flat"] = extract_flat

        last_exc: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as exc:
                last_exc = exc
                if not _is_network_error(exc):
                    break
                if attempt < self.retries:
                    # Backoff progressivo entre tentativas (0.5s, 1s, 2s, ... máx 5s).
                    time.sleep(min(0.5 * 2**attempt, 5.0))
        msg = _strip_ansi(str(last_exc)) if last_exc else "Falha desconhecida"
        if last_exc and _is_network_error(last_exc):
            raise NetworkError(f"Falha de rede ao consultar YouTube Music: {msg}") from last_exc
        raise SearchError(f"Erro ao consultar YouTube Music: {msg}") from last_exc

    def _title_get(self, url: str) -> str | None:
        """Devolve o título já resolvido para `url` (memória ou disco) ou None."""
        key = f"title:{url}"
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now and isinstance(hit[1], str):
                return hit[1]
            if self._cache_db is not None:
                try:
                    row = self._cache_db.execute(
                        "SELECT expires, data FROM search_cache WHERE key = ?", (key,)
                    ).fetchone()
                except sqlite3.Error:
                    return None
                if row is not None and row[0] > now:
                    self._cache[key] = (row[0], row[1])
                    return row[1]
        return None

    def _title_set(self, url: str, title: str) -> None:
        """Grava o título resolvido no cache (memória + disco, quando disponível)."""
        key = f"title:{url}"
        expires = time.time() + _TITLE_TTL
        with self._cache_lock:
            self._cache[key] = (expires, title)
            if self._cache_db is not None:
                try:
                    self._cache_db.execute(
                        "INSERT OR REPLACE INTO search_cache (key, expires, data) "
                        "VALUES (?, ?, ?)",
                        (key, expires, title),
                    )
                    self._cache_db.commit()
                except sqlite3.Error:
                    pass  # falha no disco não derruba a busca

    def _resolve_title(self, url: str) -> str | None:
        """Resolve o título de uma entrada flat (com cache por URL, TTL 7 dias).

        O cache corta as duplicatas: dentro da mesma busca há URLs repetidas
        (ex.: um canal em "artists" e "playlists") e buscas parecidas reusam
        os títulos já extraídos — cada título custa 1-2 requisições ao YouTube.
        """
        cached = self._title_get(url)
        if cached is not None:
            return cached
        current = url
        for _ in range(2):
            info = self._extract(current, extract_flat=True)
            title = info.get("title")
            if title:
                resolved = str(title)
                self._title_set(url, resolved)
                return resolved
            redirect = info.get("url")
            if not redirect or redirect == current:
                return None
            current = redirect
        return None

    def _search_section(self, section: str, query: str, max_results: int) -> list[SearchItem]:
        """Extrai a seção de busca flat e traz títulos, capas e artistas."""
        params = _SEARCH_SECTIONS.get(section, "")
        if not params:
            return []
        url = (
            "https://music.youtube.com/search?q="
            + urllib.parse.quote(query)
            + "&sp="
            + urllib.parse.quote(params)
        )
        info = self._extract(url, extract_flat=True)
        # Passo 1: coleta itens com título direto das entries e marca os que
        # precisam de requisição extra (entries flat não trazem title).
        items: list[SearchItem] = []
        pending: list[tuple[dict, str]] = []
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if len(items) + len(pending) >= max_results:
                break  # seção já tem o máximo — para de varrer o resto
            item_id = entry.get("id")
            if not item_id:
                continue
            entry_url = entry.get("url") or f"https://music.youtube.com/browse/{item_id}"
            if section == "songs":
                entry_url = f"https://music.youtube.com/watch?v={item_id}"

            title = entry.get("title")
            if title:
                items.append(self._build_item(entry, item_id, entry_url, section, str(title)))
            else:
                pending.append((entry, entry_url))

        # Passo 2: resolve os títulos faltantes em paralelo (cada um custa 1-2
        # requisições ao YouTube; em série isso dominava o tempo da busca).
        if pending:
            titles = list(self._resolve_pool.map(lambda p: self._resolve_title(p[1]), pending))
            for (entry, entry_url), title in zip(pending, titles):
                if len(items) >= max_results:
                    break
                if title:
                    items.append(self._build_item(entry, entry.get("id"), entry_url, section, title))
        return items

    def _build_item(self, entry: dict, item_id: object, entry_url: str, section: str, title: str) -> SearchItem:
        """Monta um `SearchItem` a partir da entry flat e do título já resolvido."""
        thumbnail = _thumbnail_of(entry)
        artist = str(entry.get("uploader") or entry.get("channel") or "")
        return SearchItem(
            id=str(item_id),
            title=title,
            kind=_SECTION_KIND[section],
            url=entry_url,
            thumbnail=thumbnail,
            artist=artist or None,
        )

    def search(
        self,
        query: str,
        max_results: int = 6,
        on_section: Callable[[str, list[SearchItem]], None] | None = None,
    ) -> SearchResults:
        """Busca por `query` e retorna músicas, artistas, álbuns e playlists.

        `on_section(kind, items)` — opcional — é chamado conforme cada seção
        termina ("songs", "albums", "artists", "playlists"), permitindo que a
        API /api/search/stream emita as seções incrementalmente (a busca é lenta
        por causa do rate-limit do YouTube). No cache, todas as seções são
        emitidas na hora.

        URLs diretas de vídeo (`watch?v=`/`youtu.be`) resolvem como música
        avulsa; URLs de playlist (`list=PL...`/`VL...`/`OLAK...`) resolvem como
        um item de playlist (abre a lista de faixas). Resultados ficam em cache
        (memória + disco, TTL 600s).
        """
        query_clean = query.strip()
        cache_key = f"{query_clean.lower()}:{max_results}"

        cached = self._cache_get(cache_key)
        if cached is not None:
            if on_section is not None:
                for kind, items in (
                    ("songs", cached.songs),
                    ("albums", cached.albums),
                    ("artists", cached.artists),
                    ("playlists", cached.playlists),
                ):
                    on_section(kind, items)
            return cached

        # 1) URL de música (watch/youtu.be) — precedência sobre playlist.
        yt_id_match = re.search(r"(?:[?&]v=|be/)([a-zA-Z0-9_-]{11})", query_clean)
        if yt_id_match and ("youtube.com" in query_clean or "youtu.be" in query_clean):
            yt_id = yt_id_match.group(1)
            try:
                meta = self.track_metadata(yt_id)
                item = SearchItem(
                    id=yt_id,
                    title=meta.get("title") or yt_id,
                    kind="song",
                    url=f"https://music.youtube.com/watch?v={yt_id}",
                    thumbnail=meta.get("thumbnail"),
                    artist=", ".join(meta.get("artists") or []) if meta.get("artists") else None,
                )
                res = SearchResults(artists=[], albums=[], songs=[item])
                if on_section is not None:
                    on_section("songs", [item])
                self._cache_set(cache_key, res)
                return res
            except Exception:
                pass  # cai para a busca por seções

        # 2) URL de playlist direta: resolve as faixas e devolve um item.
        playlist_match = re.search(r"[?&]list=([A-Za-z0-9_-]{13,})", query_clean)
        if playlist_match and ("youtube.com" in query_clean or "youtu.be" in query_clean):
            try:
                album = self.album_tracks(playlist_match.group(1))
                item = SearchItem(
                    id=playlist_match.group(1),
                    title=album.title,
                    kind="playlist",
                    url=f"https://music.youtube.com/playlist?list={playlist_match.group(1)}",
                    thumbnail=album.cover_url,
                    artist=album.artist,
                )
                res = SearchResults(artists=[], albums=[], songs=[], playlists=[item])
                if on_section is not None:
                    on_section("playlists", [item])
                self._cache_set(cache_key, res)
                return res
            except Exception:
                pass  # cai para a busca por seções

        # Seções em série, "songs" primeiro. Medido com o YouTube real: as 4
        # seções em paralelo derrubaram o tempo da PRIMEIRA resposta (songs
        # passou de ~35s para ~184s) porque ~20 extrações simultâneas acionam
        # throttling do YouTube; em série, songs chega em ~35s e o SSE já
        # renderiza os primeiros resultados. Dentro de cada seção, os títulos
        # flat são resolvidos em paralelo (_resolve_pool) e o cache por URL
        # (_title_get/_title_set) corta as duplicatas entre seções/buscas.
        results_by_kind: dict[str, list[SearchItem]] = {}
        for kind in _SECTION_ORDER:
            items = self._search_section(kind, query_clean, max_results)
            results_by_kind[kind] = items
            if on_section is not None:
                on_section(kind, items)
        songs = results_by_kind["songs"]
        albums = results_by_kind["albums"]
        artists = results_by_kind["artists"]
        playlists = results_by_kind["playlists"]
        if not songs and not albums and not artists and not playlists:
            raise NotFoundError(f"Nenhum resultado encontrado para a busca \"{query_clean}\".")

        res = SearchResults(artists=artists, albums=albums, songs=songs, playlists=playlists)
        self._cache_set(cache_key, res)
        return res

    def artist_albums(self, artist_name: str) -> list[SearchItem]:
        """Retorna os álbuns de um artista (busca pelo nome — páginas de artista não suportadas)."""
        results = self.search(artist_name)
        return results.albums

    def album_tracks(self, browse_id: str) -> Album:
        """Resolve um álbum (browse MPRE... ou playlist OLAK...) em `Album` com faixas numeradas.

        `number` = posição na playlist (o yt-dlp 2026.07.04 não fornece `track_number`).
        `year` = None (indisponível). Browse MPRE pode devolver um redirect (stub) para a
        playlist OLAK — o redirect é seguido. Sem faixas → `NotFoundError`.
        """
        # OLAK = álbum, PL/VL = playlists do usuário, RDAM/RDCLAK = mixes.
        if browse_id.startswith(("OLAK", "PL", "VL", "RDAM", "RDCLAK")):
            url = f"https://music.youtube.com/playlist?list={browse_id}"
        else:
            url = f"https://music.youtube.com/browse/{browse_id}"
        info = self._extract(url, extract_flat=True)
        if not info.get("entries"):
            redirect = info.get("url")
            if redirect and redirect != url:
                info = self._extract(redirect, extract_flat=True)
        entries = info.get("entries") or []  # nunca None
        title = _clean_album_title(str(info.get("title") or "")) or "Desconhecido"
        artist = str(info.get("channel") or info.get("uploader") or "")
        cover_url = info.get("thumbnail") or None
        tracks: list[Track] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            yt_id = entry.get("id")
            entry_title = entry.get("title")
            if not yt_id or not entry_title:
                continue
            if not artist and entry.get("channel"):
                artist = str(entry["channel"])
            tracks.append(
                Track(
                    yt_id=str(yt_id),
                    title=str(entry_title),
                    number=idx + 1,  # posição na playlist
                    duration=entry.get("duration"),
                    cover_url=_thumbnail_of(entry),
                )
            )
        if not tracks:
            raise NotFoundError(f"Nenhuma faixa encontrada para o álbum {browse_id}.")
        if not cover_url and tracks[0].cover_url:
            cover_url = tracks[0].cover_url
        return Album(
            id=browse_id,
            title=title,
            artist=artist or "Desconhecido",
            year=None,
            cover_url=cover_url,
            tracks=tracks,
        )

    def track_metadata(self, yt_id: str) -> dict:
        """Extrai metadados completos de uma faixa (extração completa — fluxo de download).

        Retorna dict com chaves `yt_id`, `title`, `artists` (list[str]), `album`,
        `track`, `release_year`, `thumbnail`, `duration`, `webpage_url`
        (chaves ausentes = None). Consumido pelo downloader para nome de arquivo e tags.
        """
        url = f"https://music.youtube.com/watch?v={yt_id}"
        info = self._extract(url, extract_flat=False)
        return {
            "yt_id": yt_id,
            "title": info.get("title"),
            "artists": _normalize_artists(info.get("artists")),
            "album": info.get("album"),
            "track": info.get("track"),
            "release_year": info.get("release_year"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "webpage_url": info.get("webpage_url") or url,
        }
