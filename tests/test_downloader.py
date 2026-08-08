"""Testes do app/downloader.py: sanitização, fluxo done/failed/skipped, disco, .part, batch."""

import threading
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
    downloader.enqueue("yt1", "mp3")
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


def test_enqueue_album_repassa_cover(settings, history, stub_client, fake_executor):
    # Capa da faixa (album_tracks) chega na task E no histórico (player com capa).
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    tracks = [
        Track(yt_id=f"al{i}", title=f"Faixa {i}", number=i, cover_url=f"https://c/{i}.jpg")
        for i in (1, 2, 3)
    ]
    tasks = downloader.enqueue_album(tracks, "mp3", "Artista Album", "Album X")
    assert tasks[0].cover_url == "https://c/1.jpg"
    assert history.get("al1")["cover_url"] == "https://c/1.jpg"


def test_download_preenche_cover_url(settings, history, stub_client, fake_executor, wait_for):
    # Download avulso: a capa vem do metadata do YouTube Music (track_metadata).
    stub_client.track_metadata = lambda yt_id: {
        "yt_id": yt_id,
        "title": f"Titulo {yt_id}",
        "artists": ["Artista Stub"],
        "album": "Album Stub",
        "track": f"Faixa {yt_id}",
        "release_year": 2024,
        "thumbnail": "https://example.com/cover.jpg",
        "duration": 181,
        "webpage_url": f"https://music.youtube.com/watch?v={yt_id}",
    }
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt-cover", "mp3")
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt-cover"), msg="download não terminou")
        assert task.cover_url == "https://example.com/cover.jpg"
        assert history.get("yt-cover")["cover_url"] == "https://example.com/cover.jpg"
    finally:
        downloader.stop()


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


# ------------------------------------------------------------- cancelamento


def test_cancel_tarefa_pendente_discartada(settings, history, stub_client, fake_executor, wait_for):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt1", "mp3")
    assert downloader.cancel(task.task_id) is True
    assert task.status == "cancelled"
    downloader.start()
    try:
        # O worker descarta a task cancelada (não vira done/skipped).
        wait_for(
            lambda: (history.get("yt1") or {}).get("status") == "cancelled",
            msg="histórico não marcou cancelled",
        )
        assert not history.is_downloaded("yt1")
    finally:
        downloader.stop()


def test_cancel_tarefa_em_execucao(settings, history, stub_client, wait_for):
    # Executor que sinaliza quando entra em execução e segura a task até o
    # cancelamento chegar (sem time.sleep fixo — sincronização determinística).
    started = threading.Event()
    release = threading.Event()

    def blocking_executor(*args, **kwargs):
        started.set()
        release.wait(5)
        return Path("nunca-existe")

    downloader = Downloader(settings, history, stub_client, executor=blocking_executor)
    task = downloader.enqueue("yt-bloq", "mp3")
    downloader.start()
    try:
        assert started.wait(5), "executor nunca foi chamado"
        assert task.status == "running"  # garantia extra (determinística)
        assert downloader.cancel(task.task_id) is True
        wait_for(lambda: task.status == "cancelled", msg="task não foi cancelada")
        wait_for(
            lambda: (history.get("yt-bloq") or {}).get("status") == "cancelled",
            msg="histórico não marcou cancelled",
        )
    finally:
        release.set()  # libera o executor antes do stop (sem espera extra)
        downloader.stop()


def test_cancel_tarefa_terminal_ou_inexistente_false(
    settings, history, stub_client, fake_executor, wait_for
):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    t1 = downloader.enqueue("yt1", "mp3")
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt1"), msg="download não concluiu")
        assert downloader.cancel(t1.task_id) is False  # done → não cancela
        assert downloader.cancel("inexistente") is False
    finally:
        downloader.stop()


# ------------------------------------------------------------ pause / resume


def test_pause_marca_paused_e_preserva_part(settings, history, stub_client, wait_for):
    # Executor que sinaliza quando entra em execução e segura a task até o pause
    # chegar; o "download parcial" (fake do .part) fica em temp_dir.
    started = threading.Event()
    release = threading.Event()

    def blocking_executor(yt_id, fmt, temp_dir, dest_dir, dest_filename_stem, metadata):
        (Path(temp_dir) / "audio.opus").write_bytes(b"part")
        started.set()
        release.wait(5)
        return Path(temp_dir) / "audio.opus"

    downloader = Downloader(settings, history, stub_client, executor=blocking_executor)
    task = downloader.enqueue("yt-pause", "opus")
    downloader.start()
    try:
        assert started.wait(5), "executor nunca foi chamado"
        assert task.status == "running"  # garantia extra (determinística)
        assert downloader.pause([task.task_id]) == [task.task_id]
        assert task.status == "paused"
        assert task.cancel_requested is True
        wait_for(
            lambda: (history.get("yt-pause") or {}).get("status") == "paused",
            msg="histórico não marcou paused",
        )
        release.set()  # libera o executor: o worker não pode sobrescrever paused
        # Após o worker terminar, o .part (fake) permanece em temp_dir — a pausa
        # preserva para o resume nativo do yt-dlp (só o startup limpa .part).
        wait_for(lambda: downloader._queue.unfinished_tasks == 0, msg="worker não terminou")
        part = settings.musicbox_dir / ".tmp" / task.task_id / "audio.opus"
        assert part.exists()
    finally:
        release.set()
        downloader.stop()


