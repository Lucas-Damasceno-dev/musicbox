"""Testes do app/ytdlp_client.py com yt-dlp MOCKADO (sem rede).

Substitui `yt_dlp.YoutubeDL` por um fake que captura as opts (verifica
`extract_flat=True` exigido pelo spec) e devolve info/exceções por URL.
"""

import urllib.error
import urllib.parse

import pytest
from yt_dlp.utils import DownloadError

import app.ytdlp_client as ytdlp_module
from app.ytdlp_client import (
    _SEARCH_SECTIONS,
    NetworkError,
    NotFoundError,
    SearchError,
    YouTubeMusicClient,
)


@pytest.fixture
def fake_ydl(monkeypatch):
    """Fake do YoutubeDL: captura opts/urls e devolve info ou exceção por URL."""
    state = {
        "opts": [],
        "urls": [],
        "infos": {},  # url exata → info
        "default_info": {},
        "default_exception": None,
    }

    class FakeYDL:
        def __init__(self, opts=None):
            state["opts"].append(opts)
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            state["urls"].append(url)
            if url in state["infos"]:
                return state["infos"][url]
            if state["default_exception"] is not None:
                raise state["default_exception"]
            return state["default_info"]

    monkeypatch.setattr(ytdlp_module.yt_dlp, "YoutubeDL", FakeYDL)
    return state


def _section_url(query: str, section: str) -> str:
    """Monta a URL de seção igual ao `_search_section` do client (para o fake)."""
    return (
        "https://music.youtube.com/search?q="
        + urllib.parse.quote(query)
        + "&sp="
        + urllib.parse.quote(_SEARCH_SECTIONS[section])
    )


def test_search_passa_extract_flat_e_classifica(fake_ydl):
    fake_ydl["infos"][_section_url("queen", "albums")] = {
        "title": "Resultado",
        "entries": [{"id": "MPRE1", "url": "https://music.youtube.com/browse/MPRE1"}],
    }
    fake_ydl["infos"][_section_url("queen", "artists")] = {
        "title": "Resultado",
        "entries": [{"id": "UC1", "url": "https://music.youtube.com/channel/UC1"}],
    }
    fake_ydl["default_info"] = {"title": "Titulo Resolvido"}  # usado no _resolve_title

    results = YouTubeMusicClient().search("queen")

    assert len(results.albums) == 1 and results.albums[0].id == "MPRE1"
    assert len(results.artists) == 1 and results.artists[0].id == "UC1"
    assert results.albums[0].kind == "album"
    assert results.artists[0].kind == "artist"
    assert results.albums[0].title == "Titulo Resolvido"
    # TODAS as extrações (seções + resolução de títulos) usam extract_flat=True.
    assert fake_ydl["opts"]
    assert all(opts.get("extract_flat") is True for opts in fake_ydl["opts"])


def test_search_sem_resultados_not_found(fake_ydl):
    fake_ydl["default_info"] = {"entries": []}
    with pytest.raises(NotFoundError):
        YouTubeMusicClient().search("zzzz")


def test_extract_network_error_apos_retries(fake_ydl):
    fake_ydl["default_exception"] = urllib.error.URLError("network unreachable")
    client = YouTubeMusicClient(timeout=10, retries=2)
    with pytest.raises(NetworkError):
        client._extract("https://music.youtube.com/watch?v=abc123def45")
    # retries + 1 = 3 tentativas (backoff entre elas).
    assert len(fake_ydl["urls"]) == 3


def test_extract_erro_nao_rede_nao_retenta(fake_ydl):
    fake_ydl["default_exception"] = DownloadError("ERROR: video unavailable")
    client = YouTubeMusicClient(retries=2)
    with pytest.raises(SearchError):
        client._extract("https://music.youtube.com/watch?v=abc123def45")
    assert len(fake_ydl["urls"]) == 1  # falha não-rede: sem retry


