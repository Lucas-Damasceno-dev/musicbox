"""Configurações do MusicBox.

Carrega as configurações de um arquivo `.env` (parser simples com stdlib) e das
variáveis de ambiente reais, com precedência: env vars > `.env` > defaults.
Sem dependência externa (python-dotenv NÃO é usado).
"""

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("musicbox")


class Settings:
    """Configurações do aplicativo MusicBox.

    Attributes:
        port: Porta do servidor HTTP (env `PORT`).
        musicbox_dir: Diretório onde as músicas baixadas são salvas (env `MUSICBOX_DIR`).
        default_format: Formato padrão de download — "mp3" ou "opus" (env `DEFAULT_FORMAT`).
        workers: Número de downloads simultâneos (env `WORKERS`).
        socket_timeout: Timeout de socket em segundos (env `SOCKET_TIMEOUT`).
        retries: Número de tentativas de download (env `RETRIES`).
        auth_token: Token de acesso compartilhado exigido nas rotas `/api/*`
            (env `MUSICBOX_TOKEN`); `None` desativa a autenticação.
    """

    def __init__(
        self,
        port: int = 8080,
        musicbox_dir: Path = Path.home() / "Music" / "musicbox",
        default_format: str = "opus",
        workers: int = 2,
        socket_timeout: float = 30.0,
        retries: int = 2,
        auth_token: str | None = None,
    ) -> None:
        self.port = port
        self.musicbox_dir = musicbox_dir
        self.default_format = default_format
        self.workers = workers
        self.socket_timeout = socket_timeout
        self.retries = retries
        self.auth_token = auth_token

    @property
    def has_ffmpeg(self) -> bool:
        """True se o binário `ffmpeg` estiver disponível no PATH (detectado em runtime)."""
        return shutil.which("ffmpeg") is not None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Lê um arquivo `.env` simples (linhas `KEY=VALUE`), ignorando comentários e linhas vazias."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]  # remove aspas opcionais
        values[key] = value
    return values


def load_settings() -> Settings:
    """Carrega as configurações do app.

    Precedência: variáveis de ambiente reais > arquivo `.env` do projeto > defaults.
    `musicbox_dir` com `~` ou `$HOME` é expandido.
    """
    project_dir = Path(__file__).resolve().parent.parent
    env_values = _parse_env_file(project_dir / ".env")

    def _get(key: str, default: str) -> str:
        return os.environ.get(key, env_values.get(key, default))

    def _parse_int(name: str, default: int) -> int:
        raw = _get(name, str(default))
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "MUSICBOX: %s inválido (%r) — usando o padrão %s", name, raw, default
            )
            return default

    def _parse_float(name: str, default: float) -> float:
        raw = _get(name, str(default))
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "MUSICBOX: %s inválido (%r) — usando o padrão %s", name, raw, default
            )
            return default

    musicbox_dir_raw = _get("MUSICBOX_DIR", str(Path.home() / "Music" / "musicbox"))
    musicbox_dir = Path(os.path.expandvars(os.path.expanduser(musicbox_dir_raw)))

    # MUSICBOX_TOKEN: ausente/em-branco → None (autenticação desativada).
    auth_token = _get("MUSICBOX_TOKEN", "").strip() or None

    return Settings(
        port=_parse_int("PORT", 8080),
        musicbox_dir=musicbox_dir,
        default_format=_get("DEFAULT_FORMAT", "opus"),
        workers=_parse_int("WORKERS", 2),
        socket_timeout=_parse_float("SOCKET_TIMEOUT", 30.0),
        retries=_parse_int("RETRIES", 2),
        auth_token=auth_token,
    )
