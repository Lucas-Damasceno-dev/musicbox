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

import re
import urllib.error
import urllib.parse
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
}

# kind canônico do SearchItem por seção (contrato em models.py: "artist" | "album").
_SECTION_KIND = {
    "albums": "album",
    "artists": "artist",
}

# Título de álbum do YT Music vem "mangled": prefixo "Album - " e sufixo "(N Songs)".
_ALBUM_TITLE_PREFIX = re.compile(r"^\s*Album\s*-\s*")
_ALBUM_TITLE_SUFFIX = re.compile(r"\s*\(\d+ Songs?\)\s*$")

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


def _strip_ansi(text: str) -> str:
    """Remove códigos de escape ANSI de uma string (cores, cursor e limpeza)."""
    return _ANSI_ESCAPE.sub("", text)


def _is_network_error(exc: BaseException) -> bool:
    """True se a exceção (ou sua cadeia de causas/mensagem) indica falha de rede ou timeout."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
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


def _thumbnail_of(entry: dict) -> str | None:
    """Extrai a URL da thumbnail de um entry (chave `thumbnail` ou último de `thumbnails`)."""
    thumbnail = entry.get("thumbnail")
    if thumbnail:
        return str(thumbnail)
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        last = thumbnails[-1]
        if isinstance(last, dict) and last.get("url"):
            return str(last["url"])
    return None


class YouTubeMusicClient:
    """Wrapper fino do yt-dlp para o YouTube Music.

    Cada `extract_info` passa por retry em falhas de rede (`retries + 1` tentativas);
    exceções de rede viram `NetworkError` e falhas não-rede viram `SearchError`.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 2,
        cookies_file: Path | None = None,
        cookies_from_browser: str | None = None,
    ) -> None:
        """Inicializa o client com timeout de socket e número de retries.

        Cookies opcionais (bloqueio "Sign in to confirm you're not a bot" do
        YouTube): `cookies_file` (arquivo `cookies.txt` Netscape) ou
        `cookies_from_browser` (nome do navegador). Se ambos forem informados,
        `cookiefile` VENCE — o yt-dlp não aceita os dois simultaneamente.
        """
        self.timeout = timeout
        self.retries = retries
        self._opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": timeout,
            "skip_download": True,
            "noplaylist": False,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }
        if cookies_file is not None:
            self._opts["cookiefile"] = str(cookies_file)
        elif cookies_from_browser is not None:
            self._opts["cookiesfrombrowser"] = (cookies_from_browser,)

    def _extract(self, url: str, extract_flat: bool = False) -> dict:
        """Extrai info via yt-dlp com retry em falhas de rede.

        Falha de rede após `retries + 1` tentativas → `NetworkError`.
        Falha não-rede (vídeo removido/DMCA etc.) → `SearchError`.
        """
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            opts = dict(self._opts)
            # Cookies NÃO são usados no YouTube: a sessão logada é flagada pelo
            # YouTube (player response sem streamingData → "Requested format is not
            # available") e o client "android" não suporta cookies.
            opts.pop("cookiefile", None)
            opts.pop("cookiesfrombrowser", None)
            if extract_flat:
                opts["extract_flat"] = True
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                return info or {}
            except Exception as exc:  # noqa: BLE001 — yt-dlp lança exceções variadas
                if not _is_network_error(exc):
                    raise SearchError(
                        "Falha ao obter dados do YouTube Music: "
                        f"{_strip_ansi(str(exc))}"
                    ) from exc
                last_error = exc
        raise NetworkError(
            "Falha de rede ao acessar o YouTube Music após "
            f"{self.retries + 1} tentativas: {_strip_ansi(str(last_error))}"
        )

    def _resolve_title(self, url: str) -> str | None:
        """Resolve o título de uma entrada flat, seguindo redirects (máx. 2 hops).

        Browse MPRE do álbum devolve um stub (`_type: url`) apontando para a playlist
        OLAK — um segundo request flat na URL de destino traz o título.
        """
        current = url
        for _ in range(2):
            info = self._extract(current, extract_flat=True)
            title = info.get("title")
            if title:
                return str(title)
            redirect = info.get("url")
            if not redirect or redirect == current:
                return None
            current = redirect
        return None

    def _search_section(self, section: str, query: str, max_results: int) -> list[SearchItem]:
        """Extrai a seção de busca (albums/artists) flat e resolve títulos dos primeiros itens.

        Resolução de títulos é SEQUENCIAL (fix round 1 testou ThreadPoolExecutor com
        teto de 8 workers, mas o YouTube Music throttla requisições concorrentes do mesmo
        IP: 3 itens levavam 21.4s em paralelo vs 9.4s sequenciais — ver task-3-report.md,
        "Fix round 1"). Sequencial é mais rápido e não depende de estado global do yt-dlp.
        """
        params = _SEARCH_SECTIONS[section]
        url = (
            "https://music.youtube.com/search?q="
            + urllib.parse.quote(query)
            + "&sp="
            + urllib.parse.quote(params)
        )
        info = self._extract(url, extract_flat=True)
        items: list[SearchItem] = []
        for entry in info.get("entries") or []:  # nunca None
            if not isinstance(entry, dict) or len(items) >= max_results:
                continue
            item_id = entry.get("id")
            if not item_id:
                continue
            entry_url = entry.get("url") or f"https://music.youtube.com/browse/{item_id}"
            title = self._resolve_title(entry_url)
            if not title:
                continue  # item sem título resolvível não ajuda a UI
            items.append(SearchItem(id=str(item_id), title=title, kind=_SECTION_KIND[section], url=entry_url))
        return items

    def search(self, query: str, max_results: int = 6) -> SearchResults:
        """Busca por `query` e retorna artistas e álbuns com títulos.

        Custo: 1 request por seção (álbuns/artistas) + 1-2 requests por item para o
        título (entries flat não trazem título no yt-dlp 2026.07.04; resolução sequencial
        — paralelismo se mostrou mais lento por throttling do YouTube, ver "Fix round 1").
        Itens sem título resolvível são omitidos. Sem resultados → `NotFoundError`.
        """
        albums = self._search_section("albums", query, max_results)
        artists = self._search_section("artists", query, max_results)
        if not albums and not artists:
            raise NotFoundError(f"Nenhum resultado encontrado para a busca \"{query}\".")
        return SearchResults(artists=artists, albums=albums)

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
        if browse_id.startswith("OLAK"):
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