def test_album_tracks_limpa_titulo_e_numera(fake_ydl):
    fake_ydl["infos"]["https://music.youtube.com/browse/MPRE1"] = {
        "title": "Album - Greatest Hits (3 Songs)",
        "channel": "Queen",
        "entries": [
            {"id": "t1", "title": "Bohemian Rhapsody", "duration": 356},
            {"id": "t2", "title": "Another One Bites", "duration": 216},
            {"id": "t3", "title": "Killer Queen", "duration": 180},
        ],
    }
    album = YouTubeMusicClient().album_tracks("MPRE1")
    assert album.title == "Greatest Hits"  # mangled "Album - X (3 Songs)" → "X"
    assert album.artist == "Queen"
    assert album.year is None
    assert [t.number for t in album.tracks] == [1, 2, 3]  # posição na playlist
    assert album.tracks[0].yt_id == "t1"
    assert album.tracks[0].duration == 356
    assert all(opts.get("extract_flat") is True for opts in fake_ydl["opts"])


def test_album_tracks_follow_redirect(fake_ydl):
    browse_url = "https://music.youtube.com/browse/MPREredir"
    playlist_url = "https://music.youtube.com/playlist?list=OLAK5uy_abc"
    fake_ydl["infos"][browse_url] = {"url": playlist_url, "title": ""}  # stub redirect
    fake_ydl["infos"][playlist_url] = {
        "title": "Album X",
        "entries": [{"id": "t1", "title": "Faixa 1"}],
    }
    album = YouTubeMusicClient().album_tracks("MPREredir")
    assert album.title == "Album X"
    assert len(album.tracks) == 1
    assert fake_ydl["urls"] == [browse_url, playlist_url]


def test_album_tracks_sem_faixas_not_found(fake_ydl):
    fake_ydl["default_info"] = {"title": "Vazio", "entries": []}
    with pytest.raises(NotFoundError):
        YouTubeMusicClient().album_tracks("MPREvazio")


def test_track_metadata_artists_lista_de_dicts(fake_ydl):
    fake_ydl["default_info"] = {"title": "T", "artists": [{"name": "A1"}, "A2"]}
    md = YouTubeMusicClient().track_metadata("yt9")
    assert md["artists"] == ["A1", "A2"]


def test_track_metadata_artists_scalar(fake_ydl):
    fake_ydl["default_info"] = {"artists": "Solo Artist"}
    assert YouTubeMusicClient().track_metadata("yt9")["artists"] == ["Solo Artist"]


def test_track_metadata_chaves_ausentes_none(fake_ydl):
    fake_ydl["default_info"] = {}
    md = YouTubeMusicClient().track_metadata("yt9")
    assert md["title"] is None
    assert md["artists"] == []
    assert md["album"] is None
    assert md["track"] is None
    assert md["release_year"] is None
    assert md["duration"] is None
    assert md["webpage_url"] == "https://music.youtube.com/watch?v=yt9"


def test_opts_sem_cookies_por_padrao():
    opts = YouTubeMusicClient()._opts
    assert "cookiefile" not in opts
    assert "cookiesfrombrowser" not in opts


def test_extract_remove_ansi_do_search_error(fake_ydl):
    fake_ydl["default_exception"] = DownloadError(
        "\x1b[0;31mERROR:\x1b[0m [youtube] abc: "
        "Sign in to confirm you're not a bot. Use --cookies-from-browser or "
        "--cookies for the authentication."
    )
    with pytest.raises(SearchError) as excinfo:
        YouTubeMusicClient().search("queen")
    message = str(excinfo.value)
    assert "\x1b[" not in message
    assert "Sign in to confirm" in message


def test_extract_remove_ansi_do_network_error(fake_ydl):
    fake_ydl["default_exception"] = urllib.error.URLError(
        "\x1b[0;31mERROR:\x1b[0m [youtube] abc: connection timed out"
    )
    with pytest.raises(NetworkError) as excinfo:
        YouTubeMusicClient(timeout=10, retries=1).search("queen")
    message = str(excinfo.value)
    assert "\x1b[" not in message
    assert "connection timed out" in message


# ------------------------------------------------- busca por seções (SSE)


