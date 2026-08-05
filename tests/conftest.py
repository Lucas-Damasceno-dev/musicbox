"""Fixtures compartilhadas da suíte de testes do MusicBox.

IMPORTANTE: `MUSICBOX_DIR` é definido ANTES de importar `app.main` — o módulo
constrói o `app` default no import (History em musicbox_dir/history.db).

Nenhum teste toca a rede: yt-dlp é mockado e a integração usa `stub_client`
+ `fake_executor`. Comentários em português; identificadores em inglês.
"""

import os
import tempfile
from pathlib import Path

os.environ["MUSICBOX_DIR"] = tempfile.mkdtemp(prefix="musicbox-tests-")

import pytest

from app.config import Settings
from app.downloader import Downloader
from app.history import History
from app.main import create_app
from app.models import Album, SearchItem, SearchResults, Track


class StubClient:
    """Fake determinístico do YouTubeMusicClient (sem rede).

    Levanta `search_error`/`album_error`/`metadata_error` quando o atributo é
    setado (para testar os mapeamentos 404/503/502).
    """

    def __init__(self) -> None:
        self.search_error: Exception | None = None
        self.album_error: Exception | None = None
        self.metadata_error: Exception | None = None

    def search(self, query: str, max_results: int = 6) -> SearchResults:
        if self.search_error is not None:
            raise self.search_error
        return SearchResults(
            artists=[
                SearchItem(
                    id="UCstub",
                    title="Artista Stub",
                    kind="artist",
                    url="https://music.youtube.com/channel/UCstub",
                )
            ],
            albums=[
                SearchItem(
                    id="MPREstub",
                    title="Album Stub",
                    kind="album",
                    url="https://music.youtube.com/browse/MPREstub",
                )
            ],
        )

    def artist_albums(self, artist_name: str) -> list[SearchItem]:
        if self.search_error is not None:
            raise self.search_error
        return [
            SearchItem(
                id="MPREstub",
                title="Album Stub",
                kind="album",
                url="https://music.youtube.com/browse/MPREstub",
            )
        ]

    def album_tracks(self, browse_id: str) -> Album:
        if self.album_error is not None:
            raise self.album_error
        return Album(
            id=browse_id,
            title="Album Stub",
            artist="Artista Stub",
            year=None,
            cover_url=None,
            tracks=[
                Track(yt_id="yt1", title="Faixa 1", number=1, duration=181),
                Track(yt_id="yt2", title="Faixa 2", number=2, duration=182),
                Track(yt_id="yt3", title="Faixa 3", number=3, duration=183),
            ],
        )

    def track_metadata(self, yt_id: str) -> dict:
        if self.metadata_error is not None:
            raise self.metadata_error
        return {
            "yt_id": yt_id,
            "title": f"Titulo {yt_id}",
            "artists": ["Artista Stub"],
            "album": "Album Stub",
            "track": f"Faixa {yt_id}",
            "release_year": 2024,
            "thumbnail": None,
            "duration": 181,
            "webpage_url": f"https://music.youtube.com/watch?v={yt_id}",
        }


class FakeExecutor:
    """Executor fake síncrono: cria `audio.<ext>` em temp_dir e retorna o Path.

    `fail = True` faz `__call__` levantar RuntimeError (caminho de erro).
    """

    def __init__(self) -> None:
        self.fail = False

    def __call__(self, yt_id, fmt, temp_dir, dest_dir, dest_filename_stem, metadata) -> Path:
        if self.fail:
            raise RuntimeError("executor falhou")
        ext = "mp3" if fmt == "mp3" else "opus"
        audio = Path(temp_dir) / f"audio.{ext}"
        audio.write_bytes(b"fake audio")
        return audio


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(musicbox_dir=tmp_path / "music", workers=2)


@pytest.fixture
def history(tmp_path) -> History:
    return History(tmp_path / "history.db")


@pytest.fixture
def stub_client() -> StubClient:
    return StubClient()


@pytest.fixture
def fake_executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def downloader(settings, history, stub_client, fake_executor) -> Downloader:
    return Downloader(settings, history, stub_client, executor=fake_executor)


@pytest.fixture
def app(settings, stub_client, downloader, history):
    return create_app(settings, stub_client, downloader, history)


@pytest.fixture
def client(app):
    """TestClient com lifespan ativo (startup/shutdown do downloader por teste)."""
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def wait_for():
    """Retorna um poller com timeout (sem `sleep` longos)."""

    def _wait(cond, timeout=5.0, interval=0.02, msg="condição não satisfeita a tempo"):
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(interval)
        raise AssertionError(msg)

    return _wait
