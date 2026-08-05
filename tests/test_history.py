"""Testes do app/history.py: CRUD, dedupe, batch (transação única), COALESCE, ordenação."""

import pytest

from app.history import History


def test_add_e_get(history: History):
    row_id = history.add("yt1", "Titulo", "Artista", "Album", "mp3")
    assert row_id > 0
    record = history.get("yt1")
    assert record["yt_id"] == "yt1"
    assert record["title"] == "Titulo"
    assert record["artist"] == "Artista"
    assert record["album"] == "Album"
    assert record["format"] == "mp3"
    assert record["status"] == "pending"


def test_dedupe_por_yt_id(history: History):
    history.add("yt1", "Titulo", None, None, "mp3")
    history.add("yt1", "Outro", None, None, "opus")  # INSERT OR REPLACE atualiza
    assert history.count() == 1
    assert history.get("yt1")["format"] == "opus"


def test_add_many_transacao(history: History):
    history.add_many(
        [
            {"yt_id": "a", "title": "A", "artist": None, "album": None, "format": "mp3"},
            {"yt_id": "b", "title": "B", "artist": None, "album": None, "format": "mp3"},
            {"yt_id": "c", "title": "C", "artist": None, "album": None, "format": "opus"},
        ]
    )
    assert history.count() == 3


def test_add_many_rollback_em_falha(history: History):
    # entry sem a chave `format` → KeyError dentro da transação → rollback total.
    with pytest.raises(KeyError):
        history.add_many(
            [
                {"yt_id": "a", "title": "A", "artist": None, "album": None, "format": "mp3"},
                {"yt_id": "b", "title": "B"},
            ]
        )
    assert history.count() == 0


def test_mark_coalesce_path_error(history: History):
    history.add("yt1", "Titulo", None, None, "mp3")
    history.mark("yt1", "done", path="/music/faixa.mp3")
    history.mark("yt1", "failed")  # sem path → não limpa o path existente
    record = history.get("yt1")
    assert record["status"] == "failed"
    assert record["path"] == "/music/faixa.mp3"
    history.mark("yt1", "failed", error="motivo")
    assert history.get("yt1")["error"] == "motivo"
    history.mark("yt1", "done", path="/outro.mp3")
    assert history.get("yt1")["path"] == "/outro.mp3"


def test_update_meta_nao_toca_status_path_date(history: History):
    # I-1: update_meta substitui SÓ title/artist/album (placeholders do enqueue
    # avulso) — status/path/date já gravados são preservados (sem REPLACE).
    history.add("yt1", "yt1", None, None, "mp3")  # placeholder title=yt_id
    history.mark("yt1", "done", path="/music/faixa.mp3")
    history.update_meta("yt1", "Titulo Real", "Artista Stub", "Album Stub")
    record = history.get("yt1")
    assert record["title"] == "Titulo Real"
    assert record["artist"] == "Artista Stub"
    assert record["album"] == "Album Stub"
    assert record["status"] == "done"  # status preservado
    assert record["path"] == "/music/faixa.mp3"  # path preservado
    assert record["date"]  # date não foi resetada


def test_is_downloaded(history: History):
    history.add("yt1", "T", None, None, "mp3")
    assert not history.is_downloaded("yt1")
    history.mark("yt1", "done")
    assert history.is_downloaded("yt1")
    history.mark("yt1", "skipped")
    assert not history.is_downloaded("yt1")  # só done conta


def test_list_ordenado(history: History):
    for yt_id in ("yt1", "yt2", "yt3"):
        history.add(yt_id, f"T{yt_id}", None, None, "mp3")
    rows = history.list(limit=100)
    assert [r["yt_id"] for r in rows] == ["yt3", "yt2", "yt1"]  # date DESC, id DESC


def test_list_limite(history: History):
    for yt_id in ("yt1", "yt2", "yt3"):
        history.add(yt_id, f"T{yt_id}", None, None, "mp3")
    assert len(history.list(limit=2)) == 2


def test_count(history: History):
    assert history.count() == 0
    history.add("yt1", "T", None, None, "mp3")
    assert history.count() == 1