def test_search_on_section_emite_cada_secao(fake_ydl):
    # A busca por streaming chama on_section(kind, items) conforme cada seção
    # resolve — músicas, álbuns, artistas e playlists (vazias viram []).
    fake_ydl["infos"][_section_url("queen", "songs")] = {
        "title": "R",
        "entries": [{"id": "vid1", "title": "Song 1"}],
    }
    fake_ydl["infos"][_section_url("queen", "albums")] = {
        "title": "R",
        "entries": [
            {"id": "MPRE1", "url": "https://music.youtube.com/browse/MPRE1", "title": "Album X"}
        ],
    }
    fake_ydl["default_info"] = {"title": "R", "entries": []}

    calls: list[tuple[str, list]] = []
    YouTubeMusicClient().search("queen", on_section=lambda kind, items: calls.append((kind, items)))

    assert [k for k, _ in calls] == ["songs", "albums", "artists", "playlists"]
    assert calls[0][1][0].id == "vid1"
    assert calls[1][1][0].id == "MPRE1"
    assert calls[2][1] == [] and calls[3][1] == []


def test_search_on_section_cache_hit_emite_tudo(fake_ydl):
    # Cache quente: on_section entrega todas as seções na hora (sem reextrair).
    fake_ydl["infos"][_section_url("queen", "albums")] = {
        "title": "R",
        "entries": [
            {"id": "MPRE1", "url": "https://music.youtube.com/browse/MPRE1", "title": "Album X"}
        ],
    }
    fake_ydl["default_info"] = {"title": "R", "entries": []}
    client = YouTubeMusicClient()
    client.search("queen")  # popula o cache (sem callback)

    calls: list[tuple[str, list]] = []
    client.search("queen", on_section=lambda kind, items: calls.append((kind, items)))

    assert [k for k, _ in calls] == ["songs", "albums", "artists", "playlists"]
    assert calls[1][1][0].id == "MPRE1"


# ---------------------------------------------------------------- playlists


def test_search_url_playlist_vira_item_playlist(fake_ydl):
    playlist_url = "https://music.youtube.com/playlist?list=PLabc123def456ghi"
    fake_ydl["infos"][playlist_url] = {
        "title": "Minha Playlist",
        "channel": "Criador",
        "entries": [{"id": "t1", "title": "Faixa 1"}, {"id": "t2", "title": "Faixa 2"}],
    }
    results = YouTubeMusicClient().search(playlist_url)
    assert len(results.playlists) == 1
    item = results.playlists[0]
    assert item.id == "PLabc123def456ghi"
    assert item.kind == "playlist"
    assert item.title == "Minha Playlist"
    assert item.artist == "Criador"
    assert results.songs == [] and results.albums == [] and results.artists == []


def test_search_url_watch_ganha_de_playlist(fake_ydl):
    # URL de música com &list= → música avulsa (não vira playlist).
    fake_ydl["default_info"] = {"title": "T", "artists": ["A"]}
    url = "https://music.youtube.com/watch?v=abcdefghijk&list=PLabc123def456ghi"
    results = YouTubeMusicClient().search(url)
    assert len(results.songs) == 1 and results.songs[0].kind == "song"


def test_album_tracks_playlist_usa_url_playlist(fake_ydl):
    playlist_url = "https://music.youtube.com/playlist?list=PLabc"
    fake_ydl["infos"][playlist_url] = {
        "title": "Playlist X (3 songs)",
        "entries": [
            {"id": "t1", "title": "A"},
            {"id": "t2", "title": "B"},
            {"id": "t3", "title": "C"},
        ],
    }
    album = YouTubeMusicClient().album_tracks("PLabc")
    assert album.title == "Playlist X"  # sufixo "(3 songs)" removido (case-insensitive)
    assert len(album.tracks) == 3
    assert fake_ydl["urls"] == [playlist_url]


# ------------------------------------------------------------- cache em disco


