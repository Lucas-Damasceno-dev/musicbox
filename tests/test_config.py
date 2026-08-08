"""Testes do app/config.py: has_ffmpeg, load_settings, precedência env > .env > defaults."""

import shutil
from pathlib import Path

import app.config as config_module
from app.config import Settings, _parse_env_file, load_settings


def test_has_ffmpeg_presente(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert Settings().has_ffmpeg is True


def test_has_ffmpeg_ausente(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert Settings().has_ffmpeg is False


def test_load_settings_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("MUSICBOX_DIR", str(tmp_path / "musica"))
    monkeypatch.setenv("WORKERS", "4")
    settings = load_settings()
    assert settings.port == 9999
    assert settings.musicbox_dir == tmp_path / "musica"
    assert settings.workers == 4


def test_load_settings_env_vence_dotenv(monkeypatch):
    # .env define PORT=7070; env real define PORT=9090 → env vence.
    monkeypatch.setattr(config_module, "_parse_env_file", lambda path: {"PORT": "7070"})
    monkeypatch.setenv("PORT", "9090")
    assert load_settings().port == 9090


def test_load_settings_dotenv_vence_default(monkeypatch):
    monkeypatch.setattr(
        config_module, "_parse_env_file", lambda path: {"PORT": "7070", "DEFAULT_FORMAT": "opus"}
    )
    settings = load_settings()
    assert settings.port == 7070
    assert settings.default_format == "opus"


def test_load_settings_expande_home(monkeypatch):
    monkeypatch.setenv("MUSICBOX_DIR", "~/Music/musicbox")
    assert load_settings().musicbox_dir == Path.home() / "Music" / "musicbox"


def test_load_settings_env_invalida_fallback_default(monkeypatch, tmp_path):
    # Envs numéricas inválidas não derrubam o load: caem no default com warning.
    monkeypatch.setenv("MUSICBOX_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("PORT", "abc")
    monkeypatch.setenv("WORKERS", "xyz")
    monkeypatch.setenv("SOCKET_TIMEOUT", "nao-e-numero")
    monkeypatch.setenv("RETRIES", "muitos")
    settings = load_settings()
    assert settings.port == 8080
    assert settings.workers == 2
    assert settings.socket_timeout == 30.0
    assert settings.retries == 2


def test_load_settings_env_invalida_parcial_mantem_validas(monkeypatch, tmp_path):
    # Uma env inválida não contamina as demais (PORT cai no default, WORKERS vale).
    monkeypatch.setenv("MUSICBOX_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("PORT", "abc")
    monkeypatch.setenv("WORKERS", "4")
    settings = load_settings()
    assert settings.port == 8080
    assert settings.workers == 4


def test_parse_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comentario\n"
        "PORT=8080\n"
        'MUSICBOX_DIR="~/Music/musicbox/"\n'
        "\n"
        "LINHA_INVALIDA\n"
        "SOCKET_TIMEOUT=30\n",
        encoding="utf-8",
    )
    values = _parse_env_file(env_file)
    assert values["PORT"] == "8080"
    assert values["MUSICBOX_DIR"] == "~/Music/musicbox/"  # aspas removidas
    assert values["SOCKET_TIMEOUT"] == "30"
    assert "LINHA_INVALIDA" not in values


def test_parse_env_file_ausente(tmp_path):
    assert _parse_env_file(tmp_path / "nao-existe.env") == {}
