"""Testes de integração do app/main.py (TestClient + fixtures mockadas, sem rede)."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.downloader import Downloader
from app.main import create_app
from app.ytdlp_client import NetworkError, NotFoundError, SearchError


def test_index_sem_frontend(client):
    # index.html chega na T7 → 503 (ou 200 se um dia existir).
    assert client.get("/").status_code in (200, 503)


def test_search_ok(client):
    resp = client.get("/api/search", params={"q": "queen"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["artists"]) == 1 and len(body["albums"]) == 1
    assert body["artists"][0]["kind"] == "artist"
    assert body["albums"][0]["kind"] == "album"


def test_search_q_vazio_422(client):
    assert client.get("/api/search", params={"q": "  "}).status_code == 422
    assert client.get("/api/search").status_code == 422  # q ausente


def test_search_not_found_404(client, stub_client):
    stub_client.search_error = NotFoundError("nada encontrado")
    assert client.get("/api/search", params={"q": "zzz"}).status_code == 404


def test_search_network_error_503(client, stub_client):
    stub_client.search_error = NetworkError("rede caiu")
    assert client.get("/api/search", params={"q": "zzz"}).status_code == 503


def test_search_search_error_502(client, stub_client):
    # SearchError genérico (não NotFound/Network) → 502 com detail do motivo.
    stub_client.search_error = SearchError("falha genérica do YouTube Music")
    resp = client.get("/api/search", params={"q": "zzz"})
    assert resp.status_code == 502
    assert "falha genérica" in resp.json()["detail"]


def test_artist_albums_ok(client):
    resp = client.get("/api/artists/Artista Stub/albums")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list) and len(items) == 1
    assert items[0]["id"] == "MPREstub"
    assert items[0]["kind"] == "album"


def test_artist_albums_not_found_404(client, stub_client):
    stub_client.search_error = NotFoundError("nenhum álbum")
    assert client.get("/api/artists/Inexistente/albums").status_code == 404


def test_album_tracks_ok(client):
    resp = client.get("/api/albums/MPREstub/tracks")
    assert resp.status_code == 200
    album = resp.json()
    assert album["title"] == "Album Stub"
    assert len(album["tracks"]) == 3


def test_album_tracks_not_found_404(client, stub_client):
    stub_client.album_error = NotFoundError("sem álbum")
    assert client.get("/api/albums/MPREx/tracks").status_code == 404


def test_post_downloads_yt_id(client):
    resp = client.post("/api/downloads", json={"yt_id": "x", "formato": "mp3"})
    assert resp.status_code == 202
    task = resp.json()["task"]
    assert task["yt_id"] == "x"
    assert task["status"] == "pending"


def test_post_downloads_formato_invalido_422(client):
    resp = client.post("/api/downloads", json={"yt_id": "x", "formato": "flac"})
    assert resp.status_code == 422


def test_post_downloads_corpo_vazio_422(client):
    assert client.post("/api/downloads", json={}).status_code == 422


def test_post_downloads_ambos_422(client):
    assert client.post("/api/downloads", json={"yt_id": "a", "album_id": "b"}).status_code == 422


def test_post_downloads_vazios_contam_como_ausentes_422(client):
    # M-7: strings vazias/em-branco valem como ausentes na regra "exatamente um".
    assert client.post("/api/downloads", json={"yt_id": ""}).status_code == 422
    assert client.post("/api/downloads", json={"album_id": "   "}).status_code == 422


def test_post_downloads_sem_formato_usa_default_opus(tmp_path, history, stub_client, fake_executor):
    # M-1: `Settings.default_format` não é config morta — POST sem `formato` usa o padrão.
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2, default_format="opus")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        resp = client.post("/api/downloads", json={"yt_id": "x"})
        assert resp.status_code == 202
        assert resp.json()["task"]["format"] == "opus"


def test_get_config_has_ffmpeg(client):
    # I-3: endpoint leve expõe has_ffmpeg para a UI (banner de aviso).
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert isinstance(resp.json()["has_ffmpeg"], bool)


def test_post_downloads_album(client):
    resp = client.post("/api/downloads", json={"album_id": "MPREstub", "formato": "mp3"})
    assert resp.status_code == 202
    tasks = resp.json()["tasks"]
    assert len(tasks) == 3


def test_get_downloads_snapshot(client):
    assert client.get("/api/downloads").status_code == 200


def test_get_history(client):
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_library_200_attachment(client, settings):
    rel = "Artista/Album/01 - Faixa.mp3"
    path = settings.musicbox_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp3")
    resp = client.get(f"/api/library/{rel}")
    assert resp.status_code == 200
    assert resp.content == b"fake mp3"
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert "attachment" in resp.headers.get("content-disposition", "")


def test_library_traversal_negado(client):
    resp = client.get("/api/library/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_library_inexistente_404(client):
    assert client.get("/api/library/nao/existe.mp3").status_code == 404


def test_fluxo_completo_search_ate_library(client, history, settings, wait_for):
    # search → albums
    resp = client.get("/api/search", params={"q": "queen"})
    assert resp.status_code == 200
    album_id = resp.json()["albums"][0]["id"]
    # tracks
    resp = client.get(f"/api/albums/{album_id}/tracks")
    assert resp.status_code == 200
    tracks = resp.json()["tracks"]
    assert len(tracks) == 3
    yt_id = tracks[0]["yt_id"]
    # download
    resp = client.post("/api/downloads", json={"yt_id": yt_id, "formato": "mp3"})
    assert resp.status_code == 202
    # poll até histórico done
    assert wait_for(lambda: history.is_downloaded(yt_id), msg="download não concluiu")
    # arquivo existe no destino
    dest = settings.musicbox_dir / "Artista Stub" / "Album Stub" / f"Faixa {yt_id}.mp3"
    assert dest.exists()
    # library serve o arquivo
    resp = client.get(f"/api/library/Artista Stub/Album Stub/Faixa {yt_id}.mp3")
    assert resp.status_code == 200
    assert resp.content == b"fake audio"
    assert resp.headers["content-type"].startswith("audio/mpeg")


def test_websocket_snapshot_e_updates(client):
    with client.websocket_connect("/ws") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert "tasks" in snapshot
        # dispara um download fake (executor síncrono e rápido)
        resp = client.post("/api/downloads", json={"yt_id": "ws-yt", "formato": "mp3"})
        assert resp.status_code == 202
        statuses = []
        for _ in range(3):
            msg = ws.receive_json()
            assert msg["type"] == "update", msg
            statuses.append(msg["status"])
            if msg["status"] == "done":
                break
        assert statuses == ["pending", "running", "done"], statuses


def test_build_default_app_repassa_cookies(tmp_path, monkeypatch):
    # Fix cookies: o client default (YouTubeMusicClient) recebe as cookies lidas
    # da settings (COOKIES_FILE/COOKIES_FROM_BROWSER do ambiente).
    import app.main as main_module

    monkeypatch.setenv("MUSICBOX_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("COOKIES_FILE", str(tmp_path / "cookies.txt"))
    monkeypatch.setenv("COOKIES_FROM_BROWSER", "")
    captured: dict = {}

    class SpyClient:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "YouTubeMusicClient", SpyClient)
    main_module._build_default_app()
    assert captured["kwargs"]["cookies_file"] == tmp_path / "cookies.txt"
    assert captured["kwargs"]["cookies_from_browser"] is None
