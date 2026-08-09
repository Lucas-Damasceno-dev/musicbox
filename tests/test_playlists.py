"""Testes do app/playlists.py (store SQLite) e das rotas /api/playlists e /api/browse.

Identificadores em inglês; docstrings/comentários em português.
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.downloader import Downloader
from app.main import create_app
from app.playlists import PlaylistStore


def _make_app(settings, history, stub_client, fake_executor, store=None):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    if store is None:
        store = PlaylistStore(settings.musicbox_dir / "playlists.db")
    return create_app(settings, stub_client, downloader, history, store)


# ------------------------------------------------------------------ store


def test_store_crud(tmp_path):
    store = PlaylistStore(tmp_path / "playlists.db")
    pl = store.create("Favoritas")
    assert pl["id"] > 0 and pl["name"] == "Favoritas"
    assert store.get(pl["id"])["name"] == "Favoritas"
    assert [p["name"] for p in store.list_all()] == ["Favoritas"]
    assert store.delete(pl["id"]) is True
    assert store.get(pl["id"]) is None
    assert store.delete(pl["id"]) is False  # já não existe


def test_store_tracks_dedupe_e_ordem(tmp_path):
    store = PlaylistStore(tmp_path / "playlists.db")
    pl = store.create("Mix")
    pid = pl["id"]
    assert store.add_track(pid, "t1") is True
    assert store.add_track(pid, "t2") is True
    assert store.add_track(pid, "t1") is False  # dedupe por yt_id
    assert store.track_ids(pid) == ["t1", "t2"]
    assert store.get(pid)["track_count"] == 2
    assert store.remove_track(pid, "t1") is True
    assert store.track_ids(pid) == ["t2"]


def test_store_delete_cascade_faixas(tmp_path):
    store = PlaylistStore(tmp_path / "playlists.db")
    pid = store.create("X")["id"]
    store.add_track(pid, "t1")
    store.delete(pid)
    assert store.track_ids(pid) == []  # CASCADE removeu as faixas


# ------------------------------------------------------------------ rotas


def test_playlists_rotas_crud(settings, history, stub_client, fake_executor):
    with TestClient(_make_app(settings, history, stub_client, fake_executor)) as client:
        assert client.get("/api/playlists").json() == []
        resp = client.post("/api/playlists", json={"name": "  Favoritas  "})
        assert resp.status_code == 201
        pl = resp.json()
        assert pl["name"] == "Favoritas"
        # nome vazio → 422
        assert client.post("/api/playlists", json={"name": "   "}).status_code == 422
        # adiciona faixa (dedupe)
        resp = client.post(f"/api/playlists/{pl['id']}/tracks", json={"yt_id": "yt1"})
        assert resp.status_code == 201
        resp = client.post(f"/api/playlists/{pl['id']}/tracks", json={"yt_id": "yt1"})
        assert resp.status_code == 201
        # playlist com faixas (join com o histórico — yt1 não baixada → sem path)
        detail = client.get(f"/api/playlists/{pl['id']}").json()
        assert detail["track_count"] == 1
        assert detail["tracks"][0]["yt_id"] == "yt1"
        assert detail["tracks"][0]["path"] is None
        # remove faixa e apaga playlist
        assert client.delete(f"/api/playlists/{pl['id']}/tracks/yt1").status_code == 200
        assert client.delete(f"/api/playlists/{pl['id']}").status_code == 200
        assert client.get("/api/playlists").json() == []
        # 404s
        assert client.get("/api/playlists/999").status_code == 404
        assert client.delete("/api/playlists/999").status_code == 404
        assert client.post("/api/playlists/999/tracks", json={"yt_id": "x"}).status_code == 404


def test_playlist_export_m3u_so_com_baixadas(settings, history, stub_client, fake_executor):
    # Faixa baixada (done + path) entra no .m3u; não-baixada fica de fora.
    path = settings.musicbox_dir / "Artista" / "Album" / "faixa.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    history.add("yt1", "Faixa", "Artista", "Album", "mp3")
    history.mark("yt1", "done", path=str(path))
    history.add("yt2", "Nao baixada", None, None, "mp3")

    store = PlaylistStore(settings.musicbox_dir / "playlists.db")
    pid = store.create("Export")["id"]
    store.add_track(pid, "yt1")
    store.add_track(pid, "yt2")

    with TestClient(_make_app(settings, history, stub_client, fake_executor, store)) as client:
        resp = client.get(f"/api/playlists/{pid}/export.m3u")
        assert resp.status_code == 200
        body = resp.text
        assert "#EXTM3U" in body
        assert "Artista - Faixa" in body
        assert "/api/library/Artista/Album/faixa.mp3" in body
        assert "Nao baixada" not in body


def test_playlist_export_m3u_sanitiza_extinf_e_token(tmp_path, history, stub_client, fake_executor):
    # EXTINF com quebra de linha no título é sanitizado (espaço) e, com auth
    # ativa, a URL de cada faixa carrega `?token=`.
    settings = Settings(musicbox_dir=tmp_path / "music", workers=2, auth_token="segredo")
    path = settings.musicbox_dir / "A" / "B" / "faixa.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    history.add("yt1", "Titulo\nQuebrado", "Artista", "B", "mp3")
    history.mark("yt1", "done", path=str(path))

    store = PlaylistStore(settings.musicbox_dir / "playlists.db")
    pid = store.create("Sanitize")["id"]
    store.add_track(pid, "yt1")

    with TestClient(_make_app(settings, history, stub_client, fake_executor, store)) as client:
        resp = client.get(
            f"/api/playlists/{pid}/export.m3u",
            headers={"X-MusicBox-Token": "segredo"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "#EXTINF:-1,Artista - Titulo Quebrado" in body  # \n → espaço
        assert "?token=segredo" in body


def test_browse_agrupa_artista_album_faixas(settings, history, stub_client, fake_executor):
    # done + path → aparece; failed sem path → fica de fora. Faixas ordenadas
    # pelo prefixo numérico do arquivo ("01 - título"), não pela data.
    for num, title in (("02", "Segunda"), ("01", "Primeira")):
        p = settings.musicbox_dir / "Artista X" / "Album X" / f"{num} - {title}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake")
        history.add(f"yt-{num}", title, "Artista X", "Album X", "mp3")
        history.mark(f"yt-{num}", "done", path=str(p))
    p2 = settings.musicbox_dir / "Artista Y" / "Album Y" / "01 - Unica.mp3"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_bytes(b"fake")
    history.add("yt-y", "Unica", "Artista Y", "Album Y", "mp3")
    history.mark("yt-y", "done", path=str(p2))
    history.add("yt-f", "Falha", "Artista X", "Album X", "mp3")
    history.mark("yt-f", "failed", error="motivo")

    with TestClient(_make_app(settings, history, stub_client, fake_executor)) as client:
        resp = client.get("/api/browse")
        assert resp.status_code == 200
        tree = resp.json()
        assert [a["name"] for a in tree] == ["Artista X", "Artista Y"]
        assert tree[0]["albums"][0]["name"] == "Album X"
        tracks = tree[0]["albums"][0]["tracks"]
        # "02 - Segunda" foi adicionada primeiro, mas a ordem é pelo número.
        assert [t["title"] for t in tracks] == ["Primeira", "Segunda"]
        assert tracks[0]["path"]  # path presente


def test_browse_vazio_sem_historico(settings, history, stub_client, fake_executor):
    with TestClient(_make_app(settings, history, stub_client, fake_executor)) as client:
        assert client.get("/api/browse").json() == []


def test_search_stream_emite_secoes(settings, history, stub_client, fake_executor):
    # SSE da busca: corpo traz as seções (stub devolve artistas/álbuns; songs/playlists vazias).
    with TestClient(_make_app(settings, history, stub_client, fake_executor)) as client:
        resp = client.get("/api/search/stream", params={"q": "queen"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.text
        assert "event: section" in body
        assert "MPREstub" in body  # álbum do stub
        assert "event: done" in body


def test_search_stream_limit_invalido_422(settings, history, stub_client, fake_executor):
    with TestClient(_make_app(settings, history, stub_client, fake_executor)) as client:
        assert client.get("/api/search/stream", params={"q": "x", "limit": 0}).status_code == 422
        assert client.get("/api/search/stream", params={"q": "", "limit": 10}).status_code == 422
