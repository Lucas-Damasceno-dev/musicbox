"""Modelos de dados do MusicBox.

Dataclasses usadas pelo app: faixas, álbuns e tarefas de download.
Identificadores em inglês; docstrings/comentários em português.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class Track:
    """Faixa musical (música avulsa ou item de um álbum)."""

    yt_id: str
    title: str
    number: int | None = None  # número da faixa no álbum
    duration: int | None = None  # duração em segundos
    cover_url: str | None = None  # URL da capa


@dataclass
class Album:
    """Álbum do YouTube Music, identificado pelo browse_id."""

    id: str  # browse_id do álbum
    title: str
    artist: str
    year: int | None = None
    cover_url: str | None = None
    tracks: list[Track] = field(default_factory=list)


@dataclass
class SearchItem:
    """Item de resultado de busca (artista, álbum, música avulsa ou playlist)."""

    id: str
    title: str
    kind: str  # "artist" | "album" | "song" | "playlist"
    url: str
    thumbnail: str | None = None
    artist: str | None = None


@dataclass
class SearchResults:
    """Resultados de busca agrupados por tipo (músicas, artistas, álbuns e playlists)."""

    artists: list[SearchItem]
    albums: list[SearchItem]
    songs: list[SearchItem] = field(default_factory=list)
    playlists: list[SearchItem] = field(default_factory=list)


@dataclass
class DownloadTask:
    """Tarefa de download de uma música.

    `status` é um de: pending | running | done | failed | skipped | cancelled.
    `stage` é a etapa atual: queued | extracting | converting | moving | done | cancelled.
    `progress` varia de 0.0 a 100.0.
    """

    task_id: str
    yt_id: str
    title: str
    format: str
    artist: str | None = None
    album: str | None = None
    number: int | None = None  # número da faixa no álbum (usado no nome do arquivo)
    status: str = "pending"
    progress: float = 0.0
    stage: str = "queued"
    path: str | None = None  # caminho do arquivo final quando concluído
    error: str | None = None  # mensagem de erro quando falhou
    cover_url: str | None = None  # URL da capa (usada pelo player)
    cancel_requested: bool = False  # flag interno de cancelamento (não serializado)

    def to_dict(self) -> dict:
        """Serializa a tarefa para um dict JSON-friendly (usado no WebSocket/REST)."""
        data = asdict(self)
        data.pop("cancel_requested", None)  # flag interno — não expõe na API
        return data