def test_cache_disco_persiste_entre_instancias(tmp_path, fake_ydl):
    fake_ydl["infos"][_section_url("queen", "albums")] = {
        "title": "R",
        "entries": [
            {
                "id": "MPRE1",
                "url": "https://music.youtube.com/browse/MPRE1",
                "title": "Album X",
            }
        ],
    }
    fake_ydl["infos"][_section_url("queen", "artists")] = {"title": "R", "entries": []}
    cache_path = tmp_path / "cache" / "search_cache.db"

    client = YouTubeMusicClient(cache_path=cache_path)
    r1 = client.search("queen")
    assert len(r1.albums) == 1 and r1.albums[0].title == "Album X"
    assert cache_path.exists()

    # Nova instância (mesmo disco): hit no cache, nenhuma extração nova.
    client2 = YouTubeMusicClient(cache_path=cache_path)
    r2 = client2.search("queen")
    assert len(r2.albums) == 1 and r2.albums[0].title == "Album X"
    calls = len(fake_ydl["urls"])
    client2.search("queen")
    assert len(fake_ydl["urls"]) == calls  # nada de novo no yt-dlp


# ------------------------------------- limites (_search_section / _resolve_title)


def test_search_section_respeita_max_results(fake_ydl):
    # Entries além de `max_results` NÃO são expandidos (título flat custa rede).
    fake_ydl["infos"][_section_url("queen", "albums")] = {
        "title": "R",
        "entries": [
            {
                "id": f"MPRE{i}",
                "url": f"https://music.youtube.com/browse/MPRE{i}",
                "title": f"Album {i}",
            }
            for i in range(5)
        ],
    }
    items = YouTubeMusicClient()._search_section("albums", "queen", max_results=2)
    assert [i.id for i in items] == ["MPRE0", "MPRE1"]
    assert len(fake_ydl["urls"]) == 1  # 1 extração só (sem resolução extra)


def test_resolve_title_limite_2_hops(fake_ydl):
    # Cadeia de redirects sem título: para após 2 extrações (não segue o 3º).
    u1 = "https://music.youtube.com/browse/MPRE1"
    u2 = "https://music.youtube.com/playlist?list=OLAK1"
    u3 = "https://music.youtube.com/playlist?list=OLAK2"
    fake_ydl["infos"][u1] = {"url": u2, "title": ""}
    fake_ydl["infos"][u2] = {"url": u3, "title": ""}
    fake_ydl["infos"][u3] = {"title": "Nunca Alcançado"}

    assert YouTubeMusicClient()._resolve_title(u1) is None
    assert fake_ydl["urls"] == [u1, u2]


def test_resolve_title_aceita_no_primeiro_hop(fake_ydl):
    # Redirect com título na 2ª extração: resolve e não faz 3ª chamada.
    u1 = "https://music.youtube.com/browse/MPRE1"
    u2 = "https://music.youtube.com/playlist?list=OLAK1"
    fake_ydl["infos"][u1] = {"url": u2, "title": ""}
    fake_ydl["infos"][u2] = {"title": "Titulo Resolvido"}

    assert YouTubeMusicClient()._resolve_title(u1) == "Titulo Resolvido"
    assert fake_ydl["urls"] == [u1, u2]


def test_resolve_title_cacheia_por_url(fake_ydl):
    # 2ª chamada para a mesma URL usa o cache (memória) — sem extração nova.
    u1 = "https://music.youtube.com/browse/MPRE1"
    fake_ydl["infos"][u1] = {"title": "Titulo Cacheado"}

    client = YouTubeMusicClient()
    assert client._resolve_title(u1) == "Titulo Cacheado"
    assert client._resolve_title(u1) == "Titulo Cacheado"
    assert fake_ydl["urls"] == [u1]  # 1 extração só


def test_resolve_title_cache_disco_persiste(tmp_path, fake_ydl):
    # Cache em disco sobrevive à instância: nova instância não extrai de novo.
    u1 = "https://music.youtube.com/browse/MPRE1"
    fake_ydl["infos"][u1] = {"title": "Titulo Persistente"}
    cache_path = tmp_path / "titles.db"

    c1 = YouTubeMusicClient(cache_path=cache_path)
    assert c1._resolve_title(u1) == "Titulo Persistente"
    calls = len(fake_ydl["urls"])

    c2 = YouTubeMusicClient(cache_path=cache_path)
    assert c2._resolve_title(u1) == "Titulo Persistente"
    assert len(fake_ydl["urls"]) == calls  # nenhuma extração nova
