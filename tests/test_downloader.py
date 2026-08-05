"""Testes do app/downloader.py: sanitização, fluxo done/failed/skipped, disco, .part, batch."""

from pathlib import Path

import pytest

from app.downloader import Downloader, sanitize_filename
from app.models import Track


# --------------------------------------------------------------- sanitize_filename

def test_sanitize_filename_caracteres_invalidos():
    assert sanitize_filename("a/b<c>d") == "abcd"
    assert "\\" not in sanitize_filename("a\\b")
    assert ":" not in sanitize_filename("a:b")


def test_sanitize_filename_controle_e_trim():
    assert sanitize_filename("a\x00b") == "ab"
    assert sanitize_filename("  nome.mp3  ") == "nome.mp3"
    assert sanitize_filename("nome.mp3.") == "nome.mp3"


def test_sanitize_filename_trunca_e_fallback():
    assert len(sanitize_filename("x" * 300)) == 180
    assert sanitize_filename("") == "sem_nome"
    assert sanitize_filename("///") == "sem_nome"


# ------------------------------------------------------- fluxo com executor fake

def test_download_completo(settings, history, stub_client, fake_executor, wait_for):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    events = []
    downloader.add_listener(
        lambda task_id, status, progress, stage: events.append((task_id, status, progress, stage))
    )
    task = downloader.enqueue("yt1", "mp3")
    downloader.start()
    try:
        # `task.status` (memória) vira "done" antes do `history.mark` persistir —
        # espera o estado persistido para não correr contra a thread do worker.
        wait_for(lambda: history.is_downloaded("yt1"), msg="download não terminou")
        assert task.status == "done"
        assert task.path is not None
        expected = settings.musicbox_dir / "Artista Stub" / "Album Stub" / "Faixa yt1.mp3"
        assert Path(task.path) == expected
        assert expected.exists()
        assert history.is_downloaded("yt1")
        assert history.get("yt1")["path"] == str(expected)
        statuses = [e[1] for e in events]
        assert statuses == ["pending", "running", "done"], statuses
        assert events[0][2] == 0.0 and events[-1][2] == 100.0  # progresso 0 → 100
    finally:
        downloader.stop()


def test_download_avulso_preenche_metadata_task_e_historico(
    settings, history, stub_client, fake_executor, wait_for
):
    # I-1: enqueue avulso sem title/artist/album (placeholder title=yt_id) →
    # após done, task E histórico carregam o título/artista/álbum do metadata.
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt1", "mp3")
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt1"), msg="download não terminou")
        assert task.title == "Faixa yt1"  # metadata do stub (campo track)
        record = history.get("yt1")
        assert record["title"] == "Faixa yt1"
        assert record["artist"] == "Artista Stub"
        assert record["album"] == "Album Stub"
    finally:
        downloader.stop()


def test_download_metadados_explicitos_vencem(
    settings, history, stub_client, fake_executor, wait_for
):
    # I-1: valores explícitos informados no enqueue (UI com data-title /
    # enqueue_album) NÃO são sobrescritos pelo metadata do YouTube Music.
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue(
        "yt1", "mp3", title="Titulo UI", artist="Artista UI", album="Album UI"
    )
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt1"), msg="download não terminou")
        assert task.title == "Titulo UI"
        assert task.artist == "Artista UI"
        assert task.album == "Album UI"
        record = history.get("yt1")
        assert record["title"] == "Titulo UI"
        assert record["artist"] == "Artista UI"
        assert record["album"] == "Album UI"
    finally:
        downloader.stop()


def test_download_executor_falha(settings, history, stub_client, fake_executor, wait_for):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    fake_executor.fail = True
    task = downloader.enqueue("yt-fail", "mp3")
    downloader.start()
    try:
        # `task.status` (memória) vira "failed" antes do `history.mark` persistir —
        # espera o estado persistido (mesma corrida do test_download_completo).
        wait_for(
            lambda: (history.get("yt-fail") or {}).get("status") == "failed",
            msg="task não falhou",
        )
        assert task.status == "failed"
        assert task.error and "executor falhou" in task.error
    finally:
        downloader.stop()


def test_reenqueue_ja_done_skipped(settings, history, stub_client, fake_executor, wait_for):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    t1 = downloader.enqueue("yt1", "mp3")
    downloader.start()
    try:
        # Espera o histórico PERSISTIDO como done: o re-enqueue do mesmo yt_id
        # decide o skip lendo o banco (is_downloaded) — memória pode adiantar.
        wait_for(lambda: history.is_downloaded("yt1"), msg="primeiro download")
        t2 = downloader.enqueue("yt1", "mp3")
        wait_for(lambda: t2.status == "skipped", msg="re-enqueue não virou skipped")
        assert t2.status == "skipped"
    finally:
        downloader.stop()


def test_arquivo_existe_sem_historico_done(settings, history, stub_client, fake_executor, wait_for):
    dest = settings.musicbox_dir / "Artista Stub" / "Album Stub" / "Faixa yt2.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"ja existe")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt2", "mp3")
    downloader.start()
    try:
        wait_for(lambda: task.status == "done", msg="download com sufixo")
        assert task.path == str(dest.with_name("Faixa yt2 (2).mp3"))
        assert dest.with_name("Faixa yt2 (2).mp3").exists()
        assert dest.exists()  # original preservado
    finally:
        downloader.stop()


