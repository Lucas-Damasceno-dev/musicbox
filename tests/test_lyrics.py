"""Testes do app/lyrics.py: `fetch_lrc` com urllib mockado (sem rede)."""

import json
import urllib.error
import urllib.request

from app.lyrics import _TIMEOUT, LRCLIB_SEARCH_URL, fetch_lrc


class FakeResponse:
    """Context manager com `.status`/`.read()` (mock do retorno do urlopen)."""

    def __init__(self, status: int = 200, payload: object | None = None) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b"[]"

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_fake(captured: dict, status: int = 200, payload: object | None = None):
    """Fake de `urllib.request.urlopen` que captura url/timeout e devolve/levanta."""

    def fake(url, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        raise_on = captured.pop("raise", None)
        if raise_on is not None:
            raise raise_on
        return FakeResponse(status, payload)

    return fake


# ------------------------------------------------------------------ hits


def test_fetch_lrc_synced_lyrics(monkeypatch):
    # 1º hit com syncedLyrics → devolve o LRC com timestamps (prioridade).
    captured: dict = {}
    payload = [
        {"trackName": "A", "artistName": "X", "plainLyrics": "sem tempo"},
        {"trackName": "A", "artistName": "X", "syncedLyrics": "[00:01.00]Oi"},
    ]
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, payload=payload))
    assert fetch_lrc("Artista", "Faixa") == "[00:01.00]Oi"


def test_fetch_lrc_plain_lyrics(monkeypatch):
    # Só plainLyrics (sem synced) → devolve o texto como LRC estático.
    captured: dict = {}
    payload = [{"trackName": "A", "artistName": "X", "plainLyrics": "letra sem tempo"}]
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, payload=payload))
    assert fetch_lrc("Artista", "Faixa") == "letra sem tempo"


def test_fetch_lrc_lista_vazia_none(monkeypatch):
    # Sem hits → None (sem letra).
    captured: dict = {}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, payload=[]))
    assert fetch_lrc("Artista", "Faixa") is None


def test_fetch_lrc_http_500_none(monkeypatch):
    # HTTP != 200 → None (nunca levanta).
    captured: dict = {}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, status=500, payload=[]))
    assert fetch_lrc("Artista", "Faixa") is None


def test_fetch_lrc_urlerror_none(monkeypatch):
    # Erro de rede/timeout → None (nunca levanta).
    captured: dict = {}
    captured["raise"] = urllib.error.URLError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, payload=[]))
    assert fetch_lrc("Artista", "Faixa") is None


# ------------------------------------------------------------------ URL


def test_fetch_lrc_monta_url_codificada(monkeypatch):
    # Query montada com urlencode: espaços → +, parênteses/ vírgulas → %XX,
    # album_name presente quando informado, timeout = 5s.
    captured: dict = {}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, payload=[]))
    fetch_lrc(
        "Daft Punk",
        "Harder, Better, Faster, Stronger (feat. X)",
        "Homework",
    )
    assert captured["url"].startswith(LRCLIB_SEARCH_URL + "?")
    assert "artist_name=Daft+Punk" in captured["url"]
    assert "track_name=Harder%2C+Better%2C+Faster%2C+Stronger+%28feat.+X%29" in captured["url"]
    assert "album_name=Homework" in captured["url"]
    assert captured["timeout"] == _TIMEOUT


def test_fetch_lrc_sem_album_omite_album_name(monkeypatch):
    # album=None → album_name ausente na query.
    captured: dict = {}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_fake(captured, payload=[]))
    fetch_lrc("Daft Punk", "One More Time")
    assert "album_name" not in captured["url"]
    assert "artist_name=Daft+Punk" in captured["url"]
    assert "track_name=One+More+Time" in captured["url"]
