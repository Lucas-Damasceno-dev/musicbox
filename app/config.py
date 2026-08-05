"""Configurações do MusicBox.

Carrega as configurações de um arquivo `.env` (parser simples com stdlib) e das
variáveis de ambiente reais, com precedência: env vars > `.env` > defaults.
Sem dependência externa (python-dotenv NÃO é usado).
"""

import os
import shutil
from pathlib import Path


class Settings:
    """Configurações do aplicativo MusicBox.

    Attributes:
        port: Porta do servidor HTTP (env `PORT`).
        musicbox_dir: Diretório onde as músicas baixadas são salvas (env `MUSICBOX_DIR`).
        default_format: Formato padrão de download — "mp3" ou "opus" (env `DEFAULT_FORMAT`).
        workers: Número de downloads simultâneos (env `WORKERS`).
        socket_timeout: Timeout de socket em segundos (env `SOCKET_TIMEOUT`).
        retries: Número de tentativas de download (env `RETRIES`).
        cookies_file: Caminho de um arquivo `cookies.txt` (formato Netscape) para o
            yt-dlp (env `COOKIES_FILE`); `None` se não configurado.
        cookies_from_browser: Nome do navegador (`chrome`, `firefox`, ...) para o
            yt-dlp ler cookies direto (env `COOKIES_FROM_BROWSER`); `None` se não
            configurado. Se `cookies_file` também estiver definido, ele vence.
    """

    def __init__(
        self,
        port: int = 8080,
        musicbox_dir: Path = Path.home() / "Music" / "musicbox",
        default_format: str = "mp3",
        workers: int = 2,
        socket_timeout: float = 30.0,
        retries: int = 2,
        cookies_file: Path | None = None,
        cookies_from_browser: str | None = None,
    ) -> None:
        self.port = port
        self.musicbox_dir = musicbox_dir
        self.default_format = default_format
        self.workers = workers
        self.socket_timeout = socket_timeout
        self.retries = retries
        self.cookies_file = cookies_file
        self.cookies_from_browser = cookies_from_browser

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

    musicbox_dir_raw = _get("MUSICBOX_DIR", str(Path.home() / "Music" / "musicbox"))
    musicbox_dir = Path(os.path.expandvars(os.path.expanduser(musicbox_dir_raw)))

    # COOKIES_FILE: ausente/em-branco → None; `~`/`$HOME` expandidos (igual MUSICBOX_DIR).
    cookies_file_raw = _get("COOKIES_FILE", "").strip()
    cookies_file = (
        Path(os.path.expandvars(os.path.expanduser(cookies_file_raw)))
        if cookies_file_raw
        else None
    )
    # COOKIES_FROM_BROWSER: string simples; ausente/em-branco → None.
    cookies_from_browser = _get("COOKIES_FROM_BROWSER", "").strip() or None

    return Settings(
        port=int(_get("PORT", "8080")),
        musicbox_dir=musicbox_dir,
        default_format=_get("DEFAULT_FORMAT", "mp3"),
        workers=int(_get("WORKERS", "2")),
        socket_timeout=float(_get("SOCKET_TIMEOUT", "30.0")),
        retries=int(_get("RETRIES", "2")),
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
    )