def test_disco_cheio(settings, history, stub_client, fake_executor, wait_for, monkeypatch):
    import shutil

    monkeypatch.setattr(
        shutil, "disk_usage", lambda p: type("DU", (), {"free": 10 * 1024 * 1024})()
    )
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt-full", "mp3")
    downloader.start()
    try:
        wait_for(lambda: task.status == "failed", msg="disco cheio não falhou")
        assert task.error and "espaço" in task.error.lower()
    finally:
        downloader.stop()


def test_cleanup_partials(settings, history, stub_client, fake_executor):
    root = settings.musicbox_dir
    (root / "Artista").mkdir(parents=True)
    (root / "Artista" / "a.part").write_bytes(b"p")
    (root / "b.mp3.ytdl").write_bytes(b"m")
    (root / "ok.mp3").write_bytes(b"ok")
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    downloader.cleanup_partials()
    assert not (root / "Artista" / "a.part").exists()
    assert not (root / "b.mp3.ytdl").exists()
    assert (root / "ok.mp3").exists()


def test_enqueue_album_batch(settings, history, stub_client, fake_executor):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    tracks = [Track(yt_id=f"al{i}", title=f"Faixa {i}", number=i) for i in (1, 2, 3)]
    tasks = downloader.enqueue_album(tracks, "mp3", "Artista Album", "Album X")
    assert len(tasks) == 3
    assert all(t.status == "pending" for t in tasks)
    assert all(t.number == i for i, t in enumerate(tasks, start=1))
    assert history.count() == 3  # batch em uma transação


def test_enqueue_formato_invalido(settings, history, stub_client, fake_executor):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    with pytest.raises(ValueError):
        downloader.enqueue("yt1", "flac")
    with pytest.raises(ValueError):
        downloader.enqueue_album([], "wav", "a", "b")


def test_snapshot_e_get(settings, history, stub_client, fake_executor):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    t1 = downloader.enqueue("yt1", "mp3")
    t2 = downloader.enqueue("yt2", "opus")
    assert downloader.get(t1.task_id) is t1
    assert downloader.get("inexistente") is None
    assert [t.task_id for t in downloader.snapshot()] == [t1.task_id, t2.task_id]


# ----------------------------------- executor default (anônimo + client android)

def _capture_default_executor_opts(settings, history, stub_client, monkeypatch, tmp_path):
    """Roda `_default_executor` com yt-dlp fake que captura as opts (sem rede)."""
    import yt_dlp

    captured: dict = {}

    class FakeYDL:
        def __init__(self, opts=None):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            # Simula o arquivo de áudio pós-postprocess (outtmpl do executor).
            temp = Path(captured["opts"]["outtmpl"]).parent
            (temp / "audio.mp3").write_bytes(b"fake audio")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    downloader = Downloader(settings, history, stub_client)  # executor default real
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    downloader._default_executor("yt1", "mp3", temp_dir, tmp_path / "dest", "stem", {})
    return captured["opts"]


def test_default_executor_extractor_args_android(
    settings, history, stub_client, monkeypatch, tmp_path
):
    # Download anônimo + client android: única combinação que devolve formatos
    # (2026-08-05) — sessão logada é flagada pelo YouTube.
    opts = _capture_default_executor_opts(settings, history, stub_client, monkeypatch, tmp_path)
    assert opts["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def test_default_executor_download_anonimo_sem_cookies(
    settings, history, stub_client, monkeypatch, tmp_path
):
    opts = _capture_default_executor_opts(settings, history, stub_client, monkeypatch, tmp_path)
    assert "cookiefile" not in opts
    assert "cookiesfrombrowser" not in opts
    assert opts["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def test_default_executor_ignora_cookies_mesmo_quando_configurados(
    settings, history, stub_client, monkeypatch, tmp_path
):
    # Download DEVE ser anônimo: cookies configurados nas settings não vazam
    # para as opts do executor (sessão logada quebra o player response).
    settings.cookies_file = tmp_path / "cookies.txt"
    settings.cookies_from_browser = "chrome"
    opts = _capture_default_executor_opts(settings, history, stub_client, monkeypatch, tmp_path)
    assert "cookiefile" not in opts
    assert "cookiesfrombrowser" not in opts
    assert opts["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def test_fail_remove_ansi_do_motivo(settings, history, stub_client, wait_for):
    def failing_executor(*args, **kwargs):
        raise RuntimeError(
            "\x1b[0;31mERROR:\x1b[0m [youtube] yt1: "
            "Sign in to confirm you're not a bot. Use --cookies-from-browser."
        )

    downloader = Downloader(settings, history, stub_client, executor=failing_executor)
    task = downloader.enqueue("yt1", "mp3")
    downloader.start()
    try:
        wait_for(
            lambda: (history.get("yt1") or {}).get("status") == "failed",
            msg="task não falhou",
        )
        assert task.error == (
            "Falha no download: ERROR: [youtube] yt1: "
            "Sign in to confirm you're not a bot. Use --cookies-from-browser."
        )
        assert "\x1b[" not in task.error
    finally:
        downloader.stop()