def test_resume_reusa_mesma_task_e_reenfileira(
    settings, history, stub_client, fake_executor, wait_for
):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt-resume", "opus")
    # Pausa enquanto ainda pending (na fila) — determinístico, sem worker.
    assert downloader.pause([task.task_id]) == [task.task_id]
    assert task.status == "paused"
    resumed = downloader.resume([task.task_id])
    assert resumed == [task.task_id]
    assert task.status == "pending"
    assert task.cancel_requested is False
    # MESMA task: snapshot tem exatamente 1 task com aquele id (sem duplicata).
    assert [s.task_id for s in downloader.snapshot()].count(task.task_id) == 1
    # A task retomada completa normalmente quando o worker sobe (uma vez só).
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt-resume"), msg="resume não completou")
        assert task.status == "done"
    finally:
        downloader.stop()


def test_pause_lote_none_pausa_todas_ativas(settings, history, stub_client, fake_executor):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    t1 = downloader.enqueue("yt1", "opus")
    t2 = downloader.enqueue("yt2", "opus")
    # Sem worker: ambas ainda pending → pause() sem ids pausa as duas.
    assert sorted(downloader.pause()) == sorted([t1.task_id, t2.task_id])
    assert t1.status == "paused" and t2.status == "paused"


def test_pause_ignora_terminal(settings, history, stub_client, fake_executor, wait_for):
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt1", "mp3")
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt1"), msg="download não concluiu")
        assert task.status == "done"
        assert downloader.pause([task.task_id]) == []  # done não pausa
        assert downloader.pause() == []  # nenhuma ativa para pausar
        assert task.status == "done"
    finally:
        downloader.stop()


def test_worker_descarta_paused_na_fila(settings, history, stub_client, wait_for):
    ran = []

    def recording_executor(yt_id, *args, **kwargs):
        ran.append(yt_id)
        return Path("nunca-existe")

    downloader = Downloader(settings, history, stub_client, executor=recording_executor)
    task = downloader.enqueue("yt-desc", "mp3")
    assert downloader.pause([task.task_id]) == [task.task_id]  # pausa ainda pendente
    downloader.start()
    try:
        # O worker pega a task da fila e a descarta (guard de paused): a task
        # sai de `_queued` e o executor fake nunca roda.
        wait_for(lambda: task.task_id not in downloader._queued, msg="fila não drenou")
        assert ran == []  # executor nunca executou
        assert task.status == "paused"
    finally:
        downloader.stop()


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


# ------------------------------------------------------ letra (.lrc) no download


def test_download_grava_lrc_ao_lado(
    settings, history, stub_client, fake_executor, wait_for, monkeypatch
):
    # fetch_lrc devolve LRC → o `.lrc` irmão é gravado com os metadados da task.
    import app.downloader as dl_mod

    calls = []

    def fake_fetch(artist, title, album=None):
        calls.append((artist, title, album))
        return "[00:01.00]Oi"

    monkeypatch.setattr(dl_mod, "fetch_lrc", fake_fetch)
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt-lrc", "mp3")
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt-lrc"), msg="download não concluiu")
        assert task.status == "done"
        assert task.path is not None
        lrc = Path(task.path).with_suffix(".lrc")
        assert lrc.exists()
        assert lrc.read_text(encoding="utf-8") == "[00:01.00]Oi"
        # fetch_lrc chamado com os metadados resolvidos da task (stub).
        assert calls == [("Artista Stub", "Faixa yt-lrc", "Album Stub")]
    finally:
        downloader.stop()


def test_download_sem_lrc_quando_fetch_none(
    settings, history, stub_client, fake_executor, wait_for, monkeypatch
):
    # fetch_lrc → None: download conclui normalmente e NENHUM `.lrc` é criado.
    import app.downloader as dl_mod

    monkeypatch.setattr(dl_mod, "fetch_lrc", lambda artist, title, album=None: None)
    downloader = Downloader(settings, history, stub_client, executor=fake_executor)
    task = downloader.enqueue("yt-nolrc", "mp3")
    downloader.start()
    try:
        wait_for(lambda: history.is_downloaded("yt-nolrc"), msg="download não concluiu")
        assert task.status == "done"
        assert task.path is not None
        assert not Path(task.path).with_suffix(".lrc").exists()
    finally:
        downloader.stop()
