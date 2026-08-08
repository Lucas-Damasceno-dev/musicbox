"""Testes de integração do app/main.py (TestClient + fixtures mockadas, sem rede)."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.downloader import Downloader
from app.main import create_app
from app.playlists import PlaylistStore
from app.ytdlp_client import NetworkError, NotFoundError, SearchError


def test_index_serve_frontend(client):
    # index.html existe (PWA em app/static) → 200 com HTML servido.
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<html" in resp.text.lower()
    assert "MusicBox" in resp.text


def test_search_ok(client):
    resp = client.get("/api/search", params={"q": "queen"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["artists"]) == 1 and len(body["albums"]) == 1
    assert body["artists"][0]["kind"] == "artist"
    assert body["albums"][0]["kind"] == "album"
    assert body["playlists"] == []  # stub não retorna playlists


def test_search_q_vazio_422(client):
    assert client.get("/api/search", params={"q": "  "}).status_code == 422
    assert client.get("/api/search").status_code == 422  # q ausente


def test_search_limit_ok(client):
    # `limit` controla quantos itens por seção o cliente expande (stub ignora).
    resp = client.get("/api/search", params={"q": "queen", "limit": 20})
    assert resp.status_code == 200
    assert len(resp.json()["artists"]) == 1


def test_search_limit_invalido_422(client):
    assert client.get("/api/search", params={"q": "queen", "limit": 0}).status_code == 422
    assert client.get("/api/search", params={"q": "queen", "limit": 41}).status_code == 422


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
    # I-3: endpoint leve expõe has_ffmpeg/local_ip/default_format/auth_required.
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["has_ffmpeg"], bool)
    assert body["local_ip"]  # IP local resolvido (não vazio)
    assert body["default_format"] == "opus"  # default do Settings
    assert body["auth_required"] is False  # sem token → auth desativada
    assert body["server_url"].startswith("http://")


def test_post_downloads_album(client):
    resp = client.post("/api/downloads", json={"album_id": "MPREstub", "formato": "mp3"})
    assert resp.status_code == 202
    tasks = resp.json()["tasks"]
    assert len(tasks) == 3


def test_post_downloads_playlist(client):
    # Playlist (PL...) resolve pelo mesmo fluxo de álbum no stub.
    resp = client.post("/api/downloads", json={"playlist_id": "PLstub", "formato": "mp3"})
    assert resp.status_code == 202
    tasks = resp.json()["tasks"]
    assert len(tasks) == 3
    assert all(t["artist"] == "Artista Stub" for t in tasks)


def test_post_downloads_yt_e_playlist_422(client):
    assert client.post("/api/downloads", json={"yt_id": "a", "playlist_id": "b"}).status_code == 422


def test_cancel_download_pending(tmp_path, history, stub_client):
    # Executor que sinaliza quando entra em execução e segura a task até o
    # cancelamento chegar (sem time.sleep fixo — sincronização determinística).
    import threading
    from pathlib import Path

    started = threading.Event()
    release = threading.Event()

    def blocking_executor(*args, **kwargs):
        started.set()
        release.wait(5)  # task fica em execução até o teste liberar
        return Path("nunca")

    settings = Settings(musicbox_dir=tmp_path / "music", workers=2)
    downloader = Downloader(settings, history, stub_client, executor=blocking_executor)
    task = downloader.enqueue("yt-cancel", "mp3")
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        assert started.wait(5), "executor nunca foi chamado"
        resp = client.delete(f"/api/downloads/{task.task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        assert downloader.get(task.task_id).status == "cancelled"
        # terminal → 404 na segunda tentativa; inexistente → 404
        assert client.delete(f"/api/downloads/{task.task_id}").status_code == 404
        assert client.delete("/api/downloads/naoexiste").status_code == 404
        release.set()  # libera o executor antes do shutdown (sem espera extra)


def test_delete_history_remove_arquivo(tmp_path, history, stub_client, fake_executor):
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2)
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    rel = "Artista/Album/01 - Faixa.mp3"
    path = settings.musicbox_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    history.add("yt-del", "Faixa", "Artista", "Album", "mp3")
    history.mark("yt-del", "done", path=str(path))
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        resp = client.delete("/api/history/yt-del")
        assert resp.status_code == 200
        assert history.get("yt-del") is None
        assert not path.exists()  # arquivo apagado junto
        assert client.delete("/api/history/yt-zzz").status_code == 404


# -------------------------------------------------------------- letras (.lrc)


def test_lyrics_200(client, settings, history):
    # Registro com path + `.lrc` irmão → 200 com o conteúdo em text/plain.
    rel = "Artista/Album/01 - Faixa.mp3"
    path = settings.musicbox_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp3")
    lrc = path.with_suffix(".lrc")
    lrc.write_text("[00:01.00]Oi", encoding="utf-8")
    history.add("yt-lrc", "Faixa", "Artista", "Album", "mp3")
    history.mark("yt-lrc", "done", path=str(path))
    resp = client.get("/api/library/yt-lrc/lyrics")
    assert resp.status_code == 200
    assert resp.text == "[00:01.00]Oi"
    assert resp.headers["content-type"].startswith("text/plain")


def test_lyrics_404_sem_arquivo(client, settings, history):
    # Registro com path, mas sem `.lrc` ao lado → 404 "Sem letra".
    rel = "Artista/Album/02 - Outra.mp3"
    path = settings.musicbox_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp3")  # sem irmão .lrc
    history.add("yt-nolrc", "Outra", "Artista", "Album", "mp3")
    history.mark("yt-nolrc", "done", path=str(path))
    resp = client.get("/api/library/yt-nolrc/lyrics")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Sem letra"


def test_lyrics_404_sem_registro(client):
    resp = client.get("/api/library/yt-fantasma/lyrics")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Sem letra"


def test_delete_history_remove_lrc_irmao(client, settings, history):
    # DELETE do histórico remove o áudio E o `.lrc` irmão (missing_ok).
    rel = "Artista/Album/03 - Faixa.mp3"
    path = settings.musicbox_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp3")
    lrc = path.with_suffix(".lrc")
    lrc.write_text("[00:01.00]Oi", encoding="utf-8")
    history.add("yt-del-lrc", "Faixa", "Artista", "Album", "mp3")
    history.mark("yt-del-lrc", "done", path=str(path))
    resp = client.delete("/api/history/yt-del-lrc")
    assert resp.status_code == 200
    assert not path.exists()  # áudio removido
    assert not lrc.exists()  # letra irmã removida junto


def test_auth_requer_token_401(tmp_path, history, stub_client, fake_executor):
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2, auth_token="segredo123")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        # sem token → 401 em qualquer /api/*
        assert client.get("/api/search", params={"q": "queen"}).status_code == 401
        assert client.get("/api/history").status_code == 401
        assert client.get("/api/library/foo.mp3").status_code == 401
        assert client.get("/api/export.m3u").status_code == 401
        # header X-MusicBox-Token → 200
        resp = client.get(
            "/api/search", params={"q": "queen"}, headers={"X-MusicBox-Token": "segredo123"}
        )
        assert resp.status_code == 200
        # token na query (audio/download links) → 200
        resp = client.get("/api/search", params={"q": "queen", "token": "segredo123"})
        assert resp.status_code == 200
        # token errado → 401
        assert (
            client.get(
                "/api/search",
                params={"q": "queen"},
                headers={"X-MusicBox-Token": "errado"},
            ).status_code
            == 401
        )
        # /api/config continua pública (a UI precisa descobrir auth_required)
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/config").json()["auth_required"] is True


def test_auth_desativada_config_false(client):
    assert client.get("/api/config").json()["auth_required"] is False


def test_auth_websocket_sem_token_4401(tmp_path, history, stub_client, fake_executor):
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2, auth_token="segredo123")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
        assert excinfo.value.code == 4401


def test_auth_websocket_com_token_snapshot(tmp_path, history, stub_client, fake_executor):
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2, auth_token="segredo123")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        with client.websocket_connect("/ws?token=segredo123") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"


def test_get_downloads_snapshot(client):
    # Snapshot em memória: lista de tasks com o shape do contrato (to_dict).
    resp = client.post("/api/downloads", json={"yt_id": "snap1", "formato": "mp3"})
    assert resp.status_code == 202
    snapshot = client.get("/api/downloads")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert isinstance(body, list)
    assert any(t["yt_id"] == "snap1" for t in body)
    task = next(t for t in body if t["yt_id"] == "snap1")
    assert {"task_id", "yt_id", "title", "format", "status", "progress", "stage"} <= set(task)
    assert task["status"] in ("pending", "running", "done", "failed", "skipped", "cancelled")
    assert task["progress"] >= 0.0 and task["progress"] <= 100.0


def test_get_history(client, history):
    history.add("yt-h1", "Titulo", "Artista", "Album", "mp3")
    resp = client.get("/api/history")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert row["yt_id"] == "yt-h1"
    assert row["title"] == "Titulo"
    assert {"yt_id", "title", "artist", "album", "format", "status", "date"} <= set(row)


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


def test_build_default_app_client_recebe_settings_env(tmp_path, monkeypatch):
    # O client default (YouTubeMusicClient) recebe timeout/retries/cache_path da
    # settings carregada do ambiente (SOCKET_TIMEOUT/RETRIES/MUSICBOX_DIR).
    import app.main as main_module

    monkeypatch.setenv("MUSICBOX_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("SOCKET_TIMEOUT", "12")
    monkeypatch.setenv("RETRIES", "4")
    captured: dict = {}

    class SpyClient:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "YouTubeMusicClient", SpyClient)
    main_module._build_default_app()
    assert captured["kwargs"]["timeout"] == 12
    assert captured["kwargs"]["retries"] == 4
    assert captured["kwargs"]["cache_path"] == tmp_path / "music" / "search_cache.db"


# ------------------------------------------------------- metadados do histórico


def test_post_history_metadata_sucesso(client, history):
    history.add("yt-meta", "Titulo Antigo", "Artista Antigo", "Album Antigo", "mp3")
    resp = client.post(
        "/api/history/yt-meta/metadata",
        json={"title": "Titulo Novo", "artist": "Artista Novo", "album": "Album Novo"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "yt_id": "yt-meta",
        "title": "Titulo Novo",
        "artist": "Artista Novo",
        "album": "Album Novo",
    }
    record = history.get("yt-meta")  # DB atualizado
    assert record["title"] == "Titulo Novo"
    assert record["artist"] == "Artista Novo"
    assert record["album"] == "Album Novo"


def test_post_history_metadata_validacao_422(client, history):
    history.add("yt-meta", "T", None, None, "mp3")
    # campo longo (>200 chars) → 422
    resp = client.post(
        "/api/history/yt-meta/metadata",
        json={"title": "x" * 300, "artist": "A", "album": "B"},
    )
    assert resp.status_code == 422
    # valor indesejado (não-string) → 422
    assert client.post("/api/history/yt-meta/metadata", json={"title": 123}).status_code == 422
    # título vazio/em-branco → 422
    assert client.post("/api/history/yt-meta/metadata", json={"title": "   "}).status_code == 422
    # registro inexistente → 404
    assert client.post("/api/history/naoexiste/metadata", json={"title": "T"}).status_code == 404


def test_history_update_tags_escreve_no_arquivo(tmp_path, history):
    # mutagen atualiza as tags ID3 de um mp3 real (fluxo do editor de metadados).
    # MP3 mínimo (frames MPEG-1 Layer III) sem cabeçalho ID3 — caso de download
    # recém-feito, em que o fallback do update_tags cria as tags do zero.
    frame = b"\xff\xfb\x90\x64" + b"\x00" * (417 - 4)
    mp3 = tmp_path / "faixa.mp3"
    mp3.write_bytes(frame * 3)
    history.add("yt-tags", "Titulo Antigo", "Artista Antigo", "Album Antigo", "mp3")
    history.mark("yt-tags", "done", path=str(mp3))
    assert history.update_tags("yt-tags", "Titulo Novo", "Artista Novo", "Album Novo") is True

    from mutagen.easyid3 import EasyID3

    tags = EasyID3(str(mp3))
    assert tags["title"] == ["Titulo Novo"]
    assert tags["artist"] == ["Artista Novo"]
    assert tags["album"] == ["Album Novo"]
    assert history.get("yt-tags")["title"] == "Titulo Novo"  # DB atualizado junto


# ------------------------------------------------------------ retry de falhas


def test_retry_failed_reenfileira(tmp_path, history, stub_client, fake_executor, wait_for):
    # Task failed no histórico → retry re-enfileira e o download completa.
    fake_executor.fail = True
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2)
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    downloader.enqueue("yt-retry", "mp3")
    downloader.start()
    try:
        wait_for(
            lambda: (history.get("yt-retry") or {}).get("status") == "failed",
            msg="task não falhou",
        )
    finally:
        downloader.stop()

    fake_executor.fail = False
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        resp = client.post("/api/downloads/retry-failed")
        assert resp.status_code == 200
        body = resp.json()
        assert body["retried_count"] == 1
        assert body["errors"] == []
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["yt_id"] == "yt-retry"
        wait_for(lambda: history.is_downloaded("yt-retry"), msg="retry não concluiu")


def test_retry_failed_erro_no_reenqueue_vira_errors(tmp_path, history, stub_client, fake_executor):
    # Formato inválido no histórico → enqueue levanta ValueError → campo `errors`.
    history.add("yt-flac", "T", None, None, "flac")
    history.mark("yt-flac", "failed")
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2)
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        resp = client.post("/api/downloads/retry-failed")
        assert resp.status_code == 200
        body = resp.json()
        assert body["retried_count"] == 0
        assert body["tasks"] == []
        assert len(body["errors"]) == 1
        assert body["errors"][0]["yt_id"] == "yt-flac"
        assert "Formato inválido" in body["errors"][0]["error"]


def test_retry_failed_fila_vazia(tmp_path, history, stub_client, fake_executor):
    # Sem registros failed → resposta com contagem zero e sem erros.
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2)
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        resp = client.post("/api/downloads/retry-failed")
        assert resp.status_code == 200
        assert resp.json() == {"retried_count": 0, "tasks": [], "errors": []}


# --------------------------------------- storage + pause/resume (Fase 2)


def _storage_client(settings, stub_client, downloader, history, tmp_path):
    """TestClient com o store de playlists FORA de musicbox_dir.

    O app default cria `playlists.db` dentro de `musicbox_dir` — isso poluiria o
    walk do /api/storage (library_size). O store externo mantém o diretório com
    apenas os arquivos que o próprio teste escrever.
    """
    app = create_app(
        settings,
        stub_client,
        downloader,
        history,
        playlists=PlaylistStore(tmp_path / "external" / "playlists.db"),
    )
    return TestClient(app)


def test_storage_retorna_shape(settings, stub_client, downloader, history, tmp_path):
    root = settings.musicbox_dir
    with _storage_client(settings, stub_client, downloader, history, tmp_path) as client:
        # Arquivos criados DEPOIS do lifespan (o startup limpa .part).
        root.mkdir(parents=True, exist_ok=True)
        (root / "a.mp3").write_bytes(b"x" * 100)
        (root / "b.mp3.part").write_bytes(b"x" * 50)
        resp = client.get("/api/storage")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["disk"]) == {"total", "used", "free"}
    assert body["library_size"] == 100
    assert body["partials_size"] == 50
    assert body["partials_count"] == 1


def test_storage_cleanup_remove_part(settings, stub_client, downloader, history, tmp_path):
    root = settings.musicbox_dir
    with _storage_client(settings, stub_client, downloader, history, tmp_path) as client:
        root.mkdir(parents=True, exist_ok=True)
        (root / "b.mp3.part").write_bytes(b"x" * 50)
        (root / "a.mp3").write_bytes(b"x" * 100)
        resp = client.post("/api/storage/cleanup")
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed"] == 1 and body["freed_bytes"] == 50
    assert not (root / "b.mp3.part").exists()
    assert (root / "a.mp3").exists()


def test_pause_resume_rotas_lote(client):
    # Sem tasks ativas, o lote é vazio e válido (200 com as chaves esperadas).
    resp = client.post("/api/downloads/pause", json={})
    assert resp.status_code == 200
    assert "paused" in resp.json()
    resp = client.post("/api/downloads/resume", json={})
    assert resp.status_code == 200
    assert "resumed" in resp.json()


def test_pause_task_inexistente_404(client):
    assert client.post("/api/downloads/nao-existe/pause").status_code == 404
    assert client.post("/api/downloads/nao-existe/resume").status_code == 404


def test_resume_task_nao_pausada_409(client, downloader):
    # Task em qualquer estado ≠ paused (aqui: pending/em execução/done) → 409.
    task = downloader.enqueue("yt-res-409", "mp3")
    resp = client.post(f"/api/downloads/{task.task_id}/resume")
    assert resp.status_code == 409
    assert "incompatível" in resp.json()["detail"]


def test_retry_failed_ignora_paused(client, history):
    # Histórico com 1 failed + 1 paused: retry conta SÓ o failed — paused não
    # é falha (o filtro do retry-failed é por status == "failed").
    history.add("yt-f", "F", "A", "Al", "mp3")
    history.mark("yt-f", "failed", error="erro")
    history.add("yt-p", "P", "A", "Al", "mp3")
    history.mark("yt-p", "paused")
    resp = client.post("/api/downloads/retry-failed")
    assert resp.status_code == 200
    assert resp.json()["retried_count"] == 1


# -------------------------------------------------------------- export .m3u


def test_export_m3u_global_auth_off_sem_token(client, history, settings):
    path = settings.musicbox_dir / "A" / "B" / "faixa.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    history.add("yt1", "Faixa", "A", "B", "mp3")
    history.mark("yt1", "done", path=str(path))
    resp = client.get("/api/export.m3u")
    assert resp.status_code == 200
    body = resp.text
    assert "?token=" not in body  # auth off → URL sem token
    assert "/api/library/A/B/faixa.mp3" in body
    assert "#EXTINF:-1,A - Faixa" in body


def test_export_m3u_global_auth_on_com_token(tmp_path, history, stub_client, fake_executor):
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2, auth_token="segredo123")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    path = settings.musicbox_dir / "A" / "B" / "faixa.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    history.add("yt1", "Faixa", "A", "B", "mp3")
    history.mark("yt1", "done", path=str(path))
    with TestClient(create_app(settings, stub_client, downloader, history)) as client:
        resp = client.get("/api/export.m3u", headers={"X-MusicBox-Token": "segredo123"})
        assert resp.status_code == 200
        assert "?token=segredo123" in resp.text  # auth on → URL com token


def test_export_m3u_global_sanitiza_extinf(client, history, settings):
    # Título/artista com quebras de linha não podem vazar para fora da linha
    # EXTINF (injeção de linha no .m3u): \n/\r viram espaço.
    path = settings.musicbox_dir / "A" / "B" / "faixa.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    history.add("yt1", "Titulo\nQuebrado", "Artista\rX", "B", "mp3")
    history.mark("yt1", "done", path=str(path))
    resp = client.get("/api/export.m3u")
    assert resp.status_code == 200
    extinf_lines = [line for line in resp.text.splitlines() if line.startswith("#EXTINF")]
    assert extinf_lines == ["#EXTINF:-1,Artista X - Titulo Quebrado"]


# ------------------------------------- erros do POST /api/downloads (álbum)


def test_post_downloads_album_not_found_404(client, stub_client):
    stub_client.album_error = NotFoundError("sem álbum")
    resp = client.post("/api/downloads", json={"album_id": "MPREx", "formato": "mp3"})
    assert resp.status_code == 404


def test_post_downloads_album_network_error_503(client, stub_client):
    stub_client.album_error = NetworkError("rede caiu")
    resp = client.post("/api/downloads", json={"album_id": "MPREx", "formato": "mp3"})
    assert resp.status_code == 503


def test_post_downloads_album_search_error_502(client, stub_client):
    stub_client.album_error = SearchError("falha genérica do YouTube Music")
    resp = client.post("/api/downloads", json={"album_id": "MPREx", "formato": "mp3"})
    assert resp.status_code == 502
    assert "falha genérica" in resp.json()["detail"]


def test_post_downloads_album_sem_faixas_404(client, stub_client):
    from app.models import Album

    stub_client.album_tracks = lambda browse_id: Album(
        id=browse_id, title="Vazio", artist="X", tracks=[]
    )
    resp = client.post("/api/downloads", json={"album_id": "MPREvazio", "formato": "mp3"})
    assert resp.status_code == 404
    assert "Sem faixas" in resp.json()["detail"]


def test_post_downloads_playlist_not_found_404(client, stub_client):
    stub_client.album_error = NotFoundError("sem playlist")
    resp = client.post("/api/downloads", json={"playlist_id": "PLstub", "formato": "mp3"})
    assert resp.status_code == 404


# ---------------------------------------------------------- guard de Origin


def test_origin_guard_bloqueia_origin_estranha(client):
    resp = client.post(
        "/api/downloads",
        json={"yt_id": "x", "formato": "mp3"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403


def test_origin_guard_sem_origin_ok(client):
    # curl/scripts/apps nativos não enviam Origin → seguem livres.
    resp = client.post("/api/downloads", json={"yt_id": "x", "formato": "mp3"})
    assert resp.status_code == 202


def test_origin_guard_mesmo_host_ok(client):
    # Origin do próprio host (TestClient usa "testserver") → liberada.
    resp = client.post(
        "/api/downloads",
        json={"yt_id": "x", "formato": "mp3"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 202


def test_origin_guard_nao_bloqueia_get(client):
    # O guard só protege métodos que escrevem (GET não é bloqueado).
    resp = client.get(
        "/api/search", params={"q": "queen"}, headers={"Origin": "http://evil.example.com"}
    )
    assert resp.status_code == 200


# ------------------------------------------------------------ SSE (429) e m3u


def test_search_stream_quinta_conexao_429(client):
    # Ocupa as 4 vagas do semáforo global (simula 4 streams ativos). O TestClient
    # serializa requests (portal executa cada um até o fim), então a concorrência
    # real não é reproduzível via HTTP — o teste exercita o gate de 429 de forma
    # determinística e libera tudo no finally (sem conexão órfã).
    import app.main as main_module

    for _ in range(main_module._MAX_SSE_STREAMS):
        assert main_module._SSE_SEMAPHORE.acquire(blocking=False)
    try:
        resp = client.get("/api/search/stream", params={"q": "queen"})
        assert resp.status_code == 429
        assert "Muitos streams" in resp.json()["detail"]
    finally:
        for _ in range(main_module._MAX_SSE_STREAMS):
            main_module._SSE_SEMAPHORE.release()


def test_m3u_lines_sanitiza_e_controla_token(settings):
    # Helper _m3u_lines (importado de app.main): sanitização do EXTINF e
    # ausência/presença do token conforme o token_suffix passado.
    from app.main import _m3u_lines

    records = [
        {
            "status": "done",
            "path": str(settings.musicbox_dir / "A" / "B" / "faixa.mp3"),
            "title": "Titulo\nQuebrado",
            "artist": "Artista\x00X",
        },
        {"status": "failed", "path": "/x.mp3", "title": "Fora", "artist": "F"},
        {
            "status": "skipped",
            "path": str(settings.musicbox_dir / "C" / "skip.mp3"),
            "title": "Pulada",
            "artist": None,
        },
    ]
    body = "".join(_m3u_lines(records, "http://host:8080", "", settings.musicbox_dir))
    assert body.startswith("#EXTM3U\n")
    assert "Titulo Quebrado" in body and "Artista X" in body  # \n e \x00 → espaço
    assert "?token=" not in body
    assert "Fora" not in body  # failed não entra
    assert "Pulada" in body and "Desconhecido - Pulada" in body  # skipped entra

    body_token = "".join(
        _m3u_lines(records, "http://host:8080", "?token=abc", settings.musicbox_dir)
    )
    assert "?token=abc" in body_token
